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
