"""Phase 5a executable workflow skill tests."""
from __future__ import annotations

from pathlib import Path

from agent.models import BotState, InventoryItem, Position
from skills.loader import SkillStore, parse_skill_file
from skills.matcher import SkillMatcher
from skills.scope_validator import ScopeValidationError, ScopeValidator


async def test_skill_store_loads_universal_workflow_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bots" / "andy" / "universal" / "skills"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "pickup.md"
    skill_file.write_text(
        "---\n"
        "name: pickup\n"
        "description: Pick up drops\n"
        "scope: bot\n"
        "triggers: [pick up items]\n"
        "confidence: 0.95\n"
        "---\n\n"
        "## 执行步骤\n\n"
        "- !pickupNearbyItems(distance=8)\n"
    )

    store = SkillStore.for_bot("andy", root_dir=tmp_path / "bots")
    await store.load()

    assert len(store.skills) == 1
    assert store.skills[0].steps[0].command == "pickupNearbyItems"
    assert store.skills[0].steps[0].kwargs == {"distance": 8}


async def test_skill_matcher_respects_state_requirements(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bots" / "andy" / "universal" / "skills"
    skill_dir.mkdir(parents=True)
    (skill_dir / "surface.md").write_text(
        "---\n"
        "name: go_to_surface\n"
        "description: Return to the surface\n"
        "scope: bot\n"
        "triggers: [go to surface]\n"
        "requires: [pos.y < 63]\n"
        "confidence: 0.95\n"
        "---\n\n"
        "## 执行步骤\n\n"
        "- !goToSurface()\n"
    )
    store = SkillStore.for_bot("andy", root_dir=tmp_path / "bots")
    await store.load()
    matcher = SkillMatcher(store)

    underground = BotState(position=Position(y=12))
    surface = BotState(position=Position(y=70))

    assert await matcher.match("go to surface", underground) is not None
    assert await matcher.match("go to surface", surface) is None


def test_scope_validator_rejects_coordinates_in_bot_scope(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text(
        "---\n"
        "name: bad_home\n"
        "scope: bot\n"
        "triggers: [go home]\n"
        "---\n\n"
        "## 执行步骤\n\n"
        "- !goToCoordinates(x=10, y=64, z=-3)\n"
    )
    skill = parse_skill_file(path)
    assert skill is not None

    try:
        ScopeValidator().validate(skill)
    except ScopeValidationError:
        return
    raise AssertionError("expected ScopeValidationError")


async def test_skill_store_records_outcome_to_frontmatter(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bots" / "andy" / "universal" / "skills"
    skill_dir.mkdir(parents=True)
    path = skill_dir / "eat.md"
    path.write_text(
        "---\n"
        "name: eat_bread\n"
        "scope: bot\n"
        "triggers: [eat bread]\n"
        "requires: [inventory has bread]\n"
        "confidence: 0.9\n"
        "success_count: 0\n"
        "fail_count: 0\n"
        "---\n\n"
        "## 执行步骤\n\n"
        "- !consume(item_name=\"bread\")\n"
    )
    store = SkillStore.for_bot("andy", root_dir=tmp_path / "bots")
    await store.load()
    await store.record_outcome(store.skills[0], success=True)

    raw = path.read_text()
    assert "success_count: 1" in raw
    assert "fail_count: 0" in raw


async def test_matcher_supports_inventory_conditions(tmp_path: Path) -> None:
    skill_dir = tmp_path / "bots" / "andy" / "universal" / "skills"
    skill_dir.mkdir(parents=True)
    (skill_dir / "eat.md").write_text(
        "---\n"
        "name: eat_bread\n"
        "scope: bot\n"
        "triggers: [eat bread]\n"
        "requires: [food < 18, inventory has bread]\n"
        "confidence: 0.95\n"
        "---\n\n"
        "## 执行步骤\n\n"
        "- !consume(item_name=\"bread\")\n"
    )
    store = SkillStore.for_bot("andy", root_dir=tmp_path / "bots")
    await store.load()
    matcher = SkillMatcher(store)

    hungry = BotState(food=10, inventory=[InventoryItem(name="bread", count=1)])
    no_bread = BotState(food=10, inventory=[])

    assert await matcher.match("eat bread", hungry) is not None
    assert await matcher.match("eat bread", no_bread) is None
