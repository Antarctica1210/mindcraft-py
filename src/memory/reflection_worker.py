"""
Phase 3 — Background reflection worker.

Consumes L1 raw event traces and calls a lightweight LLM to generate
L2 episodic summary candidates.  These candidates are then submitted
through ``MemoryPolicy`` before being written to ``memory/episodic/``.

The worker runs as an asyncio background task, waking on triggers:
session end, task completion, idle timeout, or event volume threshold.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import MemoryRecord
from .policies import MemoryPolicy, WriteDecision

logger = logging.getLogger(__name__)

# Default triggers
DEFAULT_IDLE_SECONDS = 300        # 5 min idle
DEFAULT_EVENT_THRESHOLD = 200     # L1 events per session
DEFAULT_MAX_FAILURES = 3          # consecutive failures before marking pending

# Max tokens per reflection run
DEFAULT_REFLECTION_TOKENS = 2000


@dataclass
class ReflectionTrigger:
    session_id: str
    reason: str  # "session_end" | "task_complete" | "task_fail" | "idle" | "volume" | "manual"


@dataclass
class ReflectionResult:
    session_id: str
    candidates: list[MemoryRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    content_hash: str = ""


class ReflectionWorker:
    """Background worker: L1 trace → LLM summary → L2 episodic candidate."""

    def __init__(
        self,
        sink: Any,  # SessionSink (avoid circular import)
        policy: MemoryPolicy,
        store: Any,  # MemoryStore
        llm_call: Callable[..., Any] | None = None,
        *,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        event_threshold: int = DEFAULT_EVENT_THRESHOLD,
        max_failures: int = DEFAULT_MAX_FAILURES,
        max_tokens: int = DEFAULT_REFLECTION_TOKENS,
    ) -> None:
        self._sink = sink
        self._policy = policy
        self._store = store
        self._llm_call = llm_call  # async callable(prompt: str) -> str

        self._idle_seconds = idle_seconds
        self._event_threshold = event_threshold
        self._max_failures = max_failures
        self._max_tokens = max_tokens

        self._task: asyncio.Task[Any] | None = None
        self._pending: set[str] = set()  # session_ids needing reflection
        self._failure_counts: dict[str, int] = {}
        self._seen_hashes: set[str] = set()  # dedup

    # ═══════════════════════════════════════════════════════════════
    #  Lifecycle
    # ═══════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """Start the background reflection loop."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info("ReflectionWorker started (idle=%.0fs, threshold=%d events)",
                     self._idle_seconds, self._event_threshold)

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("ReflectionWorker stopped")

    # ═══════════════════════════════════════════════════════════════
    #  Trigger API (called by MemoryService / AgentLoop)
    # ═══════════════════════════════════════════════════════════════

    def trigger(self, session_id: str, reason: str) -> None:
        """Queue a session for reflection."""
        self._pending.add(session_id)
        logger.debug("Reflection queued for session=%s reason=%s", session_id, reason)

    # ═══════════════════════════════════════════════════════════════
    #  Main loop
    # ═══════════════════════════════════════════════════════════════

    async def _run_loop(self) -> None:
        while True:
            try:
                # Process all queued sessions
                pending = list(self._pending)
                self._pending.clear()

                for session_id in pending:
                    try:
                        await self._reflect_session(session_id)
                    except Exception:
                        logger.exception("Reflection failed for session=%s", session_id)
                        self._failure_counts[session_id] = (
                            self._failure_counts.get(session_id, 0) + 1
                        )
                        if self._failure_counts[session_id] < self._max_failures:
                            self._pending.add(session_id)  # retry
                        else:
                            logger.warning(
                                "Session %s exceeded max reflection failures (%d) — "
                                "marked reflection_pending",
                                session_id,
                                self._max_failures,
                            )

                # Check for volume-based triggers
                await self._check_volume_triggers()

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ReflectionWorker loop error")

            await asyncio.sleep(self._idle_seconds)

    # ═══════════════════════════════════════════════════════════════
    #  Reflection logic
    # ═══════════════════════════════════════════════════════════════

    async def _reflect_session(self, session_id: str) -> None:
        """Consume L1 events for *session_id* and produce L2 candidates."""
        events = await self._sink.read_session(session_id)
        if not events:
            logger.debug("No L1 events found for session=%s", session_id)
            return

        # Build a deterministic content hash (strip timestamps for idempotency)
        content_hash = self._hash_events(events)
        if content_hash in self._seen_hashes:
            logger.debug("Session %s already reflected (hash match)", session_id)
            return

        # If no LLM callable, skip (just log the trace size)
        if self._llm_call is None:
            logger.info(
                "Reflection skipped for session=%s (%d events) — no LLM configured",
                session_id, len(events),
            )
            return

        # Build reflection prompt
        prompt = self._build_prompt(session_id, events)
        if not prompt:
            return

        # Call lightweight LLM
        try:
            response = await self._llm_call(prompt)
        except Exception:
            logger.exception("LLM call failed for session=%s reflection", session_id)
            raise

        # Parse candidates from LLM response
        candidates = self._parse_candidates(session_id, response)

        # Submit each candidate through MemoryPolicy
        for record in candidates:
            result = self._policy.evaluate_write(
                record, is_llm_reflection=True,
            )
            if result.decision in (WriteDecision.AUTO_WRITE, WriteDecision.CANDIDATE):
                if result.suggested_confidence is not None:
                    record.confidence = result.suggested_confidence
                try:
                    await self._store.put(record)
                    logger.info(
                        "L2 episodic written: %s/%s (confidence=%.2f)",
                        record.type.value, record.name, record.confidence,
                    )
                except Exception:
                    logger.exception("Failed to write L2 candidate %s", record.name)

        self._seen_hashes.add(content_hash)
        logger.info(
            "Reflection complete for session=%s: %d candidates from %d events",
            session_id, len(candidates), len(events),
        )

    async def _check_volume_triggers(self) -> None:
        """Queue sessions whose L1 event count exceeds the threshold."""
        sessions = await self._sink.list_sessions()
        for sid in sessions:
            if sid in self._pending:
                continue
            count = await self._sink.session_size(sid)
            if count >= self._event_threshold:
                self._pending.add(sid)
                logger.info("Volume trigger: session=%s has %d events", sid, count)

    # ═══════════════════════════════════════════════════════════════
    #  Prompt & parse
    # ═══════════════════════════════════════════════════════════════

    def _build_prompt(self, session_id: str, events: list[dict[str, Any]]) -> str:
        """Build a compact reflection prompt from L1 events."""
        # Truncate events to fit within token budget
        sample = events[-self._event_threshold:]  # most recent N
        events_json = json.dumps(sample, ensure_ascii=False, default=str)

        # Rough token estimate: truncate if too long
        max_chars = self._max_tokens * 4
        if len(events_json) > max_chars:
            events_json = events_json[:max_chars] + "\n... (truncated)"

        return (
            "You are a memory consolidation assistant for a Minecraft bot.\n\n"
            "Below is a raw event trace from the bot's recent activity. "
            "Extract 1-3 concise, actionable memories that the bot should "
            "remember for future sessions.  For each memory, output a JSON "
            "object with these fields:\n"
            "- name: short unique identifier (slug)\n"
            "- description: one-line summary\n"
            "- type: one of [place, feedback, fact, episodic, skill]\n"
            "- scope: one of [minecraft, bot, world] (world if location-specific)\n"
            "- body: 2-4 sentence markdown with a **Why:** line and a "
            "**How to apply:** line\n\n"
            "Output format: one JSON object per line (NDJSON).\n\n"
            f"Session: {session_id}\n"
            f"Events ({len(sample)}):\n{events_json}\n"
        )

    def _parse_candidates(
        self, session_id: str, llm_response: str
    ) -> list[MemoryRecord]:
        """Parse NDJSON candidates from the LLM response."""
        records: list[MemoryRecord] = []
        for line in llm_response.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # Try to extract JSON object from the line
                import re
                m = re.search(r"\{.*\}", line, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group())
                    except json.JSONDecodeError:
                        continue
                else:
                    continue

            try:
                record = MemoryRecord(
                    name=data.get("name", f"reflection-{session_id[:8]}"),
                    description=data.get("description", ""),
                    type=data.get("type", "episodic"),
                    scope=data.get("scope", "world"),
                    source="llm_reflected",
                    confidence=0.5,  # reflection starts at medium confidence
                    body=data.get("body", ""),
                    evidence=f"L1 session {session_id}",
                )
                records.append(record)
            except Exception:
                logger.debug("Failed to parse reflection candidate: %s", line[:100])
                continue

        return records

    @staticmethod
    def _hash_events(events: list[dict[str, Any]]) -> str:
        """Deterministic hash of event content (timestamp-stripped for idempotency)."""
        stripped = []
        for ev in events:
            ev_copy = {k: v for k, v in ev.items() if k != "ts"}
            stripped.append(ev_copy)
        canonical = json.dumps(stripped, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()
