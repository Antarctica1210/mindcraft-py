"""
Phase 7a — Static Minecraft knowledge graph.

Read-only structured queries over exported minecraft-data JSON files
(items, blocks, recipes, entities, foods).  Provides deterministic
answers to recipe lookups, tool requirements, collection paths, and
mining capability checks — no LLM involvement needed.

Usage::

    kg = KnowledgeGraph("data/minecraft/1.21.6")
    await kg.load()
    recipe = kg.get_recipe("oak_planks")
    tools = kg.get_tool_requirements("diamond_ore")
    missing = kg.get_missing_materials("iron_pickaxe", inventory=["iron_ingot"])
    path = kg.get_collection_path("oak_log")
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Tool material ordering (higher = can mine harder blocks)
TOOL_TIERS: dict[str, int] = {
    "wooden": 1,
    "stone": 2,
    "iron": 3,
    "diamond": 4,
    "netherite": 5,
    "golden": 6,
}

# Pattern: tool name suffix → material tier
_TOOL_SUFFIX_MAP: dict[str, str] = {
    "wooden_": "wooden",
    "stone_": "stone",
    "iron_": "iron",
    "diamond_": "diamond",
    "netherite_": "netherite",
    "golden_": "golden",
    "gold_": "golden",
}


class KnowledgeGraph:
    """Read-only structured queries over minecraft-data JSON."""

    def __init__(self, data_dir: str | Path = "data/minecraft/1.21.6") -> None:
        self._data_dir = Path(data_dir)
        self._items: dict[int, dict[str, Any]] = {}
        self._items_by_name: dict[str, int] = {}
        self._blocks: dict[int, dict[str, Any]] = {}
        self._blocks_by_name: dict[str, int] = {}
        self._recipes: list[dict[str, Any]] = []
        self._recipe_index: dict[str, list[dict[str, Any]]] = {}
        self._entities: dict[int, dict[str, Any]] = {}
        self._entities_by_name: dict[str, int] = {}
        self._foods: dict[int, dict[str, Any]] = {}
        self._foods_by_name: dict[str, int] = {}
        self._loaded = False

    # ═══════════════════════════════════════════════════════════════
    #  Load
    # ═══════════════════════════════════════════════════════════════

    async def load(self) -> None:
        """Load all JSON data files.  Idempotent."""
        if self._loaded:
            return

        await self._load_items()
        await self._load_blocks()
        await self._load_recipes()
        await self._load_entities()
        await self._load_foods()
        self._loaded = True
        logger.info(
            "KnowledgeGraph loaded: %d items, %d blocks, %d recipes, %d entities, %d foods",
            len(self._items), len(self._blocks), len(self._recipes),
            len(self._entities), len(self._foods),
        )

    async def _load_items(self) -> None:
        path = self._data_dir / "items.json"
        if not path.exists():
            logger.warning("items.json not found at %s", path)
            return
        data = json.loads(path.read_text())
        for item in data:
            item_id = item.get("id")
            if item_id is None:
                continue
            self._items[item_id] = item
            name = item.get("name", "")
            if name:
                self._items_by_name[name.lower()] = item_id

    async def _load_blocks(self) -> None:
        path = self._data_dir / "blocks.json"
        if not path.exists():
            logger.warning("blocks.json not found at %s", path)
            return
        data = json.loads(path.read_text())
        for block in data:
            block_id = block.get("id")
            if block_id is None:
                continue
            self._blocks[block_id] = block
            name = block.get("name", "")
            if name:
                self._blocks_by_name[name.lower()] = block_id

    async def _load_recipes(self) -> None:
        path = self._data_dir / "recipes.json"
        if not path.exists():
            logger.warning("recipes.json not found at %s", path)
            return
        data = json.loads(path.read_text())
        self._recipes = data
        for recipe in data:
            result = recipe.get("result")
            if not isinstance(result, dict):
                continue
            result_name = result.get("name", "")
            if not result_name:
                continue
            key = result_name.lower()
            # ingredient_names is the pre-resolved list from the export script
            ingredient_names = recipe.get("ingredient_names", [])
            if not isinstance(ingredient_names, list):
                ingredient_names = []
            self._recipe_index.setdefault(key, []).append({
                "result": result,
                "ingredient_names": ingredient_names,
                "inShape": recipe.get("inShape"),
                "requires_table": recipe.get("requires_table", False),
            })

    async def _load_entities(self) -> None:
        path = self._data_dir / "entities.json"
        if not path.exists():
            logger.warning("entities.json not found at %s", path)
            return
        data = json.loads(path.read_text())
        for entity in data:
            entity_id = entity.get("id")
            if entity_id is None:
                continue
            self._entities[entity_id] = entity
            name = entity.get("name", "")
            if name:
                self._entities_by_name[name.lower()] = entity_id

    async def _load_foods(self) -> None:
        path = self._data_dir / "foods.json"
        if not path.exists():
            logger.warning("foods.json not found at %s", path)
            return
        data = json.loads(path.read_text())
        for food in data:
            food_id = food.get("id")
            if food_id is None:
                continue
            self._foods[food_id] = food
            name = food.get("name", "")
            if name:
                self._foods_by_name[name.lower()] = food_id

    # ═══════════════════════════════════════════════════════════════
    #  Name resolution
    # ═══════════════════════════════════════════════════════════════

    def _resolve_item_name(self, name: str) -> str:
        """Normalise a human-friendly item name to the minecraft-data key."""
        return name.lower().replace(" ", "_")

    def get_item_id(self, name: str) -> int | None:
        name = self._resolve_item_name(name)
        return self._items_by_name.get(name)

    def get_item_name(self, item_id: int) -> str | None:
        item = self._items.get(item_id)
        return item.get("name") if item else None

    def get_block_id(self, name: str) -> int | None:
        name = self._resolve_item_name(name)
        return self._blocks_by_name.get(name)

    def get_block_name(self, block_id: int) -> str | None:
        block = self._blocks.get(block_id)
        return block.get("name") if block else None

    # ═══════════════════════════════════════════════════════════════
    #  Query: Recipes
    # ═══════════════════════════════════════════════════════════════

    def get_recipe(self, result_item: str) -> list[dict[str, Any]]:
        """Return all known recipes for *result_item*.

        Each recipe dict includes:
          - result: the output item
          - ingredients: list[str] of required ingredient names
          - requires_table: whether a crafting table is needed
          - shape: the crafting grid shape (if shaped)
        """
        name = self._resolve_item_name(result_item)
        return self._recipe_index.get(name, [])

    def get_recipes_for_ingredient(self, ingredient: str) -> list[dict[str, Any]]:
        """Find all recipes that consume *ingredient*."""
        name = self._resolve_item_name(ingredient)
        results: list[dict[str, Any]] = []
        for result_name, recipes in self._recipe_index.items():
            for recipe in recipes:
                ing_list = [self._resolve_item_name(i) for i in recipe.get("ingredient_names", [])]
                if name in ing_list:
                    results.append(recipe)
        return results

    # ═══════════════════════════════════════════════════════════════
    #  Query: Tool requirements
    # ═══════════════════════════════════════════════════════════════

    def get_tool_requirements(self, block_name: str) -> dict[str, Any]:
        """Return the tool required to mine *block_name*.

        Returns a dict with:
          - tool_type: e.g. "pickaxe", "axe", "shovel"
          - min_tier: minimum material tier name (e.g. "iron")
          - tool_ids: list of tool item IDs that can harvest
          - can_harvest: whether the block can be harvested at all
        """
        name = self._resolve_item_name(block_name)
        block_id = self._blocks_by_name.get(name)
        block = self._blocks.get(block_id) if block_id is not None else None
        if block is None:
            return {"tool_type": "hand", "min_tier": None,
                    "tool_ids": [], "can_harvest": True}

        harvest_tools = block.get("harvestTools", {})
        if not isinstance(harvest_tools, dict) or not harvest_tools:
            return {"tool_type": "hand", "min_tier": None,
                    "tool_ids": [], "can_harvest": True}

        # Map tool item IDs → names and extract tiers
        tool_entries: list[dict[str, Any]] = []
        for tid_str, can_harvest in harvest_tools.items():
            if not can_harvest:
                continue
            tid = int(tid_str)
            item = self._items.get(tid, {})
            tool_name = item.get("name", f"unknown_{tid}")
            tier = self._extract_tool_tier(tool_name)
            tool_entries.append({
                "id": tid, "name": tool_name, "tier": tier,
                "tier_level": TOOL_TIERS.get(tier, 0),
            })

        if not tool_entries:
            return {"tool_type": "hand", "min_tier": None,
                    "tool_ids": [], "can_harvest": True}

        # Find minimum tier required
        tool_entries.sort(key=lambda t: t["tier_level"])
        lowest = tool_entries[0]

        return {
            "tool_type": "pickaxe",  # heuristic — refined by block material
            "min_tier": lowest["tier"],
            "min_tool": lowest["name"],
            "tool_ids": [t["id"] for t in tool_entries],
            "can_harvest": True,
        }

    def can_mine(self, block_name: str, tool_name: str | None = None) -> bool:
        """Check if *block_name* can be mined with *tool_name*.

        If *tool_name* is None, checks if the block can be mined by hand.
        """
        req = self.get_tool_requirements(block_name)
        if tool_name is None:
            # No harvestTools entry means it can be mined by hand
            return len(req.get("tool_ids", [])) == 0

        # Look up the tool's item ID
        tool_id = self.get_item_id(tool_name)
        if tool_id is None:
            return False

        # Check if this tool ID is in the block's harvestTools
        return tool_id in req.get("tool_ids", [])

    @staticmethod
    def _extract_tool_tier(tool_name: str) -> str:
        """Extract material tier from a tool name like 'iron_pickaxe'."""
        name_lower = tool_name.lower()
        for suffix, tier in _TOOL_SUFFIX_MAP.items():
            if name_lower.startswith(suffix):
                return tier
        return "unknown"

    # ═══════════════════════════════════════════════════════════════
    #  Query: Materials
    # ═══════════════════════════════════════════════════════════════

    def get_missing_materials(
        self, target_item: str, inventory: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return materials needed to craft *target_item* that are not in *inventory*.

        Returns a dict:
          - have_all: bool — whether everything is available
          - missing: list[str] — missing ingredient names
          - recipe: dict — the first applicable recipe
          - steps: list[str] — human-readable craft steps
        """
        inventory = inventory or []
        inv_set = {self._resolve_item_name(i) for i in inventory}

        recipes = self.get_recipe(target_item)
        if not recipes:
            return {
                "have_all": False,
                "missing": ["unknown_recipe"],
                "recipe": None,
                "steps": [f"No recipe found for {target_item}"],
            }

        # Use the first (simplest) recipe
        recipe = recipes[0]
        ingredients = recipe.get("ingredient_names", [])
        missing = [
            ing for ing in ingredients
            if self._resolve_item_name(ing) not in inv_set
        ]

        steps: list[str] = []
        if recipe.get("requires_table"):
            if "crafting_table" not in inv_set:
                steps.append("1. Craft or place a crafting table")
                missing.append("crafting_table")

        if missing:
            steps.append(f"2. Gather missing: {', '.join(missing)}")
        else:
            steps.append("2. All materials available")

        steps.append(f"3. Craft {target_item}")

        return {
            "have_all": len(missing) == 0,
            "missing": sorted(set(missing)),
            "recipe": recipe,
            "steps": steps,
        }

    # ═══════════════════════════════════════════════════════════════
    #  Query: Collection path
    # ═══════════════════════════════════════════════════════════════

    def get_collection_path(self, target_item: str) -> dict[str, Any]:
        """Compute the shortest path to obtain *target_item* from raw resources.

        Returns a dict with:
          - direct: bool — whether the item is directly mineable (no crafting)
          - path: list[dict] — ordered steps, each with {action, item, tool?}
          - total_steps: int
        """
        name = self._resolve_item_name(target_item)

        # Check recipes first — if craftable, that's the preferred path
        recipes = self.get_recipe(target_item)
        if recipes:
            recipe = recipes[0]
            path: list[dict[str, Any]] = []

            if recipe.get("requires_table"):
                path.append({"action": "place", "item": "crafting_table"})

            for ing in recipe.get("ingredient_names", []):
                ing_name = self._resolve_item_name(ing)
                if ing_name in self._blocks_by_name:
                    tool_req = self.get_tool_requirements(ing)
                    path.append({
                        "action": "mine",
                        "item": ing,
                        "tool": tool_req.get("min_tool"),
                    })
                else:
                    path.append({"action": "craft", "item": ing})

            path.append({"action": "craft", "item": target_item})

            return {
                "direct": False,
                "path": path,
                "total_steps": len(path),
            }

        # Not craftable — check if directly mineable
        if name in self._blocks_by_name:
            tool_req = self.get_tool_requirements(target_item)
            return {
                "direct": True,
                "path": [{
                    "action": "mine",
                    "item": target_item,
                    "tool": tool_req.get("min_tool"),
                }],
                "total_steps": 1,
            }

        return {
            "direct": False,
            "path": [],
            "total_steps": 0,
            "error": f"No recipe or block data found for {target_item}",
        }

    # ═══════════════════════════════════════════════════════════════
    #  Query: Food values
    # ═══════════════════════════════════════════════════════════════

    def get_food_value(self, item_name: str) -> dict[str, Any] | None:
        """Return nutritional information for a food item."""
        name = self._resolve_item_name(item_name)
        food_id = self._foods_by_name.get(name)
        if food_id is None:
            return None
        food = self._foods.get(food_id, {})
        return {
            "name": food.get("name", item_name),
            "food_points": food.get("foodPoints", 0),
            "saturation": food.get("saturation", 0),
        }

    # ═══════════════════════════════════════════════════════════════
    #  Query: Entity info
    # ═══════════════════════════════════════════════════════════════

    def get_entity_info(self, entity_name: str) -> dict[str, Any] | None:
        """Return entity type metadata."""
        name = self._resolve_item_name(entity_name)
        entity_id = self._entities_by_name.get(name)
        if entity_id is None:
            return None
        entity = self._entities.get(entity_id, {})
        return {
            "id": entity_id,
            "name": entity.get("name", entity_name),
            "type": entity.get("type", "unknown"),
            "height": entity.get("height"),
            "width": entity.get("width"),
            "hostile": entity.get("type") == "mob" and entity.get("category") == "hostile",
        }

    # ═══════════════════════════════════════════════════════════════
    #  Bulk data access (for ActionExecutor integration)
    # ═══════════════════════════════════════════════════════════════

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def items(self) -> dict[int, dict[str, Any]]:
        return self._items

    @property
    def blocks(self) -> dict[int, dict[str, Any]]:
        return self._blocks

    @property
    def items_by_name(self) -> dict[str, int]:
        return self._items_by_name

    @property
    def blocks_by_name(self) -> dict[str, int]:
        return self._blocks_by_name
