"""
Phase 3 — MemoryService unified entry point.

The single API surface for all memory operations.  Upper layers
(AgentLoop, ActionExecutor, MessageBuilder) call only this service —
never the raw store, sink, or policy objects directly.

Responsibilities:
- ``remember()`` / ``recall()`` / ``forget()`` — CRUD for long-term memory
- ``record_event()`` — fire-and-forget L1 event sink
- ``get_working_context()`` — aggregate L0 working context for prompt injection
- Coordinate background reflection worker and periodic evaluator
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .evaluator import MemoryEvaluator
from .models import MemoryQuery, MemoryRecord, MemoryResult, Scope
from .policies import InjectPlan, MemoryPolicy, WriteDecision
from .reflection_worker import ReflectionWorker
from .session_sink import SessionSink
from .store import MemoryStore
from .tombstones import TombstoneStore
from .working import WorkingMemoryBuilder

logger = logging.getLogger(__name__)


class MemoryService:
    """Unified memory API.

    Args:
        store: Persistent memory backend (file, SQLite, etc.).
        bot_name: Bot identifier, used for path construction.
        world_id: Current world identifier (default ``"default"``).
        base_dir: Root directory for bot data (e.g. ``bots/<name>/worlds/<world>/``).
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        bot_name: str = "default",
        world_id: str = "default",
        base_dir: Path | None = None,
        llm_reflection: Any = None,
    ) -> None:
        self._store = store
        self._bot_name = bot_name
        self._world_id = world_id
        self._policy = MemoryPolicy()
        self._evaluator = MemoryEvaluator()

        # Paths
        if base_dir is None:
            base_dir = Path("bots") / bot_name
        self._base_dir = Path(base_dir)
        self._world_dir = self._base_dir / "worlds" / world_id
        self._tombstones = TombstoneStore(
            self._world_dir / "memory" / ".tombstones.json"
        )
        self._sink = SessionSink(base_dir=self._base_dir / "worlds", world_id=world_id)
        self._working = WorkingMemoryBuilder()

        # Reflection worker (started separately)
        self._reflection: ReflectionWorker | None = None
        if llm_reflection is not None:
            self._reflection = ReflectionWorker(
                sink=self._sink,
                policy=self._policy,
                store=self._store,
                llm_call=llm_reflection,
            )

    # ═══════════════════════════════════════════════════════════════
    #  Lifecycle
    # ═══════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """Load tombstones and start background workers."""
        await self._tombstones.load()
        if self._reflection is not None:
            await self._reflection.start()

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._reflection is not None:
            await self._reflection.stop()

    # ═══════════════════════════════════════════════════════════════
    #  CRUD
    # ═══════════════════════════════════════════════════════════════

    async def remember(
        self,
        record: MemoryRecord,
        *,
        is_player_requested: bool = False,
    ) -> str:
        """Store a memory record.  Returns the record's stable identifier (name)."""
        logger.info(
            "Memory remember start: name=%s type=%s scope=%s confidence=%.2f "
            "player_requested=%s tags=%s",
            record.name,
            record.type.value,
            record.scope.value,
            record.confidence,
            is_player_requested,
            record.tags,
        )

        # Enforce world_id on scope=world records
        if record.scope == Scope.WORLD and not record.world_id:
            record.world_id = self._world_id

        # Scope content validation
        scope_result = self._policy.validate_scope(record)
        if scope_result.decision == WriteDecision.REJECT:
            logger.warning("Scope validation rejected %s: %s", record.name, scope_result.reason)
            raise ValueError(f"Scope validation failed: {scope_result.reason}")
        if scope_result.suggested_scope is not None:
            logger.info(
                "Scope auto-corrected for %s: %s → %s",
                record.name, record.scope.value, scope_result.suggested_scope.value,
            )
            record.scope = scope_result.suggested_scope

        # Check tombstones
        key = f"{record.type.value}/{record.name}"
        if self._tombstones.is_forgotten(key):
            logger.info("Rejected write for %s — in tombstone list", key)
            raise ValueError(f"'{record.name}' has been forgotten. Use unforget first.")

        # Check for existing record (conflict detection)
        existing = await self._store.get(
            record.name, scope=record.scope, type=record.type.value
        )
        conflicts_exist = existing is not None

        # Policy gate
        result = self._policy.evaluate_write(
            record,
            is_player_requested=is_player_requested,
            conflicts_exist=conflicts_exist,
            tombstone_hit=False,  # already checked
        )

        if result.decision == WriteDecision.REJECT:
            raise ValueError(f"Write rejected by policy: {result.reason}")
        if result.decision == WriteDecision.SHORT_TERM_ONLY:
            logger.info("Record %s kept short-term only: %s", record.name, result.reason)
            return record.name

        # Apply policy suggestions
        if result.suggested_confidence is not None:
            record.confidence = result.suggested_confidence
        if result.suggested_scope is not None:
            record.scope = result.suggested_scope
        if result.suggested_type is not None:
            record.type = result.suggested_type

        record.updated = datetime.now(timezone.utc)

        if existing is not None:
            path = await self._store.update(record)
        else:
            path = await self._store.put(record)

        logger.info(
            "Memory written: %s/%s (%s scope=%s conf=%.2f)",
            record.type.value, record.name, result.decision.value,
            record.scope.value, record.confidence,
        )
        return path

    async def recall(self, query: MemoryQuery) -> list[MemoryResult]:
        """Search / recall records matching *query*.

        Results are ordered by scope priority (WORLD > BOT > MINECRAFT),
        then by relevance score descending.
        """
        logger.info(
            "Memory recall start: name=%s type=%s scope=%s keywords=%s tags=%s "
            "min_conf=%.2f limit=%d world_id=%s",
            query.name,
            query.type.value if query.type else None,
            query.scope.value if query.scope else None,
            query.keywords,
            query.tags,
            query.min_confidence,
            query.limit,
            query.world_id,
        )
        results = await self._store.search(query)
        # Sort by scope priority, then by score
        from .models import SCOPE_PRIORITY

        results.sort(
            key=lambda r: (
                SCOPE_PRIORITY.get(r.record.scope, 99),
                -r.score,
            )
        )
        limited = results[: query.limit]
        logger.info(
            "Memory recall done: count=%d hits=%s",
            len(limited),
            [
                {
                    "name": r.record.name,
                    "type": r.record.type.value,
                    "scope": r.record.scope.value,
                    "score": round(r.score, 3),
                    "matched_on": r.matched_on,
                }
                for r in limited
            ],
        )
        return limited

    async def forget(
        self, name: str, *, scope: Scope | None = None, type: str | None = None
    ) -> bool:
        """Delete a record and add its key to the tombstone list."""
        key = f"{type or '*'}/{name}"
        deleted = await self._store.delete(name, scope=scope, type=type)
        if deleted:
            await self._tombstones.forget(key)
            logger.info("Forgot memory: %s", key)
        return deleted

    async def unforget(self, name: str, *, type: str | None = None) -> bool:
        """Remove a tombstone so the record *could* be re-written."""
        key = f"{type or '*'}/{name}"
        return await self._tombstones.unforget(key)

    # ═══════════════════════════════════════════════════════════════
    #  L1 Event Sink
    # ═══════════════════════════════════════════════════════════════

    async def record_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        session_id: str = "default",
        intent: str | None = None,
    ) -> None:
        """Fire-and-forget: append one event to the L1 NDJSON trace.

        Args:
            event_type: One of ``llm_turn``, ``action_call``, ``action_result``,
                        ``world_event``, ``player_msg``, ``safety_interrupt``.
            payload: Arbitrary event data.
            session_id: Current session identifier.
            intent: For ``player_msg`` events — the classified intent from InputRouter.
        """
        event: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "world_id": self._world_id,
            "type": event_type,
            "payload": payload,
        }
        if intent is not None:
            event["intent"] = intent

        await self._sink.append(event)

    # ═══════════════════════════════════════════════════════════════
    #  Working Context (L0)
    # ═══════════════════════════════════════════════════════════════

    def set_conversation(self, turns: list[str]) -> None:
        self._working.set_conversation(turns)

    def append_conversation(self, turn: str) -> None:
        self._working.append_conversation(turn)

    def set_recalled(self, memories: list[str]) -> None:
        self._working.set_recalled(memories)

    def get_working_context(self, **kwargs: Any) -> Any:
        """Build and return the current WorkingContext for prompt injection."""
        return self._working.build(**kwargs)

    # ═══════════════════════════════════════════════════════════════
    #  Injection plan (for MessageBuilder)
    # ═══════════════════════════════════════════════════════════════

    async def plan_injection(self, token_budget: int = 2000) -> InjectPlan:
        """Return an injection plan for the current world context."""
        records = await self._store.list(
            scope=Scope.WORLD, limit=100,
        )
        # Also fetch bot-scoped records
        bot_records = await self._store.list(
            scope=Scope.BOT, limit=50,
        )
        records.extend(bot_records)
        plan = self._policy.plan_injection(records, token_budget=token_budget)
        logger.info(
            "Memory injection plan: full=%d summary=%d index=%d tokens_est=%d",
            len(plan.full),
            len(plan.summary),
            len(plan.index),
            plan.total_tokens_est,
        )
        return plan

    # ═══════════════════════════════════════════════════════════════
    #  Maintenance
    # ═══════════════════════════════════════════════════════════════

    async def compact(self) -> int:
        """Run TTL expiry, confidence decay, and conflict resolution.

        Returns the total number of records removed.
        """
        # Get all records
        all_records = await self._store.list(limit=10_000)

        # Run evaluator
        report = self._evaluator.evaluate(all_records)

        # Apply mutations
        removed = 0
        for mutation in report.mutations:
            if mutation.action == "expire":
                await self._store.delete(mutation.name)
                removed += 1
            elif mutation.action == "downgrade":
                record = await self._store.get(mutation.name)
                if record is not None and mutation.new_confidence is not None:
                    record.confidence = mutation.new_confidence
                    record.updated = datetime.now(timezone.utc)
                    await self._store.update(record)
            # flag_conflict is informational — logged but no store action
            elif mutation.action == "flag_conflict":
                logger.info(
                    "Conflict: %s vs %s — %s",
                    mutation.name, mutation.conflict_with, mutation.reason,
                )

        # Delegate to store's own compact (index rebuild, etc.)
        store_removed = await self._store.compact()
        removed += store_removed

        # Purge old L1 traces (>30 days)
        cutoff = datetime.now(timezone.utc).replace(
            day=1  # approximate 30-day window
        )
        # Actually use 30 days
        cutoff = datetime.now(timezone.utc)
        from datetime import timedelta
        cutoff = cutoff - timedelta(days=30)
        l1_removed = await self._sink.purge_before(cutoff)
        logger.info("Compact: %d records removed, %d L1 files purged", removed, l1_removed)

        return removed

    # ═══════════════════════════════════════════════════════════════
    #  Reflection triggers
    # ═══════════════════════════════════════════════════════════════

    def trigger_reflection(self, session_id: str, reason: str) -> None:
        """Queue a session for background reflection."""
        if self._reflection is not None:
            self._reflection.trigger(session_id, reason)

    @property
    def store(self) -> MemoryStore:
        """Direct access to the store (for Phase 4a file-store integration)."""
        return self._store

    @property
    def sink(self) -> SessionSink:
        """Direct access to the L1 sink."""
        return self._sink
