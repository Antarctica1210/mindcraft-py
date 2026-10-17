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

import difflib
import json
import logging
import math
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

WOOD_TYPES: tuple[str, ...] = (
    "oak", "spruce", "birch", "jungle", "acacia", "dark_oak",
    "mangrove", "cherry", "bamboo", "crimson", "warped", "pale_oak",
)

WOOL_COLORS: tuple[str, ...] = (
    "white", "orange", "magenta", "light_blue", "yellow", "lime",
    "pink", "gray", "light_gray", "cyan", "purple", "blue",
    "brown", "green", "red", "black",
)

_TOOL_TYPE_PRIORITY: tuple[str, ...] = ("pickaxe", "axe", "shovel", "hoe")


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
        self._block_loot_by_name: dict[str, dict[str, Any]] = {}
        self._entity_loot_by_name: dict[str, dict[str, Any]] = {}
        self._materials: dict[str, dict[str, Any]] = {}
        self._biomes: dict[int, dict[str, Any]] = {}
        self._biomes_by_name: dict[str, int] = {}
        self._features: dict[str, dict[str, Any]] = {}
        self._version: dict[str, Any] = {}
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
        await self._load_block_loot()
        await self._load_entity_loot()
        await self._load_materials()
        await self._load_biomes()
        await self._load_features()
        await self._load_version()
        self._loaded = True
        logger.info(
            (
                "KnowledgeGraph loaded: %d items, %d blocks, %d recipes, "
                "%d entities, %d foods, %d block loot, %d entity loot"
            ),
            len(self._items), len(self._blocks), len(self._recipes),
            len(self._entities), len(self._foods),
            len(self._block_loot_by_name), len(self._entity_loot_by_name),
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
            ingredient_names = [
                self._resolve_item_name(str(name))
                for name in ingredient_names
                if not self._is_empty_recipe_item(name)
            ]
            in_shape = recipe.get("inShape")
            requires_table = bool(recipe.get("requires_table", False))
            requires_table = requires_table or self._shape_requires_table(in_shape)
            self._recipe_index.setdefault(key, []).append({
                "result": result,
                "ingredient_names": ingredient_names,
                "ingredient_counts": self._ingredient_counts(recipe),
                "inShape": in_shape,
                "requires_table": requires_table,
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

    async def _load_block_loot(self) -> None:
        path = self._data_dir / "blockLoot.json"
        if not path.exists():
            logger.info("blockLoot.json not found at %s", path)
            return
        data = json.loads(path.read_text())
        for entry in data:
            block_name = entry.get("block", "")
            if block_name:
                self._block_loot_by_name[self._resolve_item_name(block_name)] = entry

    async def _load_entity_loot(self) -> None:
        path = self._data_dir / "entityLoot.json"
        if not path.exists():
            logger.info("entityLoot.json not found at %s", path)
            return
        data = json.loads(path.read_text())
        for entry in data:
            entity_name = entry.get("entity", "")
            if entity_name:
                self._entity_loot_by_name[self._resolve_item_name(entity_name)] = entry

    async def _load_materials(self) -> None:
        path = self._data_dir / "materials.json"
        if not path.exists():
            logger.info("materials.json not found at %s", path)
            return
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            self._materials = data

    async def _load_biomes(self) -> None:
        path = self._data_dir / "biomes.json"
        if not path.exists():
            logger.info("biomes.json not found at %s", path)
            return
        data = json.loads(path.read_text())
        for biome in data:
            biome_id = biome.get("id")
            if biome_id is None:
                continue
            self._biomes[biome_id] = biome
            name = biome.get("name", "")
            if name:
                self._biomes_by_name[name.lower()] = biome_id

    async def _load_features(self) -> None:
        path = self._data_dir / "features.json"
        if not path.exists():
            logger.info("features.json not found at %s", path)
            return
        data = json.loads(path.read_text())
        for feature in data:
            name = feature.get("name", "")
            if name:
                self._features[name] = feature

    async def _load_version(self) -> None:
        path = self._data_dir / "version.json"
        if not path.exists():
            logger.info("version.json not found at %s", path)
            return
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            self._version = data

    # ═══════════════════════════════════════════════════════════════
    #  Name resolution
    # ═══════════════════════════════════════════════════════════════

    def _resolve_item_name(self, name: str) -> str:
        """Normalise a human-friendly item name to the minecraft-data key."""
        return name.lower().replace(" ", "_")

    @staticmethod
    def _is_empty_recipe_item(value: Any) -> bool:
        if value is None:
            return True
        text = str(value).strip().lower()
        return text in {"", "air", "none", "null", "unknown_null"}

    @classmethod
    def _shape_requires_table(cls, in_shape: Any) -> bool:
        """Infer crafting-table need from recipe shape.

        The exported data can miss ``requires_table`` for shaped recipes.
        Any recipe wider than 2 or taller than 2 cannot fit the player 2x2 grid.
        """
        if not isinstance(in_shape, list) or not in_shape:
            return False
        occupied: list[tuple[int, int]] = []
        for y, row in enumerate(in_shape):
            if not isinstance(row, list):
                continue
            for x, cell in enumerate(row):
                if cls._is_empty_recipe_item(cell):
                    continue
                occupied.append((x, y))
        if not occupied:
            return False
        xs = [x for x, _ in occupied]
        ys = [y for _, y in occupied]
        width = max(xs) - min(xs) + 1
        height = max(ys) - min(ys) + 1
        return width > 2 or height > 2

    def _ingredient_counts(self, recipe: dict[str, Any]) -> dict[str, int]:
        """Return exact ingredient counts when shape data is available."""
        counts: dict[str, int] = {}
        in_shape = recipe.get("inShape")
        if isinstance(in_shape, list):
            for row in in_shape:
                if not isinstance(row, list):
                    continue
                for cell in row:
                    if self._is_empty_recipe_item(cell):
                        continue
                    name = self._resolve_item_name(str(cell))
                    counts[name] = counts.get(name, 0) + 1

        if not counts:
            for name in recipe.get("ingredient_names", []) or []:
                if self._is_empty_recipe_item(name):
                    continue
                norm = self._resolve_item_name(str(name))
                counts[norm] = counts.get(norm, 0) + 1

        return counts

    def _normalise_inventory(
        self, inventory: Any = None,
    ) -> dict[str, int]:
        """Convert inventory inputs to {item_name: count}.

        Accepts old list[str], bridge dicts, InventoryItem-like objects, or
        already-counted dicts.
        """
        counts: dict[str, int] = {}
        if not inventory:
            return counts

        if isinstance(inventory, dict):
            for name, count in inventory.items():
                norm = self._resolve_item_name(str(name))
                try:
                    n = int(count)
                except (TypeError, ValueError):
                    n = 1
                counts[norm] = counts.get(norm, 0) + max(n, 0)
            return counts

        for item in inventory:
            if isinstance(item, str):
                name, count = item, 1
            elif isinstance(item, dict):
                name = str(item.get("name", ""))
                count = int(item.get("count", 1) or 1)
            else:
                name = str(getattr(item, "name", ""))
                count = int(getattr(item, "count", 1) or 1)
            if not name:
                continue
            norm = self._resolve_item_name(name)
            counts[norm] = counts.get(norm, 0) + max(count, 0)
        return counts

    def _category_members(self, category: str) -> list[str]:
        if category == "planks":
            return [f"{wood}_planks" for wood in WOOD_TYPES]
        if category == "logs":
            return [
                name
                for wood in WOOD_TYPES
                for name in (f"{wood}_log", f"stripped_{wood}_log")
            ]
        if category == "wool":
            return [f"{color}_wool" for color in WOOL_COLORS]
        return []

    def _item_category(self, item_name: str) -> str | None:
        name = self._resolve_item_name(item_name)
        if name.endswith("_planks"):
            return "planks"
        if name.endswith("_log") or name.endswith("_stem"):
            return "logs"
        if name.endswith("_wool"):
            return "wool"
        return None

    def _available_count(self, inventory: dict[str, int], item_name: str) -> int:
        """Count exact item plus compatible category items."""
        name = self._resolve_item_name(item_name)
        exact = inventory.get(name, 0)
        if name.endswith("_wool"):
            return exact
        category = self._item_category(name)
        if category is None:
            return exact
        category_total = sum(
            inventory.get(member, 0)
            for member in self._category_members(category)
        )
        return max(exact, category_total)

    def _derived_available_count(self, inventory: dict[str, int], item_name: str) -> int:
        """Count directly available/craftable equivalents for recipe preference."""
        name = self._resolve_item_name(item_name)
        count = inventory.get(name, 0)
        if name.endswith("_planks"):
            wood = name.removesuffix("_planks")
            count += inventory.get(f"{wood}_log", 0) * 4
            count += inventory.get(f"stripped_{wood}_log", 0) * 4
            count += inventory.get(f"{wood}_stem", 0) * 4
            count += inventory.get(f"stripped_{wood}_stem", 0) * 4
        return count

    def _preferred_bed_name(self, inventory: dict[str, int]) -> str:
        for color in WOOL_COLORS:
            if inventory.get(f"{color}_wool", 0) > 0:
                return f"{color}_bed"
        return "white_bed"

    def resolve_craft_target(
        self, target_item: str, inventory: Any = None,
    ) -> str:
        """Resolve player-friendly craft targets to concrete item names."""
        name = self._resolve_item_name(target_item)
        inv_counts = self._normalise_inventory(inventory)

        if name == "bed":
            return self._preferred_bed_name(inv_counts)

        if name in self._recipe_index or name in self._items_by_name:
            return name

        fuzzy = self._fuzzy_match(name, list(self._recipe_index.keys()))
        if fuzzy:
            return fuzzy
        return name

    def _fuzzy_match(self, name: str, candidates: list[str]) -> str | None:
        """Return the closest matching candidate using token+edit-distance.

        Splits the query into underscore tokens, then scores each candidate
        by whole-token matches (dominate) + sequence ratio (tiebreaker).
        This ensures "bed" matches "red_bed" (shared "bed" token) over
        "bedrock" (substring match only).
        """
        target = name.lower()
        tokens = [t for t in target.split("_") if len(t) > 1]
        if not tokens:
            return None

        best, best_score = None, -1.0
        for c in candidates:
            c_tokens = set(c.split("_"))
            token_hits = sum(1 for t in tokens if t in c_tokens)
            ratio = difflib.SequenceMatcher(None, target, c).ratio()
            score = token_hits + ratio
            if score > best_score:
                best_score = score
                best = c

        if best and best_score >= 0.4:
            logger.info("Fuzzy matched %r → %r (score=%.2f)", name, best, best_score)
            return best
        return None

    def get_item_id(self, name: str) -> int | None:
        name = self._resolve_item_name(name)
        item_id = self._items_by_name.get(name)
        if item_id is not None:
            return item_id
        fuzzy = self._fuzzy_match(name, list(self._items_by_name.keys()))
        if fuzzy:
            return self._items_by_name[fuzzy]
        return None

    def get_item_name(self, item_id: int) -> str | None:
        item = self._items.get(item_id)
        return item.get("name") if item else None

    def get_block_id(self, name: str) -> int | None:
        name = self._resolve_item_name(name)
        block_id = self._blocks_by_name.get(name)
        if block_id is not None:
            return block_id
        fuzzy = self._fuzzy_match(name, list(self._blocks_by_name.keys()))
        if fuzzy:
            return self._blocks_by_name[fuzzy]
        return None

    def get_block_name(self, block_id: int) -> str | None:
        block = self._blocks.get(block_id)
        return block.get("name") if block else None

    # ═══════════════════════════════════════════════════════════════
    #  Query: Recipes
    # ═══════════════════════════════════════════════════════════════

    def get_recipe(
        self,
        result_item: str,
        inventory: Any = None,
    ) -> list[dict[str, Any]]:
        """Return all known recipes for *result_item*.

        Each recipe dict includes:
          - result: the output item
          - ingredients: list[str] of required ingredient names
          - requires_table: whether a crafting table is needed
          - shape: the crafting grid shape (if shaped)
        """
        inv_counts = self._normalise_inventory(inventory)
        name = self.resolve_craft_target(result_item, inv_counts)
        recipes = self._recipe_index.get(name)
        if recipes:
            return self._sort_recipes_for_inventory(recipes, inv_counts)
        if name in self._items_by_name:
            # The item exists but is not craftable. Do not fuzzy-match exact
            # known items like cooked_porkchop to unrelated recipes.
            return []
        # Fuzzy fallback
        fuzzy = self._fuzzy_match(name, list(self._recipe_index.keys()))
        if fuzzy:
            return self._sort_recipes_for_inventory(
                self._recipe_index.get(fuzzy, []), inv_counts,
            )
        return []

    def _sort_recipes_for_inventory(
        self,
        recipes: list[dict[str, Any]],
        inventory: dict[str, int],
    ) -> list[dict[str, Any]]:
        """Prefer recipes that match available inventory materials."""
        def score(recipe: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
            counts = recipe.get("ingredient_counts", {}) or {}
            missing_total = 0
            matched_kinds = 0
            exact_hits = 0
            exact_available = 0
            derived_available = 0
            for item, needed in counts.items():
                available = self._available_count(inventory, item)
                exact_available += min(inventory.get(item, 0), needed)
                derived_available += min(
                    self._derived_available_count(inventory, item),
                    needed,
                )
                if available >= needed:
                    matched_kinds += 1
                else:
                    missing_total += needed - available
                if inventory.get(item, 0) >= needed:
                    exact_hits += 1
            return (
                self._recipe_preference_penalty(recipe),
                missing_total,
                -matched_kinds,
                -exact_hits,
                -exact_available,
                -derived_available,
            )

        return sorted(recipes, key=score)

    def _sort_recipes_for_quantity(
        self,
        recipes: list[dict[str, Any]],
        inventory: dict[str, int],
        quantity: int,
    ) -> list[dict[str, Any]]:
        """Prefer recipes that satisfy the requested output quantity."""
        quantity = max(int(quantity or 1), 1)

        def score(recipe: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
            result = recipe.get("result", {}) or {}
            output_count = max(int(result.get("count", 1) or 1), 1)
            crafts_needed = math.ceil(quantity / output_count)
            counts = recipe.get("ingredient_counts", {}) or {}
            missing_total = 0
            matched_kinds = 0
            exact_hits = 0
            exact_available = 0
            derived_available = 0
            for item, per_craft in counts.items():
                needed = int(per_craft) * crafts_needed
                available = self._available_count(inventory, item)
                exact = inventory.get(item, 0)
                derived = self._derived_available_count(inventory, item)
                exact_available += min(exact, needed)
                derived_available += min(derived, needed)
                if available >= needed:
                    matched_kinds += 1
                else:
                    missing_total += needed - available
                if exact >= needed:
                    exact_hits += 1
            return (
                self._recipe_preference_penalty(recipe),
                missing_total,
                -matched_kinds,
                -exact_hits,
                -exact_available,
                -derived_available,
            )

        return sorted(recipes, key=score)

    def _recipe_preference_penalty(self, recipe: dict[str, Any]) -> int:
        """Prefer semantically direct recipes over conversion/recolor recipes."""
        result = recipe.get("result", {}) or {}
        result_name = self._resolve_item_name(str(result.get("name", "")))
        counts = recipe.get("ingredient_counts", {}) or {}
        ingredients = {self._resolve_item_name(name) for name in counts}

        if result_name.endswith("_bed"):
            has_wool = any(name.endswith("_wool") for name in ingredients)
            has_planks = any(self._item_category(name) == "planks" for name in ingredients)
            if has_wool and has_planks:
                return 0
            # Recolor recipes such as black_bed + light_gray_dye are valid, but
            # they are a poor plan when the user simply asked to craft a bed.
            return 10

        return 0

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
                    "tool_ids": [], "can_harvest": True, "requires_tool": False}

        material = str(block.get("material", "") or "")
        material_tool_type = self._tool_type_from_material(material)
        harvest_tools = block.get("harvestTools", {})
        requires_tool = isinstance(harvest_tools, dict) and bool(harvest_tools)

        tool_entries: list[dict[str, Any]] = []
        if requires_tool:
            # Map tool item IDs → names and extract tiers. harvestTools marks
            # hard requirements, e.g. ores that need a pickaxe tier.
            for tid_str, can_harvest in harvest_tools.items():
                if not can_harvest:
                    continue
                try:
                    tid = int(tid_str)
                except (TypeError, ValueError):
                    continue
                tool_name = self.get_item_name(tid) or f"unknown_{tid}"
                tier = self._extract_tool_tier(tool_name)
                tool_entries.append({
                    "id": tid, "name": tool_name, "tier": tier,
                    "tier_level": TOOL_TIERS.get(tier, 0),
                })
        elif material_tool_type:
            # Many axe/shovel/hoe blocks are harvestable by hand, so they have
            # no harvestTools requirement. Use material speeds to expose the
            # preferred tool family for planning.
            tool_entries = self._tool_entries_for_type(material_tool_type)

        if not tool_entries:
            return {
                "tool_type": material_tool_type or "hand",
                "min_tier": None,
                "tool_ids": [],
                "can_harvest": True,
                "requires_tool": False,
                "material": material,
            }

        tool_type = material_tool_type or self._tool_type_from_entries(tool_entries) or "hand"

        # Find minimum tier required / preferred.
        tool_entries.sort(key=lambda t: t["tier_level"])
        lowest = tool_entries[0]

        return {
            "tool_type": tool_type,
            "min_tier": lowest["tier"],
            "min_tool": lowest["name"],
            "tool_ids": [t["id"] for t in tool_entries],
            "tool_names": [str(t["name"]) for t in tool_entries],
            "tool_options": [
                {
                    "id": t["id"],
                    "name": t["name"],
                    "tier": t["tier"],
                    "speed": self._tool_speed_for_block_material(material, int(t["id"])),
                }
                for t in tool_entries
            ],
            "can_harvest": True,
            "requires_tool": requires_tool,
            "material": material,
        }

    def select_tool_for_block(
        self,
        block_name: str,
        inventory: Any = None,
    ) -> dict[str, Any]:
        """Choose the best available tool for mining *block_name*.

        Returns a small dict with ``tool`` (or None), ``tool_type``,
        ``requires_tool``, ``can_mine``, and ``reason``.  For blocks that are
        mineable by hand but benefit from a tool, this still returns a better
        owned tool when one exists.
        """
        req = self.get_tool_requirements(block_name)
        inv_counts = self._normalise_inventory(inventory)
        tool_options = list(req.get("tool_options", []) or [])
        owned_options = [
            opt for opt in tool_options
            if inv_counts.get(str(opt.get("name", "")), 0) > 0
        ]
        owned_options.sort(
            key=lambda opt: (
                float(opt.get("speed", 0.0) or 0.0),
                TOOL_TIERS.get(str(opt.get("tier", "")), 0),
            ),
            reverse=True,
        )

        if owned_options:
            chosen = owned_options[0]
            return {
                "tool": chosen.get("name"),
                "tool_type": req.get("tool_type"),
                "requires_tool": bool(req.get("requires_tool", False)),
                "can_mine": True,
                "reason": "inventory_tool",
                "min_tool": req.get("min_tool"),
                "available_tools": [opt.get("name") for opt in owned_options],
            }

        if req.get("requires_tool", False):
            return {
                "tool": None,
                "tool_type": req.get("tool_type"),
                "requires_tool": True,
                "can_mine": False,
                "reason": "missing_required_tool",
                "min_tool": req.get("min_tool"),
                "available_tools": [],
            }

        return {
            "tool": None,
            "tool_type": req.get("tool_type"),
            "requires_tool": False,
            "can_mine": True,
            "reason": "hand_ok",
            "min_tool": req.get("min_tool"),
            "available_tools": [],
        }

    def can_mine(self, block_name: str, tool_name: str | None = None) -> bool:
        """Check if *block_name* can be mined with *tool_name*.

        If *tool_name* is None, checks if the block can be mined by hand.
        """
        req = self.get_tool_requirements(block_name)
        if not req.get("requires_tool", False):
            return True
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

    @staticmethod
    def _tool_type_from_tool_name(tool_name: str) -> str | None:
        for tool_type in _TOOL_TYPE_PRIORITY:
            if tool_name.endswith(f"_{tool_type}"):
                return tool_type
        return None

    @staticmethod
    def _tool_type_from_material(material: str) -> str | None:
        tokens = [token.strip() for token in material.split(";") if token.strip()]
        for token in tokens:
            if token.startswith("mineable/"):
                tool_type = token.split("/", 1)[1]
                if tool_type in _TOOL_TYPE_PRIORITY:
                    return tool_type
        return None

    def _tool_entries_for_type(self, tool_type: str) -> list[dict[str, Any]]:
        material = self._materials.get(f"mineable/{tool_type}", {})
        entries: list[dict[str, Any]] = []
        for tid_raw in material:
            try:
                tid = int(tid_raw)
            except (TypeError, ValueError):
                continue
            tool_name = self.get_item_name(tid)
            if not tool_name or self._tool_type_from_tool_name(tool_name) != tool_type:
                continue
            tier = self._extract_tool_tier(tool_name)
            entries.append({
                "id": tid,
                "name": tool_name,
                "tier": tier,
                "tier_level": TOOL_TIERS.get(tier, 0),
            })
        entries.sort(key=lambda t: t["tier_level"])
        return entries

    def _tool_type_from_entries(self, tool_entries: list[dict[str, Any]]) -> str | None:
        counts: dict[str, int] = {}
        for entry in tool_entries:
            tool_type = self._tool_type_from_tool_name(str(entry.get("name", "")))
            if tool_type:
                counts[tool_type] = counts.get(tool_type, 0) + 1
        if not counts:
            return None
        return max(
            counts,
            key=lambda tool_type: (counts[tool_type], -_TOOL_TYPE_PRIORITY.index(tool_type)),
        )

    def _tool_speed_for_block_material(self, block_material: str, tool_id: int) -> float:
        speeds: list[float] = []
        for token in [part.strip() for part in block_material.split(";") if part.strip()]:
            material = self._materials.get(token, {})
            if str(tool_id) in material:
                try:
                    speeds.append(float(material[str(tool_id)]))
                except (TypeError, ValueError):
                    continue
        return max(speeds, default=0.0)

    # ═══════════════════════════════════════════════════════════════
    #  Query: Materials
    # ═══════════════════════════════════════════════════════════════

    def get_missing_materials(
        self,
        target_item: str,
        inventory: Any = None,
        quantity: int = 1,
    ) -> dict[str, Any]:
        """Return materials needed to craft *target_item* that are not in *inventory*.

        Returns a dict:
          - have_all: bool — whether everything is available
          - missing: list[str] — missing ingredient names
          - recipe: dict — the first applicable recipe
          - steps: list[str] — human-readable craft steps
        """
        inv_counts = self._normalise_inventory(inventory)
        quantity = max(int(quantity or 1), 1)
        resolved_target = self.resolve_craft_target(target_item, inv_counts)

        recipes = self.get_recipe(resolved_target, inv_counts)
        if not recipes:
            return {
                "have_all": False,
                "missing": ["unknown_recipe"],
                "recipe": None,
                "steps": [f"No recipe found for {target_item}"],
                "target": resolved_target,
            }

        recipe = self._sort_recipes_for_quantity(recipes, inv_counts, quantity)[0]
        result = recipe.get("result", {}) or {}
        output_count = max(int(result.get("count", 1) or 1), 1)
        crafts_needed = math.ceil(quantity / output_count)
        ingredient_counts = recipe.get("ingredient_counts", {}) or {}

        missing_counts: dict[str, int] = {}
        have_counts: dict[str, int] = {}
        for ing, per_craft in ingredient_counts.items():
            needed = int(per_craft) * crafts_needed
            have = self._available_count(inv_counts, ing)
            have_counts[ing] = have
            if have < needed:
                missing_counts[ing] = needed - have

        steps: list[str] = []
        if recipe.get("requires_table"):
            if inv_counts.get("crafting_table", 0) <= 0:
                steps.append("1. Craft or place a crafting table")
                missing_counts["crafting_table"] = max(missing_counts.get("crafting_table", 0), 1)

        if missing_counts:
            missing_text = ", ".join(
                f"{name} x{count}" for name, count in sorted(missing_counts.items())
            )
            steps.append(f"2. Gather or craft missing: {missing_text}")
        else:
            steps.append("2. All materials available")

        steps.append(f"3. Craft {resolved_target}")

        return {
            "target": resolved_target,
            "have_all": len(missing_counts) == 0,
            "missing": sorted(missing_counts.keys()),
            "missing_counts": missing_counts,
            "have_counts": have_counts,
            "ingredient_counts": {
                name: count * crafts_needed
                for name, count in ingredient_counts.items()
            },
            "crafts_needed": crafts_needed,
            "recipe": recipe,
            "steps": steps,
        }

    def get_crafting_plan(
        self,
        target_item: str,
        quantity: int = 1,
        inventory: Any = None,
    ) -> str:
        """Return a readable deterministic crafting plan for the LLM/tool output."""
        inv_counts = self._normalise_inventory(inventory)
        plan = self.get_missing_materials(target_item, inv_counts, quantity)
        recipe = plan.get("recipe")
        target = plan.get("target", self._resolve_item_name(target_item))
        if recipe is None:
            return f"No recipe found for {target_item}."

        result = recipe.get("result", {}) or {}
        result_count = int(result.get("count", 1) or 1)
        ingredient_counts = plan.get("ingredient_counts", {}) or {}
        lines = [
            f"Crafting plan for {target} x{quantity}",
            f"Recipe output: {result.get('name', target)} x{result_count}",
            "Requires crafting table: " + ("yes" if recipe.get("requires_table") else "no"),
            "Ingredients:",
        ]
        for name, needed in sorted(ingredient_counts.items()):
            have = self._available_count(inv_counts, name)
            status = "ok" if have >= needed else f"missing {needed - have}"
            lines.append(f"  - {name} x{needed} (have {have}, {status})")

        missing_counts = plan.get("missing_counts", {}) or {}
        if missing_counts:
            lines.append("Missing:")
            for name, count in sorted(missing_counts.items()):
                lines.append(f"  - {name} x{count}")
        else:
            lines.append("Missing: none")

        lines.append("Steps:")
        for step in plan.get("steps", []):
            lines.append(f"  - {step}")
        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════════════
    #  Query: Collection path
    # ═══════════════════════════════════════════════════════════════

    def get_collection_path(self, target_item: str, inventory: Any = None) -> dict[str, Any]:
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
                    tool_choice = self.select_tool_for_block(ing, inventory)
                    path.append({
                        "action": "mine",
                        "item": ing,
                        "tool": tool_choice.get("tool") or tool_req.get("min_tool"),
                        "tool_type": tool_req.get("tool_type"),
                        "requires_tool": tool_req.get("requires_tool", False),
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
            tool_choice = self.select_tool_for_block(target_item, inventory)
            return {
                "direct": True,
                "path": [{
                    "action": "mine",
                    "item": target_item,
                    "tool": tool_choice.get("tool") or tool_req.get("min_tool"),
                    "tool_type": tool_req.get("tool_type"),
                    "requires_tool": tool_req.get("requires_tool", False),
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
    #  Query: Loot
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _normalise_drops(entry: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not entry:
            return []
        drops = entry.get("drops", [])
        if not isinstance(drops, list):
            return []
        normalised: list[dict[str, Any]] = []
        for drop in drops:
            if not isinstance(drop, dict):
                continue
            item = drop.get("item")
            if not item:
                continue
            normalised.append({
                "item": item,
                "drop_chance": drop.get("dropChance", 1),
                "stack_size_range": drop.get("stackSizeRange", [1, 1]),
                "player_kill": bool(drop.get("playerKill", False)),
            })
        return normalised

    def get_block_drops(self, block_name: str) -> list[dict[str, Any]]:
        """Return known drops for a block."""
        name = self._resolve_item_name(block_name)
        entry = self._block_loot_by_name.get(name)
        if entry is None and name == "short_grass":
            # Older minecraft-data loot tables use the pre-1.20.3 name.
            entry = self._block_loot_by_name.get("grass")
        return self._normalise_drops(entry)

    def get_entity_drops(self, entity_name: str) -> list[dict[str, Any]]:
        """Return known drops for an entity."""
        name = self._resolve_item_name(entity_name)
        return self._normalise_drops(self._entity_loot_by_name.get(name))

    def drops_item(self, source_name: str, item_name: str, *, source: str = "block") -> bool:
        """Return whether a block/entity has a known drop for an item."""
        target = self._resolve_item_name(item_name)
        drops = (
            self.get_entity_drops(source_name)
            if source == "entity"
            else self.get_block_drops(source_name)
        )
        return any(self._resolve_item_name(str(drop.get("item", ""))) == target for drop in drops)

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
    #  Query: Version, features, biomes, materials
    # ═══════════════════════════════════════════════════════════════

    def get_biome_info(self, biome_name: str) -> dict[str, Any] | None:
        """Return biome metadata."""
        name = self._resolve_item_name(biome_name)
        biome_id = self._biomes_by_name.get(name)
        if biome_id is None:
            return None
        return dict(self._biomes.get(biome_id, {}))

    def get_material_tool_speeds(self, material_name: str) -> dict[str, float]:
        """Return tool speed multipliers for a minecraft-data material key."""
        material = self._materials.get(material_name, {})
        result: dict[str, float] = {}
        for item_id, speed in material.items():
            try:
                item_name = self.get_item_name(int(item_id))
                if item_name:
                    result[item_name] = float(speed)
            except (TypeError, ValueError):
                continue
        return result

    def get_feature(self, feature_name: str) -> dict[str, Any] | None:
        """Return version feature metadata by name."""
        feature = self._features.get(feature_name)
        return dict(feature) if feature else None

    @property
    def version(self) -> dict[str, Any]:
        return dict(self._version)

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
