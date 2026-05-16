"""
Shared pytest fixtures and import setup.

Adds ``src/`` to ``sys.path`` so tests can import packages by their flat
name (``from llm.input_router import ...``).

LLM calls go through ``langchain-openai`` (ChatOpenAI / OpenAIEmbeddings)
which is a required runtime dependency — no stubs needed.
"""
from __future__ import annotations

import os
import sys
from typing import Any

# ── Path setup ──────────────────────────────────────────────────────

HERE = os.path.dirname(__file__)
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


# ── Shared fakes ────────────────────────────────────────────────────


class FakeBridge:
    """Minimal in-process stand-in for :class:`mc.SocketBridge`.

    Captures call names and chat output so tests can assert routing
    decisions without a real Mineflayer connection.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.chats: list[str] = []
        # Mutable state — tests tweak these before calling observe()
        self.health: float = 20.0
        self.food: float = 20.0
        self.position: dict[str, float] = {"x": 0.0, "y": 64.0, "z": 0.0}
        self.entities_nearby: str = ""
        self.blocks_nearby: str = "standing on: grass_block"
        self.players_nearby: str = ""
        self.inventory: list[dict[str, Any]] = []
        self.entities: list[dict[str, Any]] = []
        self.nearby_blocks: list[str] = ["grass_block"]
        self.goal: str = "No current goal"
        self._response_overrides: dict[str, Any] = {}

    # ── observation ────────────────────────────────────────────────
    async def observe(self) -> dict[str, Any]:
        self.calls.append(("observe",))
        if "observe" in self._response_overrides:
            return self._response_overrides["observe"]
        return {
            "health": self.health,
            "food": self.food,
            "position": dict(self.position),
            "gamemode": "survival",
            "time_of_day": "day",
            "biome": "plains",
            "entities_nearby": self.entities_nearby,
            "blocks_nearby": self.blocks_nearby,
            "players_nearby": self.players_nearby,
        }

    async def get_inventory(self) -> list[dict[str, Any]]:
        self.calls.append(("get_inventory",))
        if "get_inventory" in self._response_overrides:
            return self._response_overrides["get_inventory"]
        return list(self.inventory)

    async def get_entities(self) -> list[dict[str, Any]]:
        self.calls.append(("get_entities",))
        return list(self.entities)

    async def count_entities(self, entity_type: str = "", range_val: float = 64) -> dict[str, Any]:
        self.calls.append(("count_entities", entity_type, range_val))
        wanted = entity_type.lower()
        matches: list[dict[str, Any]] = []
        bx = float(self.position.get("x", 0.0))
        by = float(self.position.get("y", 0.0))
        bz = float(self.position.get("z", 0.0))
        for entity in self.entities:
            name = str(entity.get("name", "")).lower()
            typ = str(entity.get("type", "")).lower()
            if wanted and wanted not in {name, typ}:
                continue
            pos = entity.get("position", {}) or {}
            dx = bx - float(pos.get("x", 0.0))
            dy = by - float(pos.get("y", 0.0))
            dz = bz - float(pos.get("z", 0.0))
            dist = (dx * dx + dy * dy + dz * dz) ** 0.5
            if dist <= range_val:
                copied = dict(entity)
                copied["distance"] = dist
                matches.append(copied)
        return {
            "entity_type": entity_type,
            "range": range_val,
            "count": len(matches),
            "entities": matches[:20],
        }

    async def get_nearby_blocks(self, distance: int = 16) -> list[str]:
        self.calls.append(("get_nearby_blocks", distance))
        return list(self.nearby_blocks)

    async def get_goal(self) -> str:
        self.calls.append(("get_goal",))
        return self.goal

    # ── communication ─────────────────────────────────────────────
    async def chat(self, message: str) -> None:
        self.chats.append(message)
        self.calls.append(("chat", message))

    # ── actions (record-only) ─────────────────────────────────────
    async def stop(self) -> str:
        self.calls.append(("stop",))
        return "stopped"

    async def collect_blocks(self, block_type: str, count: int = 1) -> str:
        self.calls.append(("collect_blocks", block_type, count))
        return f"collected {count} {block_type}"

    async def break_nearby_blocks(
        self, block_type: str, count: int = 1, range_val: float = 6,
    ) -> str:
        self.calls.append(("break_nearby_blocks", block_type, count, range_val))
        return f"broke {count} {block_type}"

    async def breed_animals(
        self,
        animal_type: str,
        breeding_item: str | None = None,
        range_val: float = 16,
    ) -> str:
        self.calls.append(("breed_animals", animal_type, breeding_item, range_val))
        item = breeding_item or "default_food"
        return f"bred {animal_type} with {item}"

    async def move_to(self, x: float, y: float, z: float) -> str:
        self.calls.append(("move_to", x, y, z))
        return f"moved to ({x},{y},{z})"

    async def go_to_player(self, player_name: str, closeness: float = 3) -> str:
        self.calls.append(("go_to_player", player_name, closeness))
        return f"went to {player_name}"

    async def follow_player(self, player_name: str, follow_dist: float = 4) -> str:
        self.calls.append(("follow_player", player_name, follow_dist))
        return f"following {player_name}"

    async def search_for_block(self, block_type: str, search_range: float = 64) -> str:
        self.calls.append(("search_for_block", block_type, search_range))
        return f"found {block_type}"

    async def move_away(self, distance: float = 16) -> str:
        self.calls.append(("move_away", distance))
        return f"moved away {distance}"

    async def search_for_entity(self, entity_type: str, search_range: float = 64) -> str:
        self.calls.append(("search_for_entity", entity_type, search_range))
        return f"found {entity_type}"

    async def set_control_state(self, control: str, state: bool = True) -> None:
        self.calls.append(("set_control_state", control, state))

    async def attack(self, entity_type: str, range_val: float = 64) -> str:
        self.calls.append(("attack", entity_type, range_val))
        return f"attacking {entity_type}"

    async def pickup_nearby_items(self, distance: float = 8) -> str:
        self.calls.append(("pickup_nearby_items", distance))
        return f"picked up items within {distance}"

    async def get_crafting_plan(self, target_item: str, quantity: int = 1) -> str:
        self.calls.append(("get_crafting_plan", target_item, quantity))
        return f"plan for {quantity} {target_item}"

    async def give_to_player(
        self, player_name: str, item_name: str, num: int = 8,
    ) -> str:
        self.calls.append(("give_to_player", player_name, item_name, num))
        return f"gave {num} {item_name} to {player_name}"

    async def should_place_torch(self) -> dict[str, Any]:
        self.calls.append(("should_place_torch",))
        return {"should_place": False}

    async def place_torch(self) -> str:
        self.calls.append(("place_torch",))
        return "placed torch"

    async def look_at_position(self, x: float, y: float, z: float) -> str:
        self.calls.append(("look_at_position", x, y, z))
        return "looked"

    async def execute_code(self, code: str, timeout_ms: int = 60000) -> dict[str, Any]:
        self.calls.append(("execute_code", code, timeout_ms))
        return {"ok": True, "output": "ran code"}

    # ── crafting / smelting ───────────────────────────────────────
    async def craft_recipe(self, target_item: str, count: int = 1) -> str:
        self.calls.append(("craft_recipe", target_item, count))
        return f"Crafted {count}x {target_item}."

    async def smelt_item(self, item_name: str, count: int = 1) -> str:
        self.calls.append(("smelt_item", item_name, count))
        return f"Smelted {count}x {item_name}."

    # ── item manipulation ─────────────────────────────────────────
    async def equip(self, item_name: str) -> str:
        self.calls.append(("equip", item_name))
        return f"Equipped {item_name}."

    async def consume(self, item_name: str) -> str:
        self.calls.append(("consume", item_name))
        return f"Consumed {item_name}."

    async def discard(self, item_name: str, num: int = -1) -> str:
        self.calls.append(("discard", item_name, num))
        return f"Discarded {num if num >= 0 else 'all'} {item_name}."

    # ── block interaction extras ──────────────────────────────────
    async def place_here(self, block_type: str) -> str:
        self.calls.append(("place_here", block_type))
        return f"Placed {block_type}."

    async def place_block(self, block_type: str) -> str:
        self.calls.append(("place_block", block_type))
        return f"Placed {block_type}."

    async def activate_block(self, block_type: str) -> str:
        self.calls.append(("activate_block", block_type))
        return f"Activated {block_type}."

    async def use_on(self, tool_name: str, target: str) -> str:
        self.calls.append(("use_on", tool_name, target))
        return f"Used {tool_name} on {target}."

    async def clear_furnace(self) -> str:
        self.calls.append(("clear_furnace",))
        return "Furnace cleared."

    # ── helpers for tests ─────────────────────────────────────────
    def action_calls(self) -> list[tuple[Any, ...]]:
        """Return calls excluding observation/chat/query traffic."""
        obs_methods = {
            "observe", "get_inventory", "get_entities",
            "get_nearby_blocks", "get_goal", "chat",
        }
        return [c for c in self.calls if c[0] not in obs_methods]

    def method_called(self, name: str) -> bool:
        """Check if a specific bridge method was ever called."""
        return any(c[0] == name for c in self.calls)

    def method_call_count(self, name: str) -> int:
        """Count how many times a specific method was called."""
        return sum(1 for c in self.calls if c[0] == name)


class SmartBridge(FakeBridge):
    """Extended FakeBridge with intelligent mock behavior for regression tests.

    Instead of returning fixed strings, this bridge checks setup state
    (inventory, nearby_blocks) and produces responses that resemble the
    real socket server output — including error messages for missing
    tools, unknown blocks, and missing items.
    """

    def __init__(self) -> None:
        super().__init__()
        # Response overrides — set per-test to force specific outcomes
        self._response_overrides: dict[str, str | None] = {}
        # Block tools map: block_name → required tool (or None)
        self._block_tools: dict[str, str | None] = {
            "stone": "wooden_pickaxe",
            "cobblestone": "wooden_pickaxe",
            "iron_ore": "stone_pickaxe",
            "coal_ore": "wooden_pickaxe",
            "diamond_ore": "iron_pickaxe",
            "oak_log": None,  # no tool needed
            "dirt": None,
            "sand": None,
            "gravel": None,
            "crafting_table": None,
            "furnace": None,
            "chest": None,
        }
        # Crafting recipes map: item → list of ingredient names
        self._recipes: dict[str, list[str]] = {
            "oak_planks": ["oak_log"],
            "stick": ["oak_planks"],
            "crafting_table": ["oak_planks"],
            "wooden_pickaxe": ["oak_planks", "stick"],
            "wooden_axe": ["oak_planks", "stick"],
            "wooden_shovel": ["oak_planks", "stick"],
            "wooden_sword": ["oak_planks", "stick"],
            "stone_pickaxe": ["cobblestone", "stick"],
            "furnace": ["cobblestone"],
            "torch": ["stick", "coal"],
            "white_bed": ["oak_planks", "white_wool"],
        }

    def override(self, method: str, response: str | None) -> None:
        """Force a specific response string for a method call."""
        self._response_overrides[method] = response

    def _count_item(self, item_name: str) -> int:
        return sum(i["count"] for i in self.inventory if i["name"] == item_name)

    def _remove_item(self, item_name: str, count: int) -> int:
        remaining = max(0, count)
        removed = 0
        for item in list(self.inventory):
            if item["name"] != item_name or remaining <= 0:
                continue
            take = min(int(item["count"]), remaining)
            item["count"] -= take
            remaining -= take
            removed += take
        self.inventory = [i for i in self.inventory if int(i["count"]) > 0]
        return removed

    def _add_item(self, item_name: str, count: int) -> None:
        if count <= 0:
            return
        for item in self.inventory:
            if item["name"] == item_name:
                item["count"] += count
                return
        self.inventory.append({"name": item_name, "count": count})

    # ── rich mocks ────────────────────────────────────────────────

    async def search_for_block(self, block_type: str, search_range: float = 64) -> str:
        self.calls.append(("search_for_block", block_type, search_range))
        if "search_for_block" in self._response_overrides:
            return self._response_overrides["search_for_block"]
        if search_range > 64:
            return f"No {block_type} within 64 blocks."
        if block_type in self.nearby_blocks:
            return f"Found {block_type} nearby."
        return f"No {block_type} nearby (64 block radius)."

    async def collect_blocks(self, block_type: str, count: int = 8) -> str:
        self.calls.append(("collect_blocks", block_type, count))
        if "collect_blocks" in self._response_overrides:
            return self._response_overrides["collect_blocks"]
        required = self._block_tools.get(block_type)
        if required:
            have = any(i["name"] == required for i in self.inventory)
            if not have:
                items = ", ".join(f"{i['name']} x{i['count']}" for i in self.inventory) or "nothing"
                return (
                    f"FAILED: Need a {required} to mine {block_type}. "
                    f"You have: {items}. Craft a {required} first, then retry."
                )
        self._add_item(block_type, count)
        return f"Collected {count}x {block_type}."

    async def break_nearby_blocks(
        self, block_type: str, count: int = 1, range_val: float = 6,
    ) -> str:
        self.calls.append(("break_nearby_blocks", block_type, count, range_val))
        if "break_nearby_blocks" in self._response_overrides:
            return self._response_overrides["break_nearby_blocks"]
        return f"Broke {count}x {block_type}."

    async def give_to_player(
        self, player_name: str, item_name: str, num: int = 8,
    ) -> str:
        self.calls.append(("give_to_player", player_name, item_name, num))
        if "give_to_player" in self._response_overrides:
            return self._response_overrides["give_to_player"]
        have = self._count_item(item_name)
        if have <= 0:
            return f"FAILED: No {item_name} in inventory. Cannot give {num}x {item_name}."
        given = min(have, num)
        if given <= 0:
            return f"FAILED: Could not deliver {num}x {item_name}. No requested item in inventory."
        self._remove_item(item_name, given)
        return f"Gave {given}x {item_name} to {player_name}."

    async def get_crafting_plan(self, target_item: str, quantity: int = 1) -> str:
        self.calls.append(("get_crafting_plan", target_item, quantity))
        if "get_crafting_plan" in self._response_overrides:
            return self._response_overrides["get_crafting_plan"]
        recipe = self._recipes.get(target_item)
        if not recipe:
            return f"No recipe for {target_item}."
        # Check missing
        missing: list[str] = []
        for ing in recipe:
            have = self._count_item(ing)
            needed = quantity
            if have < needed:
                missing.append(f"{ing} (need {needed}, have {have})")
        if missing:
            return (
                f"Crafting {target_item} requires: {', '.join(recipe)}. "
                f"Missing: {'; '.join(missing)}."
            )
        return f"Can craft {quantity}x {target_item} from {', '.join(recipe)}."

    async def craft_recipe(self, target_item: str, count: int = 1) -> str:
        self.calls.append(("craft_recipe", target_item, count))
        if "craft_recipe" in self._response_overrides:
            return self._response_overrides["craft_recipe"]
        if target_item == "furnace":
            needed = 8 * count
            if self._count_item("cobblestone") < needed:
                return f"FAILED: Need {needed} cobblestone to craft {count}x furnace."
            self._remove_item("cobblestone", needed)
            self._add_item("furnace", count)
            return f"Crafted {count}x furnace."
        self._add_item(target_item, count)
        return f"Crafted {count}x {target_item}."

    async def smelt_item(self, item_name: str, count: int = 1) -> str:
        self.calls.append(("smelt_item", item_name, count))
        if "smelt_item" in self._response_overrides:
            return self._response_overrides["smelt_item"]
        have = self._count_item(item_name)
        if have < count:
            return f"FAILED: Not enough {item_name} (have {have}, need {count})."
        fuel_items = ["coal", "charcoal", "oak_planks", "stick"]
        has_fuel = any(
            i["name"] in fuel_items and i["count"] > 0 for i in self.inventory
        )
        if not has_fuel:
            return f"FAILED: No fuel to smelt {item_name}."
        outputs = {
            "porkchop": "cooked_porkchop",
            "mutton": "cooked_mutton",
            "beef": "cooked_beef",
            "chicken": "cooked_chicken",
            "cod": "cooked_cod",
            "salmon": "cooked_salmon",
            "potato": "baked_potato",
            "raw_iron": "iron_ingot",
            "raw_gold": "gold_ingot",
            "raw_copper": "copper_ingot",
        }
        cooked_item = outputs.get(item_name, f"cooked_{item_name}")
        self._remove_item(item_name, count)
        self._add_item(cooked_item, count)
        return f"Smelted {count}x {item_name}."

    def set_inventory(self, items: list[dict[str, Any]]) -> None:
        """Replace inventory with a specific list."""
        self.inventory = list(items)


# Re-export for `from conftest import FakeBridge` in test files.
__all__ = ["FakeBridge", "SmartBridge"]
