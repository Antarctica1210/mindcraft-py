"""Unit tests for agent loop protocol helpers."""
from __future__ import annotations

from types import SimpleNamespace

from agent.action_result import ActionResult
from agent.command_registry import (
    command_defs,
    command_meta,
    world_change_command_can_complete,
    world_change_command_has_intrinsic_effect,
)
from agent.command_runner import CommandRunner
from agent.completion import (
    CompletionEngine,
    CompletionSpec,
    completion_spec_satisfied,
    task_effect_count,
)
from agent.effects import ActionEffect, action_effects_from_result
from agent.failures import classify_failure, is_hard_failure, parse_missing_counts
from agent.models import BotState, InventoryItem
from agent.planner_loop import PlannerLoop
from agent.recovery import RecoveryManager
from agent.task_goal_parser import completion_spec_from_message, parse_craft_request
from agent.task_manager import ActiveTaskEvent, TaskManager


def test_action_result_wraps_legacy_success_with_effects() -> None:
    result = ActionResult.from_legacy(
        "givePlayer",
        "Gave 2x iron_pickaxe to Wilson.",
    )

    assert result.ok is True
    assert result.command == "givePlayer"
    assert result.summary == "Done: gave 2x iron_pickaxe to Wilson."
    assert result.detail == "Gave 2x iron_pickaxe to Wilson."
    assert result.effects == (
        ActionEffect(
            type="delivery",
            name="iron_pickaxe",
            count=2,
            item="iron_pickaxe",
        ),
    )
    assert result.failure is None


def test_action_result_classifies_missing_tool_failure() -> None:
    result = ActionResult.from_legacy(
        "collectBlocks",
        "FAILED: Need a stone_pickaxe to mine iron_ore. Craft a stone_pickaxe first.",
    )

    assert result.ok is False
    assert result.summary == "Failed: need stone_pickaxe for iron_ore."
    assert result.failure is not None
    assert result.failure.kind == "missing_tool"
    assert result.failure.required == "stone_pickaxe"
    assert result.failure.target == "iron_ore"


def test_failure_classifier_parses_missing_material_counts() -> None:
    assert parse_missing_counts("stick x2, cobblestone x3, stick x1") == {
        "stick": 3,
        "cobblestone": 3,
    }

    cause = classify_failure("FAILED: Missing materials for torch: stick x2, coal x1.")

    assert cause is not None
    assert cause.kind == "missing_materials"
    assert cause.target == "torch"
    assert cause.missing == {"stick": 2, "coal": 1}


def test_failure_classifier_distinguishes_timeout_from_bridge_failure() -> None:
    timeout = classify_failure("FAILED: Request 'collect_blocks' timed out after 120s")
    generic_timeout = classify_failure("FAILED: The operation timed out while moving")
    bridge = classify_failure("FAILED: Socket server read failed: Connection reset by peer")

    assert timeout is not None
    assert timeout.kind == "path_timeout"
    assert timeout.target == "collect_blocks"
    assert generic_timeout is not None
    assert generic_timeout.kind == "path_timeout"
    assert is_hard_failure(generic_timeout.message) is False
    assert bridge is not None
    assert bridge.kind == "bridge_failure"
    assert is_hard_failure(bridge.message) is True


def test_failure_classifier_treats_no_breakable_as_nearby_miss() -> None:
    cause = classify_failure("FAILED: No breakable tall_grass within 16 blocks.")

    assert cause is not None
    assert cause.kind == "nearby_miss"
    assert cause.target == "tall_grass"


def test_effect_extractor_counts_planting_without_task_specific_code() -> None:
    effects = action_effects_from_result(
        "tillAndSow",
        "tilled dirt and planted wheat_seeds",
    )
    task = SimpleNamespace(
        completion_spec=CompletionSpec(kind="planting", quantity=2),
        effects=effects + effects,
        events=[],
    )
    state = BotState(inventory=[])

    assert completion_spec_satisfied(task, state) is True


