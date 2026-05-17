"""Structured action effects for task completion checks."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_DELIVERY_EFFECT_RE = re.compile(
    r"\bGave\s+(?P<count>\d+)x\s+(?P<item>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_PLANTED_EFFECT_RE = re.compile(
    r"\bPlanted\s+(?P<count>\d+)x?\b",
    re.IGNORECASE,
)
_BRED_BATCH_EFFECT_RE = re.compile(
    r"\bBred\s+(?P<count>\d+)\s+animal\s+pairs?\b",
    re.IGNORECASE,
)


@dataclass
class ActionEffect:
    """Structured effect emitted by a command result."""

    type: str
    name: str
    count: int = 1
    item: str | None = None


def action_succeeded(result: Any) -> bool:
    if result is None:
        return False
    text = str(result).strip()
    if not text:
        return False
    return not (text.startswith("FAILED:") or text.startswith("SKIPPED:"))


def action_effects_from_result(command_name: str, result: Any) -> list[ActionEffect]:
    """Extract generic state effects from a command result.

    This is intentionally additive: each command teaches the task loop what
    evidence it produced, while completion code stays generic.
    """
    if not action_succeeded(result):
        return []

    name = command_name.lstrip("!")
    text = str(result or "")
    lower = text.lower()
    effects: list[ActionEffect] = []

    if name == "tillAndSow" and ("tilled" in lower or "planted" in lower):
        effects.append(ActionEffect(type="world_change", name="planted_crop", count=1))
    if name == "plantSeedsNearPlayer":
        match = _PLANTED_EFFECT_RE.search(text)
        count = int(match.group("count")) if match is not None else 1
        effects.append(ActionEffect(type="world_change", name="planted_crop", count=count))

    if name == "breedAnimals" and ("bred" in lower or "breed" in lower):
        effects.append(ActionEffect(type="interaction", name="animal_bred", count=1))
    if name in {"breedAnimalsBatch", "breedAnimalsInPenBatch"}:
        match = _BRED_BATCH_EFFECT_RE.search(text)
        count = int(match.group("count")) if match is not None else 1
        effects.append(ActionEffect(type="interaction", name="animal_bred", count=count))

    if name == "givePlayer":
        match = _DELIVERY_EFFECT_RE.search(text)
        if match is not None:
            item_name = match.group("item")
            effects.append(ActionEffect(
                type="delivery",
                name=item_name,
                item=item_name,
                count=int(match.group("count")),
            ))
        else:
            effects.append(ActionEffect(type="delivery", name="unknown", count=1))

    return effects


def effect_count(
    effects: list[ActionEffect] | tuple[ActionEffect, ...] | None,
    *,
    effect_type: str,
    names: tuple[str, ...],
) -> int:
    wanted = set(names)
    return sum(
        effect.count
        for effect in (effects or [])
        if effect.type == effect_type and effect.name in wanted
    )
