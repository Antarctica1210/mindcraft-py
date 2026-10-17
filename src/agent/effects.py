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
_COLLECTED_EFFECT_RE = re.compile(
    r"\bCollected\s+(?P<count>\d+)x?\s+(?P<item>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_MINED_EFFECT_RE = re.compile(
    r"\bMined\s+(?P<count>\d+)x?\s+(?P<item>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_CRAFTED_EFFECT_RE = re.compile(
    r"\bCrafted\s+(?P<count>\d+)x?\s+(?P<item>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_CRAFTED_SINGLE_EFFECT_RE = re.compile(
    r"\bCrafted\s+(?P<item>(?!\d+x?\b)[A-Za-z0-9_]+)\b",
    re.IGNORECASE,
)
_SMELTED_EFFECT_RE = re.compile(
    r"\bSmelted\s+(?P<count>\d+)x?\s+(?P<item>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_TOOL_SUFFIXES: tuple[str, ...] = (
    "_pickaxe",
    "_axe",
    "_shovel",
    "_hoe",
)
_TOOL_ITEMS: frozenset[str] = frozenset({
    "brush",
    "bucket",
    "compass",
    "fishing_rod",
    "flint_and_steel",
    "shears",
})
_WEAPON_SUFFIXES: tuple[str, ...] = ("_sword",)
_WEAPON_ITEMS: frozenset[str] = frozenset({
    "bow",
    "crossbow",
    "mace",
    "shield",
    "trident",
})


@dataclass
class ActionEffect:
    """Structured effect emitted by a command result."""

    type: str
    name: str
    count: int = 1
    item: str | None = None


def _counted_item(match: re.Match[str] | None) -> tuple[str, int] | None:
    if match is None:
        return None
    item = match.group("item").strip().lower()
    if not item:
        return None
    count_text = match.groupdict().get("count")
    try:
        count = int(count_text) if count_text is not None else 1
    except (TypeError, ValueError):
        count = 1
    return item, max(count, 1)


def _inventory_effect(item_name: str, count: int) -> ActionEffect:
    return ActionEffect(type="inventory", name=item_name, item=item_name, count=count)


def _is_tool_item(item_name: str) -> bool:
    return item_name in _TOOL_ITEMS or item_name.endswith(_TOOL_SUFFIXES)


def _is_weapon_item(item_name: str) -> bool:
    return item_name in _WEAPON_ITEMS or item_name.endswith(_WEAPON_SUFFIXES)


def _crafting_effects(item_name: str, count: int) -> list[ActionEffect]:
    effects = [
        _inventory_effect(item_name, count),
        ActionEffect(type="crafting", name="crafted_item", item=item_name, count=count),
    ]
    if _is_tool_item(item_name):
        effects.append(ActionEffect(
            type="crafting",
            name="crafted_tool",
            item=item_name,
            count=count,
        ))
    if _is_weapon_item(item_name):
        effects.append(ActionEffect(
            type="crafting",
            name="crafted_weapon",
            item=item_name,
            count=count,
        ))
    return effects


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

    if name == "collectBlocks":
        counted = _counted_item(_COLLECTED_EFFECT_RE.search(text))
        if counted is not None:
            item_name, count = counted
            effects.append(_inventory_effect(item_name, count))
            effects.append(ActionEffect(
                type="gathering",
                name="collected_block",
                item=item_name,
                count=count,
            ))
    if name == "collectAnyWood":
        counted = _counted_item(_COLLECTED_EFFECT_RE.search(text))
        if counted is not None:
            item_name, count = counted
            effects.append(_inventory_effect(item_name, count))
            effects.append(ActionEffect(
                type="gathering",
                name="collected_wood",
                item=item_name,
                count=count,
            ))
    if name == "mineOreWithTool":
        counted = _counted_item(_MINED_EFFECT_RE.search(text))
        if counted is None:
            counted = _counted_item(_COLLECTED_EFFECT_RE.search(text))
        if counted is not None:
            item_name, count = counted
            effects.append(_inventory_effect(item_name, count))
            effects.append(ActionEffect(
                type="gathering",
                name="mined_ore",
                item=item_name,
                count=count,
            ))

    if name in {"craftRecipe", "craftWithDependencies"}:
        counted = _counted_item(_CRAFTED_EFFECT_RE.search(text))
        if counted is None:
            counted = _counted_item(_CRAFTED_SINGLE_EFFECT_RE.search(text))
        if counted is not None:
            item_name, count = counted
            effects.extend(_crafting_effects(item_name, count))

    if name == "smeltItem":
        counted = _counted_item(_SMELTED_EFFECT_RE.search(text))
        if counted is not None:
            item_name, count = counted
            effects.append(_inventory_effect(item_name, count))
            effects.append(ActionEffect(
                type="smelting",
                name="smelted_item",
                item=item_name,
                count=count,
            ))

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