def test_task_effect_count_prefers_structured_effects_over_legacy_events() -> None:
    task = SimpleNamespace(
        effects=[
            ActionEffect(type="world_change", name="planted_crop", count=1),
            ActionEffect(type="world_change", name="planted_crop", count=1),
        ],
        events=[
            ActiveTaskEvent(
                kind="action_result",
                message="tillAndSow args=[0, 62, 335] -> Tilled and planted wheat_seeds.",
                ok=True,
            ),
            ActiveTaskEvent(
                kind="action_result",
                message="tillAndSow args=[1, 62, 335] -> Tilled and planted wheat_seeds.",
                ok=True,
            ),
        ],
    )

    assert task_effect_count(
        task,
        effect_type="world_change",
        names=("planted_crop",),
    ) == 2


def test_task_effect_count_keeps_legacy_planting_event_fallback() -> None:
    task = SimpleNamespace(
        effects=[],
        events=[
            ActiveTaskEvent(
                kind="action_result",
                message="tillAndSow args=[0, 62, 335] -> Tilled and planted wheat_seeds.",
                ok=True,
            ),
            ActiveTaskEvent(
                kind="action_result",
                message="tillAndSow args=[1, 62, 335] -> Tilled and planted wheat_seeds.",
                ok=True,
            ),
        ],
    )

    assert task_effect_count(
        task,
        effect_type="world_change",
        names=("planted_crop",),
    ) == 2


def test_completion_requires_inventory_delta_from_baseline() -> None:
    task = SimpleNamespace(
        completion_spec=CompletionSpec(
            kind="craft_item",
            acceptable_items=("torch",),
            quantity=8,
        ),
        baseline_inventory={"torch": 4},
        effects=[],
        events=[],
    )
    partial = BotState(inventory=[InventoryItem(name="torch", count=8)])
    complete = BotState(inventory=[InventoryItem(name="torch", count=12)])

    assert completion_spec_satisfied(task, partial) is False
    assert completion_spec_satisfied(task, complete) is True


def test_completion_engine_reports_progress_and_finish_decision() -> None:
    engine = CompletionEngine()
    task = SimpleNamespace(
        completion_spec=CompletionSpec(kind="planting", quantity=3),
        effects=[
            ActionEffect(type="world_change", name="planted_crop", count=1),
            ActionEffect(type="world_change", name="planted_crop", count=1),
        ],
        events=[],
    )
    state = BotState(inventory=[])
    action_result = ActionResult.from_legacy("tillAndSow", "tilled and planted wheat")

    progress = engine.progress(task, state)
    decision = engine.should_finish_after_action(task, action_result, state)

    assert progress.kind == "planting"
    assert progress.current == 2
    assert progress.required == 3
    assert progress.satisfied is False
    assert decision.should_finish is False
    assert decision.status == "running"


def test_command_meta_centralizes_terminal_and_navigation_policy() -> None:
    assert command_meta("tillAndSow").completion_kind == "world_change"
    assert command_meta("tillAndSow").terminal_by_default is True
    assert command_meta("searchForBlock").completion_kind == "navigation"
    assert command_meta("searchForBlock").allowed_in_navigation_task is False
    assert command_meta("inventory").allowed_in_navigation_task is True
    assert command_meta("craftRecipe").inventory_prep is True
    assert command_meta("craftRecipe").requires_pre_action_observe is True
    assert "missing_materials" in command_meta("craftRecipe").preflight_hooks
    assert "knowledge_tool_requirement" in command_meta("collectBlocks").preflight_hooks
    assert command_meta("goToPlayer").direct_shortcut is True


def test_command_registry_owns_command_docs() -> None:
    defs = command_defs()

    assert "craftRecipe" in defs
    assert defs["craftRecipe"].completion_kind == "inventory"
    assert "Always use getCraftingPlan" in defs["craftRecipe"].description
    assert defs["breedAnimals"].params["breeding_item"]["description"]


