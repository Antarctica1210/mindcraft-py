"""Tests for static Minecraft knowledge planning."""
from __future__ import annotations

from knowledge.knowledge_graph import KnowledgeGraph


async def _loaded_graph() -> KnowledgeGraph:
    kg = KnowledgeGraph("data/minecraft/1.21.6")
    await kg.load()
    return kg


async def test_bed_plan_uses_inventory_matching_planks_and_wool() -> None:
    kg = await _loaded_graph()
    inventory = {
        "oak_planks": 3,
        "white_wool": 3,
        "crafting_table": 1,
    }

    missing = kg.get_missing_materials("bed", inventory=inventory)

    assert missing["target"] == "white_bed"
    assert missing["have_all"] is True
    assert missing["ingredient_counts"] == {
        "oak_planks": 3,
        "white_wool": 3,
    }
    assert missing["recipe"]["requires_table"] is True
    assert missing["missing_counts"] == {}


async def test_bed_plan_requires_matching_wool_color() -> None:
    kg = await _loaded_graph()
    inventory = {
        "oak_planks": 3,
        "white_wool": 2,
        "light_gray_wool": 2,
        "crafting_table": 1,
    }

    missing = kg.get_missing_materials("bed", inventory=inventory)

    assert missing["target"] == "white_bed"
    assert missing["have_all"] is False
    assert missing["missing_counts"] == {"white_wool": 1}


async def test_bed_plan_reports_crafting_table_when_shape_exceeds_2x2() -> None:
    kg = await _loaded_graph()
    inventory = {"oak_planks": 3, "white_wool": 3}

    plan = kg.get_crafting_plan("bed", inventory=inventory)

    assert "Crafting plan for white_bed x1" in plan
    assert "Requires crafting table: yes" in plan
    assert "oak_planks x3" in plan
    assert "white_wool x3" in plan
    assert "crafting_table x1" in plan


async def test_crafting_table_plan_counts_four_planks() -> None:
    kg = await _loaded_graph()

    missing = kg.get_missing_materials(
        "crafting_table",
        inventory={"oak_planks": 3},
    )

    assert missing["target"] == "crafting_table"
    assert missing["ingredient_counts"] == {"oak_planks": 4}
    assert missing["missing_counts"] == {"oak_planks": 1}


async def test_recipe_empty_placeholders_are_not_materials() -> None:
    kg = await _loaded_graph()

    pickaxe = kg.get_missing_materials(
        "wooden_pickaxe",
        inventory={"oak_planks": 3, "stick": 2, "crafting_table": 1},
    )
    furnace = kg.get_missing_materials(
        "furnace",
        inventory={"cobblestone": 8, "crafting_table": 1},
    )

    assert pickaxe["ingredient_counts"] == {"oak_planks": 3, "stick": 2}
    assert pickaxe["missing_counts"] == {}
    assert pickaxe["recipe"]["requires_table"] is True
    assert furnace["ingredient_counts"] == {"cobblestone": 8}
    assert furnace["missing_counts"] == {}
    assert "unknown_null" not in kg.get_crafting_plan("wooden_pickaxe", inventory={})
    assert "unknown_null" not in kg.get_crafting_plan("furnace", inventory={})


async def test_known_non_craftable_item_is_not_fuzzy_matched_to_recipe() -> None:
    kg = await _loaded_graph()

    plan = kg.get_crafting_plan("cooked_porkchop", quantity=3, inventory={})

    assert plan == "No recipe found for cooked_porkchop."
    assert "decorated_pot" not in plan


async def test_loot_tables_answer_block_and_entity_drops() -> None:
    kg = await _loaded_graph()

    tall_grass_drops = kg.get_block_drops("tall_grass")
    chicken_drops = kg.get_entity_drops("chicken")

    assert any(drop["item"] == "wheat_seeds" for drop in tall_grass_drops)
    assert any(drop["item"] == "feather" for drop in chicken_drops)
    assert kg.drops_item("tall_grass", "wheat_seeds")
    assert not kg.drops_item("chicken", "wheat_seeds", source="entity")


async def test_extra_minecraft_data_files_are_loaded() -> None:
    kg = await _loaded_graph()

    assert kg.version["minecraftVersion"] == "1.21.6"
    assert kg.get_biome_info("cherry_grove")["dimension"] == "overworld"
    assert "iron_pickaxe" in kg.get_material_tool_speeds("mineable/pickaxe")
    assert kg.get_feature("blockStateId")["versions"][-1] == "latest"


