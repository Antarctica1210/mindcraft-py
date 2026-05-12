"""
Phase 3 — Memory lifecycle evaluator.

Runs periodic maintenance: TTL expiry, confidence half-life decay,
``invalid_when`` condition evaluation, and conflict resolution between
competing records.

The evaluator is stateless — it receives the full record set from the
store, computes which records should be expired / downgraded / flagged,
and returns a list of mutations for the caller to apply.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import MemoryRecord

# Source trust weights (higher = more trusted in conflict resolution)
SOURCE_WEIGHTS: dict[str, int] = {
    "player_confirmed": 4,
    "player_marked": 3,
    "bot_observed": 2,
    "llm_reflected": 1,
    "imported": 0,
}

# Half-life in seconds for confidence decay (default: ~3 days)
DEFAULT_CONFIDENCE_HALF_LIFE = 259_200  # 72 hours


@dataclass
class EvalMutation:
    """Describes one change the evaluator wants the store to apply."""

    name: str
    action: str  # "expire" | "downgrade" | "flag_conflict"
    new_confidence: float | None = None
    conflict_with: str | None = None
    reason: str = ""


@dataclass
class EvalReport:
    mutations: list[EvalMutation] = field(default_factory=list)
    expired_count: int = 0
    downgraded_count: int = 0
    conflict_count: int = 0


class MemoryEvaluator:
    """Periodic maintenance: TTL, confidence decay, invalid_when, conflicts."""

    def __init__(
        self,
        half_life: float = DEFAULT_CONFIDENCE_HALF_LIFE,
        min_confidence: float = 0.1,
    ) -> None:
        self._half_life = half_life
        self._min_confidence = min_confidence

    # ═══════════════════════════════════════════════════════════════
    #  Main entry point
    # ═══════════════════════════════════════════════════════════════

    def evaluate(
        self,
        records: list[MemoryRecord],
        now: datetime | None = None,
        recalled_names: set[str] | None = None,
    ) -> EvalReport:
        """Run all evaluations and return a consolidated mutation list."""
        now = now or datetime.now(timezone.utc)
        recalled = recalled_names or set()
        report = EvalReport()

        # Phase 1: Per-record checks
        for record in records:
            # TTL expiry
            if record.is_expired(now):
                report.mutations.append(
                    EvalMutation(
                        name=record.name,
                        action="expire",
                        reason=f"TTL {record.ttl}s expired",
                    )
                )
                report.expired_count += 1
                continue

            # invalid_when condition (simple keyword eval for now)
            if self._check_invalid_when(record):
                report.mutations.append(
                    EvalMutation(
                        name=record.name,
                        action="downgrade",
                        new_confidence=0.0,
                        reason=f"invalid_when triggered: {record.invalid_when}",
                    )
                )
                report.downgraded_count += 1
                continue

            # Confidence half-life decay (skip if recently recalled)
            if record.name not in recalled:
                new_conf = self._decay_confidence(record, now)
                if new_conf < record.confidence and new_conf < self._min_confidence:
                    report.mutations.append(
                        EvalMutation(
                            name=record.name,
                            action="expire",
                            reason=f"confidence decayed below {self._min_confidence}",
                        )
                    )
                    report.expired_count += 1
                elif new_conf < record.confidence:
                    report.mutations.append(
                        EvalMutation(
                            name=record.name,
                            action="downgrade",
                            new_confidence=round(new_conf, 3),
                            reason="confidence half-life decay",
                        )
                    )
                    report.downgraded_count += 1

        # Phase 2: Conflict detection (pairwise, same namespace + type)
        conflict_mutations = self._detect_conflicts(records, now)
        report.mutations.extend(conflict_mutations)
        report.conflict_count = sum(
            1 for m in conflict_mutations if "conflict" in m.reason
        )

        return report

    # ═══════════════════════════════════════════════════════════════
    #  Per-record helpers
    # ═══════════════════════════════════════════════════════════════

    def _decay_confidence(self, record: MemoryRecord, now: datetime) -> float:
        """Exponential decay: c * 0.5^(Δt / half_life)."""
        delta = (now - record.updated).total_seconds()
        if delta <= 0:
            return record.confidence
        return record.confidence * math.pow(0.5, delta / self._half_life)

    @staticmethod
    def _check_invalid_when(record: MemoryRecord) -> bool:
        """Evaluate a simple invalid_when condition.

        Currently supports basic keyword checks.  Full expression evaluation
        against bot state is left to Phase 4a integration.
        """
        if not record.invalid_when:
            return False
        # A condition of "true" or "always" means immediate invalidation
        cond = record.invalid_when.strip().lower()
        if cond in ("true", "always"):
            return True
        return False

    # ═══════════════════════════════════════════════════════════════
    #  Conflict detection
    # ═══════════════════════════════════════════════════════════════

    def _detect_conflicts(
        self, records: list[MemoryRecord], now: datetime
    ) -> list[EvalMutation]:
        """Find pairs of records that conflict and resolve them."""
        mutations: list[EvalMutation] = []

        # Group by (type, name) — same identifier but different scopes
        by_key: dict[tuple[str, str], list[MemoryRecord]] = {}
        for r in records:
            key = (r.type.value, r.name)
            by_key.setdefault(key, []).append(r)

        for _key, group in by_key.items():
            if len(group) < 2:
                continue
            # Sort by scope priority (lower index = more specific)
            from .models import SCOPE_PRIORITY

            group.sort(key=lambda r: SCOPE_PRIORITY.get(r.scope, 99))
            winner = group[0]
            for loser in group[1:]:
                # If winner scope has strictly higher priority, flag loser
                winner_prio = SCOPE_PRIORITY.get(winner.scope, 99)
                loser_prio = SCOPE_PRIORITY.get(loser.scope, 99)
                if winner_prio < loser_prio:
                    # Lower priority record is overridden by more specific scope
                    continue  # No mutation needed — recall() already sorts by scope
                # Same priority → resolve by source weight + recency
                winner_score = self._resolve_score(winner, now)
                loser_score = self._resolve_score(loser, now)
                if winner_score > loser_score:
                    mutations.append(
                        EvalMutation(
                            name=loser.name,
                            action="downgrade",
                            new_confidence=round(loser.confidence * 0.5, 3),
                            conflict_with=winner.name,
                            reason="conflict resolution: lower score",
                        )
                    )
                elif loser_score > winner_score:
                    mutations.append(
                        EvalMutation(
                            name=winner.name,
                            action="downgrade",
                            new_confidence=round(winner.confidence * 0.5, 3),
                            conflict_with=loser.name,
                            reason="conflict resolution: lower score",
                        )
                    )
                # If equal, both downgrade (unresolvable)
                else:
                    mutations.append(
                        EvalMutation(
                            name=winner.name,
                            action="downgrade",
                            new_confidence=round(winner.confidence * 0.5, 3),
                            conflict_with=loser.name,
                            reason="conflict unresolved — both downgraded",
                        )
                    )
                    mutations.append(
                        EvalMutation(
                            name=loser.name,
                            action="downgrade",
                            new_confidence=round(loser.confidence * 0.5, 3),
                            conflict_with=winner.name,
                            reason="conflict unresolved — both downgraded",
                        )
                    )
        return mutations

    def _resolve_score(self, record: MemoryRecord, now: datetime) -> float:
        """Composite score: source_weight * recency * confidence."""
        source_w = float(SOURCE_WEIGHTS.get(record.source, 1))
        # Recency factor: 1.0 at updated=now, decaying to 0.5 after 7 days
        age_days = (now - record.updated).total_seconds() / 86400.0
        recency = max(0.5, 1.0 - age_days * (0.5 / 7.0))
        return source_w * recency * record.confidence