async def test_command_runner_wraps_legacy_executor_output() -> None:
    calls: list[tuple[str, list[object], dict[str, object] | None]] = []

    async def executor(
        command_name: str,
        args: list[object],
        kwargs: dict[str, object] | None,
    ) -> str:
        calls.append((command_name, args, kwargs))
        return "Gave 2x iron_pickaxe to Wilson."

    run = await CommandRunner(executor).run(
        "givePlayer",
        ["Wilson", "iron_pickaxe", 2],
        None,
    )

    assert calls == [("givePlayer", ["Wilson", "iron_pickaxe", 2], {})]
    assert run.raw == "Gave 2x iron_pickaxe to Wilson."
    assert run.result.ok is True
    assert run.result.summary == "Done: gave 2x iron_pickaxe to Wilson."
    assert run.result.effects[0].type == "delivery"
    assert run.elapsed_ms >= 0


def test_world_change_completion_policy_lives_in_command_registry() -> None:
    assert world_change_command_can_complete("tillAndSow", "种1块田") is True
    assert world_change_command_can_complete("breakNearbyBlocks", "种16块田") is False
    assert world_change_command_can_complete("breakNearbyBlocks", "清理草") is True
    assert world_change_command_can_complete("digDown", "帮我做两把铁镐") is False
    assert world_change_command_has_intrinsic_effect("tillAndSow") is True
    assert world_change_command_has_intrinsic_effect("digDown") is False


def test_task_goal_parser_uses_data_aliases_for_chinese_requests() -> None:
    craft = parse_craft_request("帮我做两把铁镐")
    planting = completion_spec_from_message("种16块田")

    assert craft is not None
    assert craft.item_name == "iron_pickaxe"
    assert craft.quantity == 2
    assert planting.kind == "planting"
    assert planting.quantity == 16
    assert planting.requires_delivery is False


def test_recovery_manager_plans_tool_and_material_repairs() -> None:
    manager = RecoveryManager()
    state = BotState(
        inventory=[
            InventoryItem(name="oak_log", count=1),
            InventoryItem(name="cobblestone", count=12),
        ],
        nearby_block_types=[],
    )
    tool_cause = classify_failure("FAILED: Need a stone_pickaxe to mine iron_ore.")
    material_cause = classify_failure("FAILED: Missing materials for torch: stick x2.")

    tool_plan = manager.plan(
        command_name="collectBlocks",
        cause=tool_cause,
        state=state,
    )
    material_plan = manager.plan(
        command_name="craftRecipe",
        cause=material_cause,
        state=state,
    )

    assert tool_plan is not None
    assert tool_plan.subgoal_kind == "missing_tool"
    assert tool_plan.subgoal_message == "Craft stone_pickaxe for iron_ore"
    assert [(step.command, step.kwargs_dict()) for step in tool_plan.steps] == [
        ("craftRecipe", {"recipe_name": "oak_planks", "num": 1}),
        ("craftRecipe", {"recipe_name": "crafting_table", "num": 1}),
        ("craftRecipe", {"recipe_name": "oak_planks", "num": 1}),
        ("craftRecipe", {"recipe_name": "stick", "num": 1}),
        ("craftRecipe", {"recipe_name": "stone_pickaxe", "num": 1}),
    ]
    assert material_plan is not None
    assert material_plan.subgoal_kind == "missing_materials"
    assert material_plan.subgoal_message == "Prepare stick x2 for torch"
    assert [(step.command, step.kwargs_dict()) for step in material_plan.steps] == [
        ("craftRecipe", {"recipe_name": "oak_planks", "num": 1}),
        ("craftRecipe", {"recipe_name": "stick", "num": 1}),
    ]


def test_recovery_manager_limits_dependency_subgoals_to_simple_repairs() -> None:
    manager = RecoveryManager()
    state = BotState(
        inventory=[
            InventoryItem(name="oak_log", count=1),
            InventoryItem(name="cobblestone", count=12),
        ],
        nearby_block_types=[],
    )
    hoe_cause = classify_failure("FAILED: Need a stone_hoe to mine farmland.")
    coal_cause = classify_failure("FAILED: Missing materials for torch: coal x1.")
    furnace_cause = classify_failure("FAILED: Missing materials for smeltItem: furnace x1.")

    assert manager.plan(command_name="collectBlocks", cause=hoe_cause, state=state) is None
    assert manager.plan(command_name="craftRecipe", cause=coal_cause, state=state) is None

    furnace_plan = manager.plan(command_name="smeltItem", cause=furnace_cause, state=state)

    assert furnace_plan is not None
    assert furnace_plan.subgoal_kind == "missing_materials"
    assert furnace_plan.subgoal_message == "Prepare furnace x1 for smeltitem"


