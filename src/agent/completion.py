"""Generic task completion contracts and checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .effects import effect_count
from .models import BotState


@dataclass
class CompletionSpec:
    """Deterministic success boundary for one player task."""

    kind: str
    target_items: tuple[str, ...] = ()
    acceptable_items: tuple[str, ...] = ()
    quantity: int | None = None
    terminal_commands: frozenset[str] = frozenset()
    intermediate_commands: frozenset[str] = frozenset()
    requires_delivery: bool = False
    delivery_score: float = 0.0
    delivery_reason: str = ""


@dataclass(frozen=True)
class CompletionProgress:
    kind: str
    current: int
    required: int
    satisfied: bool
    evidence: str = ""


@dataclass(frozen=True)
class FinishDecision:
    should_finish: bool
    status: str
    reason: str
    progress: CompletionProgress


def inventory_count_from_state(state: BotState, item_name: str) -> int:
    return sum(item.count for item in state.inventory if item.name == item_name)


def inventory_count_any_from_state(state: BotState, item_names: tuple[str, ...]) -> int:
    wanted = set(item_names)
    return sum(item.count for item in state.inventory if item.name in wanted)


def inventory_counts_from_state(state: BotState) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in state.inventory:
        counts[item.name] = counts.get(item.name, 0) + item.count
    return counts


def position_tuple_from_state(state: BotState) -> tuple[float, float, float] | None:
    pos = state.position
    if pos is None:
        return None
    return (float(pos.x), float(pos.y), float(pos.z))


def inventory_count_any(
    inventory: dict[str, int] | None,
    item_names: tuple[str, ...],
) -> int:
    if not inventory:
        return 0
    return sum(int(inventory.get(item_name, 0)) for item_name in item_names)


def inventory_delta_from_baseline(
    task: Any,
    state: BotState,
    item_names: tuple[str, ...],
) -> int:
    current = inventory_count_any(inventory_counts_from_state(state), item_names)
    baseline = inventory_count_any(getattr(task, "baseline_inventory", None), item_names)
    return current - baseline


def task_effect_count(
    task: Any,
    *,
    effect_type: str,
    names: tuple[str, ...],
) -> int:
    count = effect_count(
        getattr(task, "effects", None),
        effect_type=effect_type,
        names=names,
    )

    # Backward-compatible fallback for tests or older events created before
    # structured effects existed.
    if effect_type == "world_change" and "planted_crop" in set(names):
        for event in getattr(task, "events", None) or []:
            if event.kind != "action_result" or event.ok is not True:
                continue
            lower = event.message.lower()
            if "tillandsow" in lower and "failed:" not in lower and "planted" in lower:
                count += 1
    return count


def completion_spec_satisfied(task: Any, bot_state: BotState) -> bool:
    """Generic completion check from baseline state and structured effects."""
    spec = getattr(task, "completion_spec", None)
    if spec is None or spec.kind == "unknown":
        return any(
            event.kind == "completion_evidence" and event.ok is True
            for event in (getattr(task, "events", None) or [])
        )

    requested = spec.quantity or 1

    if spec.requires_delivery:
        delivered = task_effect_count(
            task,
            effect_type="delivery",
            names=spec.acceptable_items or spec.target_items or ("unknown",),
        )
        return delivered >= requested

    if spec.kind in {"collect_item", "craft_item", "cook_item"}:
        if not spec.acceptable_items:
            return False
        delta = inventory_delta_from_baseline(task, bot_state, spec.acceptable_items)
        if delta >= requested:
            return True
        return spec.kind == "cook_item" and (
            inventory_count_any_from_state(bot_state, spec.acceptable_items) >= requested
        )

    if spec.kind == "planting":
        return task_effect_count(
            task,
            effect_type="world_change",
            names=("planted_crop",),
        ) >= requested

    if spec.kind == "breeding":
        return task_effect_count(
            task,
            effect_type="interaction",
            names=("animal_bred",),
        ) >= 1

    if spec.kind == "navigation":
        return any(
            event.kind == "completion_evidence" and event.ok is True
            for event in (getattr(task, "events", None) or [])
        )

    return any(
        event.kind == "completion_evidence" and event.ok is True
        for event in (getattr(task, "events", None) or [])
    )


class CompletionEngine:
    """Generic completion checks from task contract, state, and effects."""

    def contract_satisfied(self, task: Any, bot_state: BotState) -> bool:
        return completion_spec_satisfied(task, bot_state)

    def progress(self, task: Any, bot_state: BotState) -> CompletionProgress:
        spec = getattr(task, "completion_spec", None)
        if spec is None or spec.kind == "unknown":
            satisfied = completion_spec_satisfied(task, bot_state)
            return CompletionProgress(
                kind="unknown",
                current=1 if satisfied else 0,
                required=1,
                satisfied=satisfied,
                evidence="completion_evidence",
            )

        required = spec.quantity or 1
        if spec.requires_delivery:
            names = spec.acceptable_items or spec.target_items or ("unknown",)
            current = task_effect_count(task, effect_type="delivery", names=names)
            return CompletionProgress(
                kind=f"{spec.kind}:delivery",
                current=current,
                required=required,
                satisfied=current >= required,
                evidence="delivery_effect",
            )

        if spec.kind in {"collect_item", "craft_item", "cook_item"}:
            current = (
                inventory_delta_from_baseline(task, bot_state, spec.acceptable_items)
                if spec.acceptable_items else 0
            )
            satisfied = current >= required
            if not satisfied and spec.kind == "cook_item" and spec.acceptable_items:
                current = inventory_count_any_from_state(bot_state, spec.acceptable_items)
                satisfied = current >= required
            return CompletionProgress(
                kind=spec.kind,
                current=current,
                required=required,
                satisfied=satisfied,
                evidence="inventory_delta",
            )

        if spec.kind == "planting":
            current = task_effect_count(
                task,
                effect_type="world_change",
                names=("planted_crop",),
            )
            return CompletionProgress(
                kind=spec.kind,
                current=current,
                required=required,
                satisfied=current >= required,
                evidence="world_change_effect",
            )

        if spec.kind == "breeding":
            current = task_effect_count(
                task,
                effect_type="interaction",
                names=("animal_bred",),
            )
            return CompletionProgress(
                kind=spec.kind,
                current=current,
                required=1,
                satisfied=current >= 1,
                evidence="interaction_effect",
            )

        satisfied = completion_spec_satisfied(task, bot_state)
        return CompletionProgress(
            kind=spec.kind,
            current=1 if satisfied else 0,
            required=1,
            satisfied=satisfied,
            evidence="completion_evidence",
        )

    def should_finish_after_action(
        self,
        task: Any,
        action_result: Any,
        bot_state: BotState,
    ) -> FinishDecision:
        progress = self.progress(task, bot_state)
        if progress.satisfied:
            return FinishDecision(
                should_finish=True,
                status="succeeded",
                reason=(
                    f"Completion contract satisfied by {progress.evidence}: "
                    f"{progress.current}/{progress.required}."
                ),
                progress=progress,
            )
        if getattr(action_result, "ok", True) is False:
            return FinishDecision(
                should_finish=False,
                status="running",
                reason=(
                    f"Action failed but contract is not satisfied: "
                    f"{progress.current}/{progress.required}."
                ),
                progress=progress,
            )
        return FinishDecision(
            should_finish=False,
            status="running",
            reason=(
                f"Completion contract not satisfied yet: "
                f"{progress.current}/{progress.required}."
            ),
            progress=progress,
        )
