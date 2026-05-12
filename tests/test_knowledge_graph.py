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


async def test_bed_target_prefers_existing_wool_color() -> None:
    kg = await _loaded_graph()

    assert kg.resolve_craft_target("bed", {"red_wool": 3}) == "red_bed"
    assert kg.resolve_craft_target("bed", {"white_wool": 3}) == "white_bed"