async def test_tool_requirements_use_block_material_tool_family() -> None:
    kg = await _loaded_graph()

    oak_log = kg.get_tool_requirements("oak_log")
    dirt = kg.get_tool_requirements("dirt")
    hay_block = kg.get_tool_requirements("hay_block")
    stone = kg.get_tool_requirements("stone")

    assert oak_log["tool_type"] == "axe"
    assert oak_log["min_tool"] == "wooden_axe"
    assert oak_log["requires_tool"] is False
    assert dirt["tool_type"] == "shovel"
    assert dirt["min_tool"] == "wooden_shovel"
    assert dirt["requires_tool"] is False
    assert hay_block["tool_type"] == "hoe"
    assert hay_block["min_tool"] == "wooden_hoe"
    assert stone["tool_type"] == "pickaxe"
    assert stone["min_tool"] == "wooden_pickaxe"
    assert stone["requires_tool"] is True
    assert "iron_axe" in oak_log["tool_names"]
    assert "diamond_shovel" in dirt["tool_names"]


async def test_can_mine_distinguishes_required_tools_from_preferred_tools() -> None:
    kg = await _loaded_graph()

    assert kg.can_mine("dirt") is True
    assert kg.can_mine("oak_log") is True
    assert kg.can_mine("stone") is False
    assert kg.can_mine("stone", "wooden_pickaxe") is True
    assert kg.can_mine("stone", "wooden_axe") is False


async def test_select_tool_for_block_prefers_best_owned_matching_tool() -> None:
    kg = await _loaded_graph()

    axe = kg.select_tool_for_block(
        "oak_log",
        inventory={"wooden_axe": 1, "iron_axe": 1, "stone_pickaxe": 1},
    )
    shovel = kg.select_tool_for_block(
        "dirt",
        inventory={"wooden_shovel": 1, "diamond_shovel": 1, "iron_axe": 1},
    )
    pickaxe = kg.select_tool_for_block(
        "stone",
        inventory={"iron_pickaxe": 1, "wooden_pickaxe": 1, "diamond_axe": 1},
    )
    missing = kg.select_tool_for_block("stone", inventory={"iron_axe": 1})

    assert axe["tool"] == "iron_axe"
    assert axe["can_mine"] is True
    assert shovel["tool"] == "diamond_shovel"
    assert pickaxe["tool"] == "iron_pickaxe"
    assert missing["tool"] is None
    assert missing["can_mine"] is False
    assert missing["min_tool"] == "wooden_pickaxe"


async def test_collection_path_uses_owned_higher_tier_tool_when_available() -> None:
    kg = await _loaded_graph()

    log_path = kg.get_collection_path("oak_log", inventory={"iron_axe": 1})
    stone_path = kg.get_collection_path("stone", inventory={"diamond_pickaxe": 1})

    assert log_path["path"][0]["tool"] == "iron_axe"
    assert log_path["path"][0]["tool_type"] == "axe"
    assert stone_path["path"][0]["tool"] == "diamond_pickaxe"
    assert stone_path["path"][0]["requires_tool"] is True


async def test_bed_target_prefers_existing_wool_color() -> None:
    kg = await _loaded_graph()

    assert kg.resolve_craft_target("bed", {"red_wool": 3}) == "red_bed"
    assert kg.resolve_craft_target("bed", {"white_wool": 3}) == "white_bed"


async def test_bed_plan_prefers_direct_wool_recipe_over_recoloring() -> None:
    kg = await _loaded_graph()
    inventory = {
        "light_gray_wool": 1,
        "mutton": 1,
        "oak_log": 5,
        "oak_sapling": 2,
    }

    plan = kg.get_missing_materials("bed", inventory=inventory)

    assert plan["target"] == "light_gray_bed"
    assert plan["ingredient_counts"] == {
        "light_gray_wool": 3,
        "oak_planks": 3,
    }
    assert "light_gray_dye" not in plan["missing_counts"]
    assert "black_bed" not in plan["missing_counts"]


async def test_torch_plan_prefers_coal_recipe_for_requested_quantity() -> None:
    kg = await _loaded_graph()
    inventory = {
        "coal": 32,
        "charcoal": 2,
        "stick": 10,
    }

    plan = kg.get_missing_materials("torch", inventory=inventory, quantity=32)

    assert plan["target"] == "torch"
    assert plan["have_all"] is True
    assert plan["ingredient_counts"] == {"coal": 8, "stick": 8}
    assert plan["missing_counts"] == {}


async def test_torch_crafting_plan_does_not_report_missing_charcoal_when_coal_exists() -> None:
    kg = await _loaded_graph()

    plan = kg.get_crafting_plan(
        "torch",
        inventory={"coal": 32, "charcoal": 2, "stick": 10},
        quantity=32,
    )

    assert "coal x8 (have 32, ok)" in plan
    assert "charcoal x8" not in plan
    assert "Missing: none" in plan
