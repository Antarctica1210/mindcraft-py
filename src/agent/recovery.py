"""Recovery plan generation for recoverable command failures."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .failures import FailureCause
from .models import BotState

WOOD_TYPES: tuple[str, ...] = (
    "oak",
    "spruce",
    "birch",
    "jungle",
    "acacia",
    "dark_oak",
    "mangrove",
    "cherry",
    "pale_oak",
    "crimson",
    "warped",
)
WOOD_PLANK_ITEMS: frozenset[str] = frozenset({
    *(f"{wood}_planks" for wood in WOOD_TYPES),
    "bamboo_planks",
})
TOOL_FAMILY_MATERIAL_COUNTS: dict[str, int] = {
    "sword": 2,
    "pickaxe": 3,
    "axe": 3,
    "shovel": 1,
    "hoe": 2,
}
TOOL_FAMILY_STICK_COUNTS: dict[str, int] = {
    "sword": 1,
    "pickaxe": 2,
    "axe": 2,
    "shovel": 2,
    "hoe": 2,
}
TOOL_MATERIAL_ITEMS: dict[str, str] = {
    "wood": "oak_planks",
    "wooden": "oak_planks",
    "stone": "cobblestone",
    "iron": "iron_ingot",
    "gold": "gold_ingot",
    "golden": "gold_ingot",
    "diamond": "diamond",
}
CRAFTING_TABLE_TOOL_FAMILIES: frozenset[str] = frozenset({"axe", "hoe", "pickaxe"})
RECOVERABLE_TOOL_FAMILIES: frozenset[str] = frozenset({"axe", "pickaxe", "shovel"})
RECOVERABLE_MATERIAL_ITEMS: frozenset[str] = frozenset({
    "stick",
    "crafting_table",
    "furnace",
    *WOOD_PLANK_ITEMS,
})


@dataclass(frozen=True)
class RecoveryStep:
    command: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] | None = None

    def kwargs_dict(self) -> dict[str, Any]:
        return dict(self.kwargs or {})


@dataclass(frozen=True)
class RecoveryPlan:
    cause: FailureCause
    steps: tuple[RecoveryStep, ...]
    retry_original: bool = True
    reason: str = ""
    subgoal_message: str = ""
    subgoal_kind: str = "dependency"


class RecoveryManager:
    """Build deterministic recovery plans from structured failure causes."""

    def plan(
        self,
        *,
        command_name: str,
        cause: FailureCause | None,
        state: BotState,
        requester: str | None = None,
        target_hint: str = "",
    ) -> RecoveryPlan | None:
        if cause is None:
            return None
        if cause.kind == "path_timeout":
            return RecoveryPlan(
                cause=cause,
                steps=(),
                retry_original=False,
                reason=(
                    "path/request timed out; refresh observation and replan "
                    "instead of retrying the same route blindly"
                ),
            )
        if cause.kind == "missing_tool" and command_name == "collectBlocks" and cause.required:
            parts = tool_parts(cause.required)
            if parts is not None and parts[1] not in RECOVERABLE_TOOL_FAMILIES:
                return None
            steps = self.tool_recovery_steps(cause.required, state)
            if steps:
                return RecoveryPlan(
                    cause=cause,
                    steps=tuple(steps),
                    retry_original=True,
                    reason=f"Missing {cause.required} for {cause.target or target_hint}",
                    subgoal_message=(
                        f"Craft {cause.required} for "
                        f"{cause.target or target_hint or 'the failed command'}"
                    ),
                    subgoal_kind="missing_tool",
                )
            return None
        if cause.kind == "missing_materials" and cause.missing:
            if any(item not in RECOVERABLE_MATERIAL_ITEMS for item in cause.missing):
                return None
            steps = self.missing_material_recovery_steps(cause.missing, state)
            if steps:
                missing_text = ", ".join(
                    f"{item} x{count}" for item, count in sorted(cause.missing.items())
                )
                return RecoveryPlan(
                    cause=cause,
                    steps=tuple(steps),
                    retry_original=True,
                    reason=f"Missing materials for {cause.target}: {cause.missing}",
                    subgoal_message=(
                        f"Prepare {missing_text} for "
                        f"{cause.target or 'the failed command'}"
                    ),
                    subgoal_kind="missing_materials",
                )
            return None
        if cause.kind == "nearby_miss" and requester:
            target = target_hint or cause.target or ""
            if not target:
                return None
            if command_name in {"collectBlocks", "searchForBlock"}:
                search = RecoveryStep(
                    "searchForBlock",
                    kwargs={"block_type": target, "search_range": 64},
                )
            elif command_name == "searchForEntity":
                search = RecoveryStep(
                    "searchForEntity",
                    kwargs={"entity_type": target, "search_range": 64},
                )
            else:
                return None
            return RecoveryPlan(
                cause=cause,
                steps=(
                    RecoveryStep(
                        "goToPlayer",
                        kwargs={"player_name": requester, "closeness": 3},
                    ),
                    search,
                ),
                retry_original=False,
                reason=f"Return to {requester}, then search for {target}",
            )
        return None

    def tool_recovery_steps(
        self,
        tool_name: str,
        state: BotState,
    ) -> list[RecoveryStep]:
        inventory = {item.name: item.count for item in state.inventory}
        steps: list[RecoveryStep] = []

        def have(item: str, count: int = 1) -> bool:
            return inventory.get(item, 0) >= count

        def add_craft(item: str, count: int = 1) -> None:
            steps.append(RecoveryStep("craftRecipe", kwargs={"recipe_name": item, "num": count}))

        def ensure_planks(count: int) -> bool:
            current = sum(inventory.get(planks, 0) for planks in WOOD_PLANK_ITEMS)
            if current >= count:
                return True
            best_log = best_inventory_log_item(state)
            family = wood_family_from_log(best_log[0]) if best_log else None
            if family is None:
                return False
            planks = planks_for_wood_family(family)
            crafts = max(1, math.ceil((count - current) / 4))
            add_craft(planks, crafts)
            inventory[planks] = inventory.get(planks, 0) + crafts * 4
            return True

        def consume_planks(count: int) -> None:
            remaining = count
            for planks in sorted(WOOD_PLANK_ITEMS):
                if remaining <= 0:
                    return
                take = min(inventory.get(planks, 0), remaining)
                if take > 0:
                    inventory[planks] -= take
                    remaining -= take

        def ensure_sticks(count: int) -> bool:
            if have("stick", count):
                return True
            missing = count - inventory.get("stick", 0)
            crafts = max(1, math.ceil(missing / 4))
            if not ensure_planks(crafts * 2):
                return False
            consume_planks(crafts * 2)
            add_craft("stick", crafts)
            inventory["stick"] = inventory.get("stick", 0) + crafts * 4
            return True

        parts = tool_parts(tool_name)
        if parts is None:
            add_craft(tool_name, 1)
            return steps

        material, family = parts
        material_item = TOOL_MATERIAL_ITEMS.get(material)
        material_count = TOOL_FAMILY_MATERIAL_COUNTS[family]
        stick_count = TOOL_FAMILY_STICK_COUNTS[family]
        if family in CRAFTING_TABLE_TOOL_FAMILIES and not (
            have("crafting_table", 1) or "crafting_table" in state.nearby_block_types
        ):
            if not ensure_planks(4):
                return []
            consume_planks(4)
            add_craft("crafting_table", 1)
            inventory["crafting_table"] = inventory.get("crafting_table", 0) + 1
        if not ensure_sticks(stick_count):
            return []
        if material in {"wooden", "wood"}:
            if not ensure_planks(material_count):
                return []
            consume_planks(material_count)
        elif material_item is not None:
            if material_item == "cobblestone" and not have("cobblestone", material_count):
                missing = material_count - inventory.get("cobblestone", 0)
                steps.append(RecoveryStep(
                    "collectBlocks",
                    kwargs={"block_type": "cobblestone", "count": missing},
                ))
                inventory["cobblestone"] = inventory.get("cobblestone", 0) + missing
            elif not have(material_item, material_count):
                return []
        else:
            return []
        add_craft(tool_name, 1)
        return steps

    def missing_material_recovery_steps(
        self,
        missing_counts: dict[str, int],
        state: BotState,
    ) -> list[RecoveryStep]:
        inventory = {item.name: item.count for item in state.inventory}
        steps: list[RecoveryStep] = []

        def have(item: str, count: int = 1) -> bool:
            return inventory.get(item, 0) >= count

        def craft(item: str, count: int = 1) -> None:
            steps.append(RecoveryStep("craftRecipe", kwargs={"recipe_name": item, "num": count}))

        def ensure_planks(count: int) -> bool:
            current = sum(inventory.get(planks, 0) for planks in WOOD_PLANK_ITEMS)
            if current >= count:
                return True
            best_log = best_inventory_log_item(state)
            family = wood_family_from_log(best_log[0]) if best_log else None
            if family is None:
                return False
            planks = planks_for_wood_family(family)
            crafts = max(1, math.ceil((count - current) / 4))
            craft(planks, crafts)
            inventory[planks] = inventory.get(planks, 0) + crafts * 4
            return True

        def consume_planks(count: int) -> None:
            remaining = count
            for planks in sorted(WOOD_PLANK_ITEMS):
                if remaining <= 0:
                    return
                take = min(inventory.get(planks, 0), remaining)
                if take > 0:
                    inventory[planks] -= take
                    remaining -= take

        for item, count in sorted(missing_counts.items()):
            if have(item, count):
                continue
            if item == "stick":
                crafts = max(1, math.ceil((count - inventory.get("stick", 0)) / 4))
                if not ensure_planks(crafts * 2):
                    return []
                consume_planks(crafts * 2)
                craft("stick", crafts)
                inventory["stick"] = inventory.get("stick", 0) + crafts * 4
                continue
            if item in WOOD_PLANK_ITEMS:
                if not ensure_planks(count):
                    return []
                continue
            if item == "crafting_table":
                if "crafting_table" in state.nearby_block_types:
                    continue
                if not ensure_planks(4):
                    return []
                consume_planks(4)
                craft("crafting_table", 1)
                inventory["crafting_table"] = inventory.get("crafting_table", 0) + 1
                continue
            if item == "furnace":
                if "furnace" in state.nearby_block_types:
                    continue
                if not have("furnace", 1):
                    missing_cobble = max(0, 8 - inventory.get("cobblestone", 0))
                    if missing_cobble > 0:
                        steps.append(RecoveryStep(
                            "collectBlocks",
                            kwargs={"block_type": "cobblestone", "count": missing_cobble},
                        ))
                        inventory["cobblestone"] = (
                            inventory.get("cobblestone", 0) + missing_cobble
                        )
                    craft("furnace", 1)
                    inventory["furnace"] = inventory.get("furnace", 0) + 1
                steps.append(RecoveryStep("placeHere", kwargs={"block_type": "furnace"}))
                continue
            if item == "cobblestone":
                missing = count - inventory.get("cobblestone", 0)
                if missing > 0:
                    steps.append(RecoveryStep(
                        "collectBlocks",
                        kwargs={"block_type": "cobblestone", "count": missing},
                    ))
                    inventory["cobblestone"] = inventory.get("cobblestone", 0) + missing
                continue
            if item == "coal":
                missing = count - inventory.get("coal", 0)
                if missing > 0:
                    steps.append(RecoveryStep(
                        "collectBlocks",
                        kwargs={"block_type": "coal_ore", "count": missing},
                    ))
                    inventory["coal"] = inventory.get("coal", 0) + missing
                continue
            return []

        return steps


def wood_family_from_log(block_name: str) -> str | None:
    name = block_name.strip().lower()
    if name.startswith("stripped_"):
        name = name.removeprefix("stripped_")
    if name.endswith("_log"):
        return name.removesuffix("_log")
    if name.endswith("_stem"):
        return name.removesuffix("_stem")
    return None


def planks_for_wood_family(family: str) -> str:
    return f"{family}_planks"


def best_inventory_log_item(state: BotState) -> tuple[str, int] | None:
    logs = [
        (item.name, item.count)
        for item in state.inventory
        if item.count > 0 and wood_family_from_log(item.name)
    ]
    if not logs:
        return None
    return max(logs, key=lambda item: item[1])


def tool_parts(tool_name: str) -> tuple[str, str] | None:
    name = tool_name.strip().lower()
    for family in TOOL_FAMILY_MATERIAL_COUNTS:
        suffix = f"_{family}"
        if not name.endswith(suffix):
            continue
        material = name[:-len(suffix)]
        if material:
            return material, family
    return None