def test_recovery_manager_plans_timeout_as_replan_without_retry() -> None:
    manager = RecoveryManager()
    state = BotState(inventory=[], nearby_block_types=[])
    cause = classify_failure("FAILED: Request 'collect_blocks' timed out after 120s")
    bridge_cause = classify_failure("FAILED: Not connected to socket server")

    plan = manager.plan(
        command_name="collectBlocks",
        cause=cause,
        state=state,
    )

    assert plan is not None
    assert plan.cause.kind == "path_timeout"
    assert plan.steps == ()
    assert plan.retry_original is False
    assert "replan" in plan.reason
    assert manager.plan(command_name="collectBlocks", cause=bridge_cause, state=state) is None


def test_task_manager_records_baseline_and_completion_spec() -> None:
    manager = TaskManager()
    state = BotState(
        inventory=[InventoryItem(name="iron_pickaxe", count=1)],
        nearby_block_types=["crafting_table"],
    )

    task = manager.start(
        requester="Wilson",
        message="帮我做两把铁镐",
        baseline_state=state,
        history_len=1,
        planning_strategy="react",
    )

    assert task.baseline_inventory == {"iron_pickaxe": 1}
    assert task.completion_spec is not None
    assert task.completion_spec.kind == "craft_item"
    assert task.completion_spec.quantity == 2
    assert task.snapshots
    assert manager.active is task
    manager.clear()
    assert manager.active is None


def test_task_manager_pushes_and_pops_internal_subgoal() -> None:
    manager = TaskManager()
    state = BotState(
        inventory=[InventoryItem(name="oak_log", count=1)],
        nearby_block_types=[],
    )
    parent = manager.start(
        requester="Wilson",
        message="挖8铁矿",
        baseline_state=state,
        history_len=1,
        planning_strategy="react",
    )

    subgoal = manager.push_subgoal(
        requester="Wilson",
        message="Craft stone_pickaxe for iron_ore",
        baseline_state=state,
        history_len=2,
        planning_strategy="react",
        parent_id=parent.id,
        reason="Missing stone_pickaxe for iron_ore",
        resume_command="collectBlocks",
        resume_args=["iron_ore", 8],
    )

    assert manager.root is parent
    assert manager.active is subgoal
    assert len(manager.stack) == 2
    assert subgoal.task_type == "subgoal"
    assert subgoal.parent_id == parent.id
    assert subgoal.resume_command == "collectBlocks"
    assert subgoal.resume_args == ["iron_ore", 8]

    popped = manager.pop_active(status="succeeded", summary="crafted stone_pickaxe")

    assert popped is subgoal
    assert manager.active is parent
    assert len(manager.stack) == 1


async def test_planner_loop_runs_no_tool_turn_on_host_runtime() -> None:
    class Host:
        def __init__(self) -> None:
            self._turn_id = 7
            self._running = True
            self._shut_up = False
            self._safety = None
            self._active_task = None
            self._modes = SimpleNamespace(flush_behavior_log=lambda: "")
            self._history = []
            self._convo_mgr = SimpleNamespace(compress_history=lambda history: history)
            self.plan_calls: list[tuple[str, int]] = []
            self.chats: list[str] = []

        async def observe(self) -> BotState:
            return BotState(inventory=[])

        async def plan(self, _state: BotState, *, tool_choice: str):
            self.plan_calls.append((tool_choice, len(self._history)))
            return SimpleNamespace(content="hello there", tool_calls=[])

        async def _send_chat(self, message: str) -> None:
            self.chats.append(message)

    host = Host()
    result = await PlannerLoop(host).run(max_responses=1, use_tools=False, turn_id=7)

    assert result is False
    assert host.plan_calls == [("none", 0)]
    assert host.chats == ["hello there"]
