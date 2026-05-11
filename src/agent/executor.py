"""
Phase 2 — Action executor.

Maps parsed CommandCall objects to bot actions via the bridge.
Mirrors the JS ``executeCommand()`` flow in ``src/agent/commands/index.js``.
"""
from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

COMMAND_DOCS: dict[str, dict[str, Any]] = {}
"""Per-command documentation populated by register_builtins()."""


class ActionExecutor:
    """Executes parsed !command(args) calls against the bot bridge."""

    def __init__(self, bridge: Any = None) -> None:
        self._bridge = bridge
        self._handlers: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, handler: Callable[..., Any]) -> None:
        self._handlers[name] = handler

    def register_many(self, handlers: dict[str, Callable[..., Any]]) -> None:
        self._handlers.update(handlers)

    def set_bridge(self, bridge: Any) -> None:
        """Attach the SocketBridge instance used to dispatch commands."""
        self._bridge = bridge

    async def execute(self, command_name: str, args: list[Any]) -> str | None:
        name = command_name.lstrip("!")
        handler = self._handlers.get(name)
        if handler is None:
            logger.warning("Unknown command: !%s", name)
            return f"Command !{name} does not exist."
        try:
            # Map positional args to handler parameter names so ordering
            # doesn't matter — tool_calls JSON key order can differ from
            # the Python function signature.
            sig = inspect.signature(handler)
            handler_params = list(sig.parameters.keys())[1:]  # skip bridge
            kwargs = dict(zip(handler_params, args))
            result = handler(self._bridge, **kwargs)
            if hasattr(result, "__await__"):
                result = await result
            return str(result) if result is not None else None
        except Exception as exc:
            logger.exception("Command !%s failed", name)
            return f"Command !{name} failed: {exc}"

    def has(self, name: str) -> bool:
        return name.lstrip("!") in self._handlers

    # ═════════════════════════════════════════════════════════════════
    #  Command handlers — each mirrors a JS action / query
    # ═════════════════════════════════════════════════════════════════

    # ── Stop / Control ──────────────────────────────────────────────

    @staticmethod
    async def _stop(bridge: Any, *_args: Any) -> Any:
        return await bridge.stop()

    @staticmethod
    async def _stfu(_bridge: Any, *_args: Any) -> str:
        return "Bot muted (stfu)."  # handled in agent

    @staticmethod
    async def _unshut(_bridge: Any, *_args: Any) -> str:
        return "Bot unmuted."  # handled in agent

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
    async def _attack(bridge: Any, entity_type: str) -> Any:
        return await bridge.attack(entity_type)

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
        bridge: Any, player_name: str, item_name: str, num: int = 1,
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
    async def _collect_blocks(bridge: Any, block_type: str, count: int = 1) -> Any:
        return await bridge.collect_blocks(block_type, count)

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
    async def _use_door(
        bridge: Any, x: float | None = None, y: float | None = None, z: float | None = None,
    ) -> Any:
        return await bridge.use_door(x, y, z)

    @staticmethod
    async def _get_crafting_plan(bridge: Any, target_item: str, quantity: int = 1) -> Any:
        return await bridge.get_crafting_plan(target_item, quantity)

    @staticmethod
    async def _search_wiki(_bridge: Any, query: str) -> str:
        """Search the Minecraft Wiki — implemented in Python via HTTP."""
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
            }

        self.register_many({
            # Stop / Control
            "stop": self._stop,
            "stfu": self._stfu,
            "restart": self._restart,
            "clearChat": self._clear_chat,
            # Observation
            "stats": self._stats,
            "inventory": self._inventory,
            "goal": self._goal,
            "entities": self._entities,
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
            # Conversation
            "startConversation": self._start_conversation,
            "endConversation": self._end_conversation,
            # Skills
            "pickupNearbyItems": self._pickup_nearby_items,
            "tillAndSow": self._till_and_sow,
            "useDoor": self._use_door,
            "getCraftingPlan": self._get_crafting_plan,
            "searchWiki": self._search_wiki,
        })
