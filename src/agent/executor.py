"""
Phase 2 — Action executor.

Maps parsed CommandCall objects to bot actions via the bridge.
Mirrors the JS ``executeCommand()`` flow in ``src/agent/commands/index.js``.
"""
from __future__ import annotations

import inspect
import logging
import math
import re
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

COMMAND_DOCS: dict[str, dict[str, Any]] = {}
"""Per-command documentation populated by register_builtins()."""


def _to_snake(name: str) -> str:
    """Convert common tool-call camelCase names to Python handler snake_case."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _failure_hint(command_name: str, error: str) -> str | None:
    """Return a short actionable hint for a known failure pattern, or None."""
    el = error.lower()

    if "no recipe for" in el or "does not exist" in el:
        return (
            "The recipe name is incorrect. Use getCraftingPlan to find the "
            "exact item name, then retry with the correct name."
        )
    if "missing ingredient" in el or "don't have" in el or "not enough" in el:
        return (
            "You are missing materials. Check your inventory, then search for "
            "and collect the missing items before retrying craftRecipe."
        )
    if "crafting table" in el:
        return (
            "You need a crafting table nearby. Check $STATS for nearby blocks "
            "— if a table is nearby, move closer to it. If not, collect logs "
            "and craft a crafting table first."
        )
    if "player not found" in el or "could not find" in el:
        return "The target was not found nearby. Try searchForBlock or searchForEntity first."
    if "path" in el and ("not found" in el or "cannot" in el):
        return "The destination is unreachable. Try moveAway or a different route."
    if "inventory full" in el or "no space" in el:
        return "Your inventory is full. Discard unnecessary items or place a chest."
    if "timeout" in el:
        return "The operation timed out. The target may be too far or the server is lagging."

    return None


def _preview(value: Any, *, limit: int = 300) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


WOOD_LOG_FALLBACKS: tuple[str, ...] = (
    "oak_log",
    "spruce_log",
    "birch_log",
    "jungle_log",
    "acacia_log",
    "dark_oak_log",
    "mangrove_log",
    "cherry_log",
    "pale_oak_log",
    "crimson_stem",
    "warped_stem",
)
PICKAXE_TIERS: tuple[str, ...] = (
    "wooden_pickaxe",
    "stone_pickaxe",
    "iron_pickaxe",
    "diamond_pickaxe",
    "netherite_pickaxe",
)
ORE_BLOCK_ALIASES: dict[str, str] = {
    "coal": "coal_ore",
    "iron": "iron_ore",
    "raw_iron": "iron_ore",
    "copper": "copper_ore",
    "raw_copper": "copper_ore",
    "gold": "gold_ore",
    "raw_gold": "gold_ore",
    "redstone": "redstone_ore",
    "diamond": "diamond_ore",
    "emerald": "emerald_ore",
    "lapis": "lapis_ore",
    "lapis_lazuli": "lapis_ore",
}
ORE_MIN_PICKAXE: dict[str, str] = {
    "iron_ore": "stone_pickaxe",
    "deepslate_iron_ore": "stone_pickaxe",
    "copper_ore": "stone_pickaxe",
    "deepslate_copper_ore": "stone_pickaxe",
    "lapis_ore": "stone_pickaxe",
    "deepslate_lapis_ore": "stone_pickaxe",
    "gold_ore": "iron_pickaxe",
    "deepslate_gold_ore": "iron_pickaxe",
    "redstone_ore": "iron_pickaxe",
    "deepslate_redstone_ore": "iron_pickaxe",
    "diamond_ore": "iron_pickaxe",
    "deepslate_diamond_ore": "iron_pickaxe",
    "emerald_ore": "iron_pickaxe",
    "deepslate_emerald_ore": "iron_pickaxe",
}
TOOL_MATERIAL_COUNTS: dict[str, int] = {
    "sword": 2,
    "pickaxe": 3,
    "axe": 3,
    "shovel": 1,
    "hoe": 2,
}
TOOL_STICK_COUNTS: dict[str, int] = {
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
WOOD_FAMILIES: tuple[str, ...] = (
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
    *(f"{family}_planks" for family in WOOD_FAMILIES),
    "bamboo_planks",
})


def _inventory_counts(items: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items or []:
        if isinstance(item, dict):
            name = str(item.get("name", ""))
            count = int(item.get("count", 0) or 0)
        else:
            name = str(getattr(item, "name", ""))
            count = int(getattr(item, "count", 0) or 0)
        if name and count > 0:
            counts[name] = counts.get(name, 0) + count
    return counts


def _is_wood_log(name: str) -> bool:
    lowered = name.strip().lower()
    if lowered.startswith("stripped_"):
        lowered = lowered.removeprefix("stripped_")
    return lowered.endswith("_log") or lowered.endswith("_stem") or lowered.endswith("_hyphae")


def _is_fence_block(name: str) -> bool:
    lowered = name.strip().lower()
    return (
        lowered.endswith("_fence")
        or lowered.endswith("_fence_gate")
        or lowered in {"nether_brick_fence"}
    )


def _wood_family_from_log(name: str) -> str | None:
    lowered = name.strip().lower()
    if lowered.startswith("stripped_"):
        lowered = lowered.removeprefix("stripped_")
    for suffix in ("_log", "_stem", "_hyphae"):
        if lowered.endswith(suffix):
            return lowered.removesuffix(suffix)
    return None


def _planks_for_family(family: str) -> str:
    return f"{family}_planks"


def _tool_parts(tool_name: str) -> tuple[str, str] | None:
    name = tool_name.strip().lower()
    for family in TOOL_MATERIAL_COUNTS:
        suffix = f"_{family}"
        if name.endswith(suffix):
            material = name[:-len(suffix)]
            if material:
                return material, family
    return None


def _has_any(counts: dict[str, int], items: tuple[str, ...], count: int = 1) -> bool:
    return any(counts.get(item, 0) >= count for item in items)


def _pickaxe_satisfies(have: str, required: str) -> bool:
    try:
        return PICKAXE_TIERS.index(have) >= PICKAXE_TIERS.index(required)
    except ValueError:
        return False


def _fuel_capacity(item_name: str) -> float:
    name = item_name.strip().lower()
    if name in {"coal", "charcoal"}:
        return 8.0
    if name in WOOD_PLANK_ITEMS or name.endswith("_log") or name.endswith("_stem"):
        return 1.5
    if name == "stick":
        return 0.5
    if name.startswith("wooden_"):
        return 1.0
    return 0.0


def _inventory_fuel_capacity(counts: dict[str, int]) -> float:
    return sum(_fuel_capacity(name) * count for name, count in counts.items())


def _normalize_ore_block(ore_type: str) -> str:
    name = ore_type.strip().lower()
    if name in ORE_BLOCK_ALIASES:
        return ORE_BLOCK_ALIASES[name]
    if name.endswith("_ore") or name.startswith("deepslate_"):
        return name
    return f"{name}_ore"


def _candidate_plant_positions(
    x: float,
    y: float,
    z: float,
    *,
    search_range: int,
) -> list[tuple[int, int, int]]:
    base_x = math.floor(x)
    base_y = math.floor(y) - 1
    base_z = math.floor(z)
    offsets = [
        (dx, dz)
        for dx in range(-search_range, search_range + 1)
        for dz in range(-search_range, search_range + 1)
    ]
    offsets.sort(
        key=lambda item: (
            abs(item[0]) + abs(item[1]),
            item[0] * item[0] + item[1] * item[1],
        ),
    )
    return [(base_x + dx, base_y, base_z + dz) for dx, dz in offsets]


class ActionExecutor:
    """Executes parsed !command(args) calls against the bot bridge."""

    def __init__(self, bridge: Any = None) -> None:
        self._bridge = bridge
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._wiki_store: Any = None

    def register(self, name: str, handler: Callable[..., Any]) -> None:
        self._handlers[name] = handler

    def register_many(self, handlers: dict[str, Callable[..., Any]]) -> None:
        self._handlers.update(handlers)

    def set_bridge(self, bridge: Any) -> None:
        """Attach the SocketBridge instance used to dispatch commands."""
        self._bridge = bridge

    def set_wiki_store(self, store: Any) -> None:
        """Attach an optional local Minecraft Wiki retrieval store."""
        self._wiki_store = store

    async def execute(
        self,
        command_name: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> str | None:
        name = command_name.lstrip("!")
        handler = self._handlers.get(name)
        if handler is None:
            logger.warning("Unknown command: !%s", name)
            return f"Command !{name} does not exist."
        started = time.perf_counter()
        logger.info(
            "ActionExecutor start: command=%s args=%s kwargs=%s handler=%s",
            name,
            args,
            kwargs or {},
            getattr(handler, "__name__", str(handler)),
        )
        try:
            # Map positional args to handler parameter names so ordering
            # doesn't matter — tool_calls JSON key order can differ from
            # the Python function signature.
            sig = inspect.signature(handler)
            handler_params = list(sig.parameters.keys())[1:]  # skip bridge
            call_kwargs: dict[str, Any] = {}
            for key, value in (kwargs or {}).items():
                snake = _to_snake(key)
                if key in handler_params:
                    call_kwargs[key] = value
                elif key == "range" and "range_val" in handler_params:
                    call_kwargs["range_val"] = value
                elif snake in handler_params:
                    call_kwargs[snake] = value
                else:
                    call_kwargs[key] = value
            if args:
                call_kwargs.update(dict(zip(handler_params, args)))
            result = handler(self._bridge, **call_kwargs)
            if hasattr(result, "__await__"):
                result = await result
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "ActionExecutor done: command=%s elapsed_ms=%.1f result=%s",
                name,
                elapsed_ms,
                _preview(result),
            )
            return str(result) if result is not None else None
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception("Command !%s failed", name)
            err_msg = str(exc)
            hint = _failure_hint(name, err_msg)
            logger.info(
                "ActionExecutor failed: command=%s elapsed_ms=%.1f error=%s hint=%s",
                name,
                elapsed_ms,
                err_msg,
                hint,
            )
            if hint:
                return f"FAILED: {err_msg}\nHINT: {hint}"
            return (
                f"FAILED: {err_msg}\n"
                "HINT: Use getCraftingPlan to check requirements, "
                "or try a different approach."
            )

    def has(self, name: str) -> bool:
        return name.lstrip("!") in self._handlers

    # ═════════════════════════════════════════════════════════════════
    #  Command handlers — each mirrors a JS action / query
    # ═════════════════════════════════════════════════════════════════

    # ── Stop / Control ──────────────────────────────────────────────

    @staticmethod
    async def _stop(bridge: Any, *_args: Any) -> Any:
        result = await bridge.stop()
        try:
            await bridge.set_control_state("jump", False)
        except Exception:
            logger.debug("Failed to release jump after stop", exc_info=True)
        return result

    @staticmethod
    async def _stfu(_bridge: Any, *_args: Any) -> str:
        return "Bot muted (stfu)."  # handled in agent

    @staticmethod
    async def _unshut(_bridge: Any, *_args: Any) -> str:
        return "Bot unmuted."  # handled in agent

    @staticmethod
    async def _new_action(_bridge: Any, code_prompt: str) -> str:
        return f"Dynamic action requested: {code_prompt}"  # handled in agent

    @staticmethod
    async def _restart(_bridge: Any, *_args: Any) -> str:
        return "Restart not supported via socket bridge."

    @staticmethod
    async def _clear_chat(_bridge: Any, *_args: Any) -> str:
        return "Chat history cleared."

    # ── Observation / Queries ───────────────────────────────────────

    @staticmethod
    async def _stats(bridge: Any, *_args: Any) -> str:
        s = await bridge.observe()
        p = s["position"]
        return (
            f"Health: {s['health']:.0f}/20  Food: {s['food']:.0f}/20  "
            f"Pos: ({p['x']:.0f}, {p['y']:.0f}, {p['z']:.0f})  "
            f"Gamemode: {s['gamemode']}  Time: {s['time_of_day']}"
        )

    @staticmethod
    async def _inventory(bridge: Any, *_args: Any) -> str:
        items = await bridge.get_inventory()
        if not items:
            return "Inventory empty."
        return "Inventory:\n" + "\n".join(
            f"  {i['name']} x{i['count']}" for i in items
        )

    @staticmethod
    async def _goal(bridge: Any, *_args: Any) -> Any:
        return await bridge.get_goal()

    @staticmethod
    async def _entities(bridge: Any, *_args: Any) -> str:
        ents = await bridge.get_entities()
        if not ents:
            return "No entities nearby."
        return "\n".join(
            f"  [{e['type']}] {e['name']} id:{e['id']} "
            f"at ({e['position']['x']:.0f}, {e['position']['y']:.0f}, {e['position']['z']:.0f}) "
            f"hp:{e.get('health', '?')}"
            for e in ents[:15]
        )

    @staticmethod
    async def _count_entities(
        bridge: Any, entity_type: str = "", range_val: float = 64, **kwargs: Any,
    ) -> str:
        if "range" in kwargs:
            range_val = kwargs["range"]
        result = await bridge.count_entities(entity_type, range_val)
        if not isinstance(result, dict):
            return str(result)
        name = result.get("entity_type") or entity_type or "entities"
        search_range = result.get("range", range_val)
        count = int(result.get("count", 0) or 0)
        entities = result.get("entities") or []
        if not entities:
            return f"Found 0 {name} within {search_range} blocks."
        details = "\n".join(
            f"  {e.get('name', name)} id:{e.get('id', '?')} "
            f"dist:{float(e.get('distance', 0) or 0):.1f}"
            for e in entities[:10]
            if isinstance(e, dict)
        )
        return f"Found {count} {name} within {search_range} blocks.\n{details}"

    @staticmethod
    async def _nearby_blocks(bridge: Any, *_args: Any) -> str:
        types = await bridge.get_nearby_blocks()
        return "Nearby block types: " + ", ".join(types)

    @staticmethod
    async def _craftable(bridge: Any, *_args: Any) -> str:
        items = await bridge.get_craftable()
        return "Craftable: " + (", ".join(items) if items else "nothing")

    @staticmethod
    async def _help(_bridge: Any, *_args: Any) -> str:
        names = sorted(COMMAND_DOCS.keys())
        return "Available commands: " + ", ".join(names)

    @staticmethod
    async def _modes(_bridge: Any, *_args: Any) -> str:
        # Modes are documented in the system prompt — list names only
        return (
            "Modes: self_preservation, unstuck, cowardice, self_defense, "
            "hunting, item_collecting, torch_placing, elbow_room"
        )

    # ── Movement ────────────────────────────────────────────────────

    @staticmethod
    async def _move_to(bridge: Any, x: float, y: float, z: float) -> Any:
        return await bridge.move_to(x, y, z)

    @staticmethod
    async def _go_to_player(bridge: Any, player_name: str, closeness: float = 3) -> Any:
        return await bridge.go_to_player(player_name, closeness)

    @staticmethod
    async def _follow_player(bridge: Any, player_name: str, follow_dist: float = 4) -> Any:
        return await bridge.follow_player(player_name, follow_dist)

    @staticmethod
    async def _go_to_coordinates(
        bridge: Any, x: float, y: float, z: float, closeness: float = 2,
    ) -> Any:
        return await bridge.move_to(x, y, z)

    @staticmethod
    async def _search_for_block(
        bridge: Any, block_type: str, search_range: float = 64,
    ) -> Any:
        return await bridge.search_for_block(block_type, search_range)

    @staticmethod
    async def _search_for_entity(
        bridge: Any, entity_type: str, search_range: float = 64,
    ) -> Any:
        return await bridge.search_for_entity(entity_type, search_range)

    @staticmethod
    async def _move_away(bridge: Any, distance: float = 16) -> Any:
        return await bridge.move_away(distance)

    @staticmethod
    async def _go_to_bed(bridge: Any, *_args: Any) -> Any:
        return await bridge.go_to_bed()

    @staticmethod
    async def _go_to_surface(bridge: Any, *_args: Any) -> Any:
        return await bridge.go_to_surface()

    @staticmethod
    async def _dig_down(bridge: Any, distance: int = 10) -> Any:
        return await bridge.dig_down(distance)

    @staticmethod
    async def _stay(bridge: Any, seconds: int = -1) -> Any:
        return await bridge.stay(seconds)

    # ── Combat ──────────────────────────────────────────────────────

    @staticmethod
    async def _attack(bridge: Any, entity_type: str, range_val: float = 64) -> Any:
        return await bridge.attack(entity_type, range_val)

    @staticmethod
    async def _attack_player(bridge: Any, player_name: str) -> Any:
        return await bridge.attack_player(player_name)

    # ── Inventory Management ────────────────────────────────────────

    @staticmethod
    async def _equip(bridge: Any, item_name: str) -> Any:
        return await bridge.equip(item_name)

    @staticmethod
    async def _consume(bridge: Any, item_name: str) -> Any:
        return await bridge.consume(item_name)

    @staticmethod
    async def _discard(bridge: Any, item_name: str, num: int = -1) -> Any:
        return await bridge.discard(item_name, num)

    @staticmethod
    async def _give_player(
        bridge: Any, player_name: str, item_name: str, num: int = 8,
    ) -> Any:
        return await bridge.give_to_player(player_name, item_name, num)

    # ── Block Interaction ───────────────────────────────────────────

    @staticmethod
    async def _place_here(bridge: Any, block_type: str) -> Any:
        return await bridge.place_block(block_type)

    @staticmethod
    async def _activate_block(bridge: Any, block_type: str) -> Any:
        return await bridge.activate_block(block_type)

    @staticmethod
    async def _collect_blocks(bridge: Any, block_type: str, count: int = 8) -> Any:
        return await bridge.collect_blocks(block_type, count)

    @staticmethod
    async def _break_nearby_blocks(
        bridge: Any,
        block_type: str,
        count: int = 1,
        range_val: float = 6,
        **kwargs: Any,
    ) -> Any:
        if "range" in kwargs:
            range_val = kwargs["range"]
        return await bridge.break_nearby_blocks(block_type, count, range_val)

    @staticmethod
    async def _breed_animals(
        bridge: Any,
        animal_type: str,
        breeding_item: str | None = None,
        range_val: float = 16,
        **kwargs: Any,
    ) -> Any:
        if "range" in kwargs:
            range_val = kwargs["range"]
        return await bridge.breed_animals(animal_type, breeding_item, range_val)

    @staticmethod
    async def _use_on(bridge: Any, tool_name: str, target: str) -> Any:
        return await bridge.use_on(tool_name, target)

    # ── Chest Operations ────────────────────────────────────────────

    @staticmethod
    async def _view_chest(bridge: Any, *_args: Any) -> Any:
        return await bridge.view_chest()

    @staticmethod
    async def _put_in_chest(bridge: Any, item_name: str, num: int = -1) -> Any:
        return await bridge.put_in_chest(item_name, num)

    @staticmethod
    async def _take_from_chest(bridge: Any, item_name: str, num: int = -1) -> Any:
        return await bridge.take_from_chest(item_name, num)

    # ── Crafting / Smelting ─────────────────────────────────────────

    @staticmethod
    async def _craft_recipe(bridge: Any, recipe_name: str, num: int = 1) -> Any:
        return await bridge.craft_recipe(recipe_name, num)

    @staticmethod
    async def _smelt_item(bridge: Any, item_name: str, num: int = 1) -> Any:
        return await bridge.smelt_item(item_name, num)

    @staticmethod
    async def _clear_furnace(bridge: Any, *_args: Any) -> Any:
        return await bridge.clear_furnace()

    # ── Villager ────────────────────────────────────────────────────

    @staticmethod
    async def _show_villager_trades(bridge: Any, villager_id: int) -> Any:
        return await bridge.show_villager_trades(villager_id)

    @staticmethod
    async def _trade_with_villager(
        bridge: Any, villager_id: int, trade_index: int = 1, count: int = 1,
    ) -> Any:
        return await bridge.trade_with_villager(villager_id, trade_index, count)

    # ── Look ────────────────────────────────────────────────────────

    @staticmethod
    async def _look_at_player(bridge: Any, player_name: str) -> Any:
        return await bridge.look_at_player(player_name)

    @staticmethod
    async def _look_at_position(bridge: Any, x: float, y: float, z: float) -> Any:
        return await bridge.look_at_position(x, y, z)

    # ── Mode / Goal ─────────────────────────────────────────────────

    @staticmethod
    async def _set_mode(_bridge: Any, mode_name: str, on: bool = True) -> str:
        state = "enabled" if on else "disabled"
        return f"Mode '{mode_name}' {state}."

    @staticmethod
    async def _set_self_goal(_bridge: Any, self_prompt: str) -> str:
        return f"Goal set: {self_prompt}"

    @staticmethod
    async def _end_goal(_bridge: Any, *_args: Any) -> str:
        return "Goal ended."

    # ── Conversation ────────────────────────────────────────────────

    @staticmethod
    async def _start_conversation(
        bridge: Any, player_name: str, message: str,
    ) -> Any:
        return await bridge.chat(message)

    @staticmethod
    async def _end_conversation(_bridge: Any, _player_name: str) -> str:
        return "Conversation ended."

    # ── Skills ──────────────────────────────────────────────────────

    @staticmethod
    async def _pickup_nearby_items(bridge: Any, distance: float = 8) -> Any:
        return await bridge.pickup_nearby_items(distance)

    @staticmethod
    async def _till_and_sow(
        bridge: Any, x: float, y: float, z: float, seed_type: str | None = None,
    ) -> Any:
        return await bridge.till_and_sow(x, y, z, seed_type)

    @staticmethod
    async def _plant_seeds_near_player(
        bridge: Any,
        count: int = 8,
        seed_type: str = "wheat_seeds",
        player_name: str | None = None,
        range_val: float = 8,
        **kwargs: Any,
    ) -> str:
        if "range" in kwargs:
            range_val = kwargs["range"]
        wanted = max(1, int(count or 1))
        search_range = max(1, min(16, int(range_val or 8)))
        notes: list[str] = []
        if player_name:
            notes.append(str(await bridge.go_to_player(player_name, 3)))
        state = await bridge.observe()
        pos = state.get("position", {}) if isinstance(state, dict) else {}
        x = float(pos.get("x", 0.0))
        y = float(pos.get("y", 64.0))
        z = float(pos.get("z", 0.0))

        planted = 0
        skipped: list[str] = []
        for px, py, pz in _candidate_plant_positions(
            x,
            y,
            z,
            search_range=search_range,
        ):
            if planted >= wanted:
                break
            try:
                result = await bridge.till_and_sow(px, py, pz, seed_type)
            except Exception as exc:
                if len(skipped) < 5:
                    skipped.append(f"({px},{py},{pz}): {exc}")
                continue
            text = str(result)
            lower = text.lower()
            if (
                "failed:" in lower
                or "cannot till" in lower
                or ("no " in lower and "to plant" in lower)
            ):
                if len(skipped) < 5:
                    skipped.append(f"({px},{py},{pz}): {text}")
                continue
            planted += 1
            notes.append(text)

        if planted <= 0:
            detail = "; ".join(skipped) if skipped else "no valid nearby planting spots"
            raise RuntimeError(f"Batch planting failed: {detail}")
        prefix = (
            f"Planted {planted}x {seed_type} near "
            f"{player_name or 'current position'}."
        )
        if planted < wanted:
            prefix += f" Requested {wanted}; stopped after reachable spots/seeds ran out."
        return "\n".join([prefix, *notes[-5:]])

    @staticmethod
    async def _collect_any_wood(
        bridge: Any,
        count: int = 8,
        range_val: float = 16,
        **kwargs: Any,
    ) -> str:
        if "range" in kwargs:
            range_val = kwargs["range"]
        wanted = max(1, int(count or 1))
        search_range = max(1, min(64, int(range_val or 16)))
        nearby = await bridge.get_nearby_blocks(search_range)
        nearby_logs = [
            str(name)
            for name in nearby or []
            if _is_wood_log(str(name))
        ]
        candidates = list(dict.fromkeys([*nearby_logs, *WOOD_LOG_FALLBACKS]))
        errors: list[str] = []
        for block_type in candidates:
            try:
                result = await bridge.collect_blocks(block_type, wanted)
                return f"Collected wood using {block_type}.\n{result}"
            except Exception as exc:
                if len(errors) < 6:
                    errors.append(f"{block_type}: {exc}")
        raise RuntimeError(
            "Could not collect any wood. Tried: "
            + ("; ".join(errors) if errors else ", ".join(candidates))
        )

    @staticmethod
    async def _mine_ore_with_tool(
        bridge: Any,
        ore_type: str,
        count: int = 8,
    ) -> str:
        wanted = max(1, int(count or 1))
        ore_block = _normalize_ore_block(ore_type)
        required_tool = ORE_MIN_PICKAXE.get(ore_block, "stone_pickaxe")
        counts = _inventory_counts(await bridge.get_inventory())
        if not any(
            _pickaxe_satisfies(item, required_tool) and qty > 0
            for item, qty in counts.items()
        ):
            await ActionExecutor._craft_with_dependencies(
                bridge,
                recipe_name=required_tool,
                num=1,
            )
        result = await bridge.collect_blocks(ore_block, wanted)
        return f"Mined {wanted}x {ore_block} with required tool support.\n{result}"

    @staticmethod
    async def _craft_with_dependencies(
        bridge: Any,
        recipe_name: str,
        num: int = 1,
    ) -> str:
        target = recipe_name.strip().lower()
        wanted = max(1, int(num or 1))
        notes: list[str] = []

        async def counts() -> dict[str, int]:
            return _inventory_counts(await bridge.get_inventory())

        async def refresh_after(result: Any) -> dict[str, int]:
            notes.append(str(result))
            return await counts()

        async def ensure_planks(required: int) -> dict[str, int]:
            inv = await counts()
            if sum(inv.get(item, 0) for item in WOOD_PLANK_ITEMS) >= required:
                return inv
            log_items = [
                (name, count)
                for name, count in inv.items()
                if count > 0 and _wood_family_from_log(name) is not None
            ]
            if not log_items:
                notes.append(await ActionExecutor._collect_any_wood(
                    bridge,
                    count=max(1, math.ceil(required / 4)),
                ))
                inv = await counts()
                log_items = [
                    (name, count)
                    for name, count in inv.items()
                    if count > 0 and _wood_family_from_log(name) is not None
                ]
            if not log_items:
                raise RuntimeError(f"Need logs to craft planks for {target}.")
            log_name, _log_count = max(log_items, key=lambda item: item[1])
            family = _wood_family_from_log(log_name) or "oak"
            planks = _planks_for_family(family)
            current = sum(inv.get(item, 0) for item in WOOD_PLANK_ITEMS)
            crafts = max(1, math.ceil(max(0, required - current) / 4))
            return await refresh_after(await bridge.craft_recipe(planks, crafts))

        async def ensure_sticks(required: int) -> dict[str, int]:
            inv = await counts()
            if inv.get("stick", 0) >= required:
                return inv
            missing = required - inv.get("stick", 0)
            stick_crafts = max(1, math.ceil(missing / 4))
            await ensure_planks(stick_crafts * 2)
            return await refresh_after(await bridge.craft_recipe("stick", stick_crafts))

        async def ensure_crafting_table() -> dict[str, int]:
            inv = await counts()
            nearby = set(str(name) for name in await bridge.get_nearby_blocks(16))
            if "crafting_table" in nearby:
                return inv
            if inv.get("crafting_table", 0) > 0:
                return await refresh_after(await bridge.place_block("crafting_table"))
            await ensure_planks(4)
            inv = await refresh_after(await bridge.craft_recipe("crafting_table", 1))
            return await refresh_after(await bridge.place_block("crafting_table"))

        async def ensure_basic_pickaxe() -> dict[str, int]:
            inv = await counts()
            if any(
                qty > 0 and _pickaxe_satisfies(item, "wooden_pickaxe")
                for item, qty in inv.items()
            ):
                return inv
            await ActionExecutor._craft_with_dependencies(
                bridge,
                recipe_name="wooden_pickaxe",
                num=1,
            )
            return await counts()

        async def ensure_cobblestone(required: int) -> dict[str, int]:
            inv = await counts()
            if inv.get("cobblestone", 0) >= required:
                return inv
            await ensure_basic_pickaxe()
            inv = await counts()
            missing = required - inv.get("cobblestone", 0)
            return await refresh_after(await bridge.collect_blocks("cobblestone", missing))

        async def ensure_furnace() -> dict[str, int]:
            inv = await counts()
            nearby = set(str(name) for name in await bridge.get_nearby_blocks(16))
            if "furnace" in nearby:
                return inv
            if inv.get("furnace", 0) > 0:
                return await refresh_after(await bridge.place_block("furnace"))
            await ensure_cobblestone(8)
            inv = await refresh_after(await bridge.craft_recipe("furnace", 1))
            return await refresh_after(await bridge.place_block("furnace"))

        async def ensure_fuel(items_to_smelt: int) -> dict[str, int]:
            inv = await counts()
            if _inventory_fuel_capacity(inv) >= items_to_smelt:
                return inv

            has_existing_wood = any(
                qty > 0 and (
                    name in WOOD_PLANK_ITEMS or _wood_family_from_log(name) is not None
                )
                for name, qty in inv.items()
            )
            if has_existing_wood:
                needed_capacity = max(0.0, items_to_smelt - _inventory_fuel_capacity(inv))
                await ensure_planks(max(1, math.ceil(needed_capacity / 1.5)))
                inv = await counts()
                if _inventory_fuel_capacity(inv) >= items_to_smelt:
                    return inv

            await ensure_basic_pickaxe()
            inv = await counts()
            missing_capacity = max(1.0, items_to_smelt - _inventory_fuel_capacity(inv))
            coal_count = max(1, math.ceil(missing_capacity / 8))
            return await refresh_after(await bridge.collect_blocks("coal_ore", coal_count))

        async def ensure_material(item_name: str, required: int) -> dict[str, int]:
            inv = await counts()
            if inv.get(item_name, 0) >= required:
                return inv
            if item_name == "cobblestone":
                return await ensure_cobblestone(required)
            if item_name == "iron_ingot" and inv.get("raw_iron", 0) >= required:
                await ensure_furnace()
                await ensure_fuel(required)
                return await refresh_after(await bridge.smelt_item("raw_iron", required))
            if item_name == "gold_ingot" and inv.get("raw_gold", 0) >= required:
                await ensure_furnace()
                await ensure_fuel(required)
                return await refresh_after(await bridge.smelt_item("raw_gold", required))
            if item_name in WOOD_PLANK_ITEMS:
                return await ensure_planks(required)
            raise RuntimeError(
                f"Missing {item_name} x{required} for {target}; cannot prepare it automatically."
            )

        if target == "stick":
            await ensure_sticks(wanted)
        elif target in WOOD_PLANK_ITEMS or target.endswith("_planks"):
            await ensure_planks(wanted)
        elif target == "crafting_table":
            await ensure_planks(4 * wanted)
        elif target == "furnace":
            await ensure_cobblestone(8 * wanted)
        else:
            parts = _tool_parts(target)
            if parts is not None:
                material, family = parts
                await ensure_crafting_table()
                await ensure_sticks(TOOL_STICK_COUNTS[family] * wanted)
                material_item = TOOL_MATERIAL_ITEMS.get(material)
                if material in {"wood", "wooden"}:
                    await ensure_planks(TOOL_MATERIAL_COUNTS[family] * wanted)
                elif material_item is not None:
                    await ensure_material(material_item, TOOL_MATERIAL_COUNTS[family] * wanted)

        result = await bridge.craft_recipe(target, wanted)
        return "\n".join([
            f"Crafted {wanted}x {target} with dependency preparation.",
            *notes[-8:],
            str(result),
        ])

    @staticmethod
    async def _breed_animals_batch(
        bridge: Any,
        animal_type: str,
        pairs: int = 1,
        breeding_item: str | None = None,
        range_val: float = 16,
        **kwargs: Any,
    ) -> str:
        if "range" in kwargs:
            range_val = kwargs["range"]
        wanted = max(1, int(pairs or 1))
        bred = 0
        notes: list[str] = []
        for _idx in range(wanted):
            try:
                result = await bridge.breed_animals(animal_type, breeding_item, range_val)
            except Exception as exc:
                if bred == 0:
                    raise
                notes.append(f"Stopped after {bred}/{wanted} pairs: {exc}")
                break
            bred += 1
            notes.append(str(result))
        return "\n".join([
            f"Bred {bred} animal pairs of {animal_type}.",
            *notes[-6:],
        ])

    @staticmethod
    async def _breed_animals_in_pen_batch(
        bridge: Any,
        animal_type: str,
        pairs: int = 1,
        breeding_item: str | None = None,
        range_val: float = 8,
        player_name: str | None = None,
        **kwargs: Any,
    ) -> str:
        if "range" in kwargs:
            range_val = kwargs["range"]
        wanted = max(1, int(pairs or 1))
        search_range = max(2.0, min(16.0, float(range_val or 8)))
        notes: list[str] = []
        if player_name:
            notes.append(str(await bridge.go_to_player(player_name, 3)))

        nearby = await bridge.get_nearby_blocks(int(max(8, search_range)))
        fence_blocks = sorted({str(name) for name in nearby or [] if _is_fence_block(str(name))})
        if not fence_blocks:
            raise RuntimeError(
                "No nearby fence or fence_gate found. Move into/next to the animal pen first, "
                "or build a fenced pen before using breedAnimalsInPenBatch."
            )

        bred = 0
        for _idx in range(wanted):
            try:
                result = await bridge.breed_animals(animal_type, breeding_item, search_range)
            except Exception as exc:
                if bred == 0:
                    raise
                notes.append(f"Stopped after {bred}/{wanted} pen pairs: {exc}")
                break
            bred += 1
            notes.append(str(result))

        return "\n".join([
            f"Bred {bred} animal pairs of {animal_type} in nearby fenced pen.",
            f"Fence evidence: {', '.join(fence_blocks[:5])}.",
            *notes[-6:],
        ])

    @staticmethod
    async def _use_door(
        bridge: Any, x: float | None = None, y: float | None = None, z: float | None = None,
    ) -> Any:
        return await bridge.use_door(x, y, z)

    @staticmethod
    async def _get_crafting_plan(bridge: Any, target_item: str, quantity: int = 1) -> Any:
        return await bridge.get_crafting_plan(target_item, quantity)

    async def _search_wiki(self, _bridge: Any, query: str) -> str:
        """Search the Minecraft Wiki, preferring the local RAG index."""
        started = time.perf_counter()
        if self._wiki_store is not None and getattr(self._wiki_store, "loaded", False):
            chunks = self._wiki_store.search(query, k=5)
            if chunks:
                elapsed_ms = (time.perf_counter() - started) * 1000
                logger.info(
                    "searchWiki: local hit chunks=%d elapsed_ms=%.1f",
                    len(chunks),
                    elapsed_ms,
                )
                return self._wiki_store.format_results(chunks)

        logger.info("searchWiki: fallback to HTTP")
        import re
        import urllib.error
        import urllib.parse
        import urllib.request

        wiki_not_found = (
            f"{query} was not found on the Minecraft Wiki."
            " Try adjusting your search term."
        )
        url = f"https://minecraft.wiki/w/{urllib.parse.quote(query)}"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "MindcraftPy/0.1"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 404:
                    return wiki_not_found
                html = resp.read().decode("utf-8", errors="replace")
                # Strip HTML tags roughly
                text = re.sub(
                    r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL,
                )
                text = re.sub(
                    r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL,
                )
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"&amp;", "&", text)
                text = re.sub(r"&lt;", "<", text)
                text = re.sub(r"&gt;", ">", text)
                text = re.sub(r"&#?\w+;", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                return text[:3000] if len(text) > 3000 else text
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return wiki_not_found
            return f"Wiki search failed: HTTP {e.code}"
        except Exception as e:
            return f"Wiki search failed: {e}"

    # ── Registration ────────────────────────────────────────────────

    def _register_builtins(self) -> None:
        """Register ALL commands (mirrors JS commands/index.js)."""
        from agent.core import _command_defs as get_cmd_defs  # avoid circular

        cmd_defs = get_cmd_defs()
        for name, cdef in cmd_defs.items():
            COMMAND_DOCS[name] = {
                "description": cdef.description,
                "params": cdef.params,
                "completion_kind": cdef.completion_kind,
            }

        self.register_many({
            # Stop / Control
            "stop": self._stop,
            "stfu": self._stfu,
            "unshut": self._unshut,
            "restart": self._restart,
            "clearChat": self._clear_chat,
            # Observation
            "stats": self._stats,
            "inventory": self._inventory,
            "goal": self._goal,
            "entities": self._entities,
            "countEntities": self._count_entities,
            "nearbyBlocks": self._nearby_blocks,
            "craftable": self._craftable,
            "help": self._help,
            "modes": self._modes,
            # Movement
            "moveTo": self._move_to,
            "goToPlayer": self._go_to_player,
            "followPlayer": self._follow_player,
            "goToCoordinates": self._go_to_coordinates,
            "searchForBlock": self._search_for_block,
            "searchForEntity": self._search_for_entity,
            "moveAway": self._move_away,
            "goToBed": self._go_to_bed,
            "goToSurface": self._go_to_surface,
            "digDown": self._dig_down,
            "stay": self._stay,
            # Combat
            "attack": self._attack,
            "attackPlayer": self._attack_player,
            # Inventory
            "equip": self._equip,
            "consume": self._consume,
            "discard": self._discard,
            "givePlayer": self._give_player,
            # Block Interaction
            "placeHere": self._place_here,
            "activateBlock": self._activate_block,
            "collectBlocks": self._collect_blocks,
            "breakNearbyBlocks": self._break_nearby_blocks,
            "breedAnimals": self._breed_animals,
            "useOn": self._use_on,
            # Chest
            "viewChest": self._view_chest,
            "putInChest": self._put_in_chest,
            "takeFromChest": self._take_from_chest,
            # Crafting / Smelting
            "craftRecipe": self._craft_recipe,
            "smeltItem": self._smelt_item,
            "clearFurnace": self._clear_furnace,
            # Villager
            "showVillagerTrades": self._show_villager_trades,
            "tradeWithVillager": self._trade_with_villager,
            # Look
            "lookAtPlayer": self._look_at_player,
            "lookAtPosition": self._look_at_position,
            # Mode / Goal
            "setMode": self._set_mode,
            "setGoal": self._set_self_goal,
            "endGoal": self._end_goal,
            "newAction": self._new_action,
            # Conversation
            "startConversation": self._start_conversation,
            "endConversation": self._end_conversation,
            # Skills
            "pickupNearbyItems": self._pickup_nearby_items,
            "tillAndSow": self._till_and_sow,
            "plantSeedsNearPlayer": self._plant_seeds_near_player,
            "collectAnyWood": self._collect_any_wood,
            "mineOreWithTool": self._mine_ore_with_tool,
            "craftWithDependencies": self._craft_with_dependencies,
            "breedAnimalsBatch": self._breed_animals_batch,
            "breedAnimalsInPenBatch": self._breed_animals_in_pen_batch,
            "useDoor": self._use_door,
            "getCraftingPlan": self._get_crafting_plan,
            "searchWiki": self._search_wiki,
        })
