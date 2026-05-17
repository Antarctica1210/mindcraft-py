"""Focused tests for agent-owned runtime commands and behavior modes."""
from __future__ import annotations

from typing import Any

from conftest import FakeBridge, SmartBridge

from agent.core import Agent, _command_defs, _compact_chat_text
from agent.models import AgentConfig, BotState, EntityInfo, Position
from agent.modes import BehaviorMode, ModeEvaluator
from llm.dispatch_gate import DispatchDecision
from llm.message_builder import ConversationTurn, MessageBuilder
from llm.prompter import LLMInput, LLMOutput, ToolCallFunction, ToolCallOutput
from memory.models import MemoryQuery, MemoryRecord, MemoryResult, MemoryType, Scope


class FakePrompter:
    model = "fake"

    async def send_request(self, _inp: LLMInput) -> LLMOutput:
        return LLMOutput(
            content="```js\nlog('hello from generated code')\nreturn 'ok'\n```",
            model="fake",
        )


class SummaryPrompter:
    model = "fake-summary"

    async def send_request(self, _inp: LLMInput) -> LLMOutput:
        return LLMOutput(content="Task summary from LLM.", model="fake-summary")


class FakeTaskMemory:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.records: list[Any] = []

    async def record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))

    async def remember(self, record: Any, *, is_player_requested: bool = False) -> str:
        self.records.append(record)
        return record.name

    async def recall(self, query: MemoryQuery) -> list[MemoryResult]:
        return []


class FakeRecallMemory(FakeTaskMemory):
    def __init__(self, records: list[MemoryRecord]) -> None:
        super().__init__()
        self.recall_queries: list[MemoryQuery] = []
        self._records = records

    async def recall(self, query: MemoryQuery) -> list[MemoryResult]:
        self.recall_queries.append(query)
        return [
            MemoryResult(record=record, score=1.0, matched_on="test")
            for record in self._records
        ]


def test_all_commands_have_completion_kind() -> None:
    defs = _command_defs()

    assert defs
    assert all(cdef.completion_kind != "none" for cdef in defs.values())
    assert defs["breedAnimals"].completion_kind == "world_change"
    assert defs["givePlayer"].completion_kind == "delivery"


def test_compact_chat_text_summarizes_verbose_done_result() -> None:
    verbose = (
        "Done: search_for_block tall_grass at (246, 63, 322): goal reached.\n"
        "Position: (246.4, 63.0, 322.5) -> (246.4, 63.0, 322.5).\n"
        "Inventory: no inventory change.\n"
        "Output:\n"
        "[2026-05-14T08:43:22.746Z] search_for_block tall_grass started"
    )

    assert _compact_chat_text(verbose) == (
        "Done: search_for_block tall_grass at (246, 63, 322): goal reached."
    )


def test_compact_chat_text_summarizes_missing_dependency() -> None:
    verbose = (
        "Done: FAILED: Need a stone_pickaxe to mine iron_ore. "
        "You have: oak_planks x4, cobblestone x12. "
        "Craft a stone_pickaxe first, then retry.\n"
        "HINT: Use getCraftingPlan to check requirements."
    )

    assert _compact_chat_text(verbose) == "Failed: need stone_pickaxe for iron_ore."


async def test_set_goal_starts_and_end_goal_stops_self_prompt() -> None:
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._running = True

    result = await agent._execute_agent_command("setGoal", ["collect wood"])
    assert result == "Goal set: collect wood. Self-prompting is active."
    assert agent._self_prompt_goal == "collect wood"
    assert agent._self_prompt_task is not None

    result = await agent._execute_agent_command("endGoal", [])
    assert result == "Goal ended. Self-prompting stopped."
    assert agent._self_prompt_goal is None
    assert agent._self_prompt_task is None


async def test_set_mode_updates_mode_evaluator_state() -> None:
    agent = Agent(AgentConfig(name="bot", max_commands=1))

    result = await agent._execute_agent_command("setMode", ["hunting", "true"])
    assert result == "Mode 'hunting' enabled."
    assert "hunting:on" in agent._modes.status_text()


async def test_new_action_generates_code_and_executes_bridge() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._prompter = FakePrompter()  # type: ignore[assignment]
    agent._msg_builder = MessageBuilder(bot_name="bot")

    result = await agent._execute_agent_command("newAction", ["say hello with code"])

    assert result == "Dynamic action ok:\nran code"
    calls = bridge.action_calls()
    assert calls
    assert calls[-1][0] == "execute_code"
    assert "hello from generated code" in calls[-1][1]


async def test_new_action_reports_bridge_execution_failure() -> None:
    class FailingExecuteBridge(FakeBridge):
        async def execute_code(self, code: str, timeout_ms: int = 60000) -> dict[str, Any]:
            self.calls.append(("execute_code", code, timeout_ms))
            raise RuntimeError("Socket server closed connection")

    bridge = FailingExecuteBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._prompter = FakePrompter()  # type: ignore[assignment]
    agent._msg_builder = MessageBuilder(bot_name="bot")

    result = await agent._execute_agent_command("newAction", ["say hello with code"])

    assert result == "FAILED: Dynamic action failed: Socket server closed connection"


async def test_player_command_preserves_sender_casing() -> None:
    bridge = FakeBridge()

    async def go_to_player(player_name: str, closeness: float = 3) -> str:
        bridge.calls.append(("go_to_player", player_name, closeness))
        return f"went to {player_name}"

    bridge.go_to_player = go_to_player  # type: ignore[method-assign]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._last_sender = "Wilson"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("goToPlayer", ["wilson"])

    assert result == "went to Wilson"
    assert ("go_to_player", "Wilson", 3) in bridge.calls


async def test_failed_active_task_reports_after_returning_to_player() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    await agent._replace_active_task("Wilson", "挖8铁矿")
    await agent._finish_active_task("failed", reason="Need stone_pickaxe")

    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert any(
        chat.startswith("Task failed:") and "Need stone_pickaxe" in chat
        and "accept a new command" in chat
        for chat in bridge.chats
    )
    assert agent._active_task is None


async def test_successful_active_task_reports_ready_for_next_command() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    await agent._replace_active_task("Wilson", "种16块田")
    await agent._finish_active_task("succeeded", reason="Planted requested fields")

    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert any(
        chat.startswith("Task complete:") and "accept a new command" in chat
        for chat in bridge.chats
    )
    assert agent._active_task is None


async def test_busy_task_rejects_new_regular_command_without_polluting_task() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._last_message = "种16块田"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "种16块田")
    active_id = agent._active_task.id

    async def fail_plan(*_args: object, **_kwargs: object) -> LLMOutput:
        raise AssertionError("LLM should not be called while task stack is busy")

    agent.plan = fail_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "帮我做两把铁镐")

    assert used_command is False
    assert agent._active_task is not None
    assert agent._active_task.id == active_id
    assert agent._last_message == "种16块田"
    assert not any(call[0] == "craft_recipe" for call in bridge.calls)
    assert any("still working on the current task" in chat for chat in bridge.chats)


async def test_busy_task_allows_come_here_to_interrupt() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._last_message = "种16块田"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "种16块田")

    async def fail_plan(*_args: object, **_kwargs: object) -> LLMOutput:
        raise AssertionError("LLM should not be called for interrupt come here")

    agent.plan = fail_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "come here")

    assert used_command is True
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert agent._active_task is None


async def test_active_task_prompt_history_starts_at_current_user_request() -> None:
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._history = [
        ConversationTurn(role="user", content="Wilson: 造8个火把"),
        ConversationTurn(role="assistant", content='!craftRecipe("torch", 8)'),
        ConversationTurn(role="system", content="FAILED: Missing stick"),
        ConversationTurn(role="user", content="Wilson: 跟我来"),
    ]

    await agent._replace_active_task("Wilson", "跟我来")

    scoped = agent._history_for_active_task_prompt()

    assert [turn.content for turn in scoped] == ["Wilson: 跟我来"]


async def test_navigation_task_blocks_crafting_bleed_from_previous_task() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=2))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "跟我来"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    agent._history = [
        ConversationTurn(role="user", content="Wilson: 造8个火把"),
        ConversationTurn(role="assistant", content='!craftRecipe("torch", 8)'),
        ConversationTurn(role="system", content="FAILED: Missing stick"),
        ConversationTurn(role="user", content="Wilson: 跟我来"),
    ]
    await agent._replace_active_task("Wilson", "跟我来")

    async def no_direct_shortcut() -> None:
        return None

    agent._try_direct_action_command = no_direct_shortcut  # type: ignore[method-assign]
    responses = [
        LLMOutput(content='!craftRecipe("oak_planks", 1)', model="fake"),
        LLMOutput(content='!goToPlayer("Wilson", 3)', model="fake"),
    ]

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        assert tool_choice == "auto"
        return responses.pop(0)

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent._run_plan_loop(max_responses=2, use_tools=True)

    assert used_command is True
    assert not any(call[0] == "craft_recipe" for call in bridge.calls)
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert agent._active_task is None
    assert any(
        turn.role == "system"
        and "Ignored craftRecipe because the current task is only" in turn.content
        for turn in agent._history
    )


async def test_active_task_completion_spec_for_iron_mining() -> None:
    agent = Agent(AgentConfig(name="bot", max_commands=1))

    await agent._replace_active_task("Wilson", "挖8铁矿")

    assert agent._active_task is not None
    spec = agent._active_task.completion_spec
    assert spec is not None
    assert spec.kind == "collect_item"
    assert spec.target_items == ("raw_iron",)
    assert "raw_iron" in spec.acceptable_items
    assert "iron_ore" in spec.acceptable_items
    assert spec.quantity == 8
    assert "craftRecipe" in spec.intermediate_commands
    assert "collectBlocks" in spec.terminal_commands


async def test_active_task_completion_spec_for_chinese_iron_pickaxe_crafting() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "iron_pickaxe", "count": 1}]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    await agent._replace_active_task("Wilson", "帮我做两把铁镐")

    assert agent._active_task is not None
    assert len(agent._active_task_stack) == 1
    assert agent._active_task.baseline_inventory == {"iron_pickaxe": 1}
    assert agent._active_task.snapshots
    spec = agent._active_task.completion_spec
    assert spec is not None
    assert spec.kind == "craft_item"
    assert spec.target_items == ("iron_pickaxe",)
    assert spec.quantity == 2
    assert spec.requires_delivery is True


async def test_crafting_prep_does_not_complete_iron_mining_task() -> None:
    bridge = FakeBridge()
    bridge.inventory = [
        {"name": "oak_planks", "count": 4},
        {"name": "cobblestone", "count": 12},
    ]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "挖8铁矿"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "挖8铁矿")

    state = await agent.observe()
    finished = await agent._maybe_finish_after_command_completion(
        "craftRecipe",
        "Crafted 1x oak_planks.\nInventory: oak_log -1, oak_planks +4.",
        state,
    )

    assert finished is False
    assert agent._active_task is not None
    assert not any(
        event.kind == "completion_evidence"
        for event in (agent._active_task.events or [])
    )


async def test_dig_down_does_not_complete_iron_pickaxe_crafting_task() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "帮我做两把铁镐"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "帮我做两把铁镐")

    state = await agent.observe()
    finished = await agent._maybe_finish_after_command_completion(
        "digDown",
        "Dug down 5 blocks max.",
        state,
    )

    assert finished is False
    assert agent._active_task is not None
    assert len(agent._active_task_stack) == 1
    assert not any(
        event.kind == "completion_evidence"
        for event in (agent._active_task.events or [])
    )


async def test_completion_spec_delivery_candidate_for_mined_iron() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "raw_iron", "count": 8}]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "挖8铁矿"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "挖8铁矿")

    state = await agent.observe()

    assert agent._delivery_candidate_from_state(state) == ("raw_iron", 8)


async def test_missing_pickaxe_failure_recovers_and_retries_collect() -> None:
    bridge = SmartBridge()
    bridge.inventory = [
        {"name": "oak_log", "count": 1},
        {"name": "oak_planks", "count": 4},
        {"name": "cobblestone", "count": 12},
    ]
    bridge.nearby_blocks = ["iron_ore", "stone"]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "挖8铁矿"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "挖8铁矿")

    recovered = await agent._recover_missing_dependency(
        "collectBlocks",
        ["iron_ore", 8],
        None,
        (
            "FAILED: Need a stone_pickaxe to mine iron_ore. "
            "You have: oak_planks x4, cobblestone x12. "
            "Craft a stone_pickaxe first, then retry."
        ),
    )

    assert recovered is True
    assert ("craft_recipe", "stick", 1) in bridge.calls
    assert ("craft_recipe", "stone_pickaxe", 1) in bridge.calls
    assert bridge.calls.count(("collect_blocks", "iron_ore", 8)) == 1
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert ("give_to_player", "Wilson", "iron_ore", 8) in bridge.calls
    assert agent._active_task is None


async def test_missing_material_failure_recovers_common_materials_and_retries() -> None:
    bridge = SmartBridge()
    bridge.inventory = [
        {"name": "oak_log", "count": 1},
        {"name": "coal", "count": 2},
    ]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "做8个火把"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "做8个火把")

    recovered = await agent._recover_missing_dependency(
        "craftRecipe",
        ["torch", 8],
        None,
        "FAILED: Missing materials for torch: stick x2.",
    )

    assert recovered is True
    assert any(call[0] == "craft_recipe" and call[1].endswith("_planks") for call in bridge.calls)
    assert ("craft_recipe", "stick", 1) in bridge.calls
    assert ("craft_recipe", "torch", 8) in bridge.calls


async def test_direct_give_player_command_moves_to_player_first() -> None:
    bridge = SmartBridge()
    bridge.inventory = [{"name": "iron_pickaxe", "count": 2}]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "把两把铁镐带给我"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "把两把铁镐带给我")

    result = await agent._execute_agent_command(
        "givePlayer",
        ["Wilson", "iron_pickaxe", 2],
    )

    assert result == "Gave 2x iron_pickaxe to Wilson."
    go_index = bridge.calls.index(("go_to_player", "Wilson", 3))
    give_index = bridge.calls.index(("give_to_player", "Wilson", "iron_pickaxe", 2))
    assert go_index < give_index


async def test_recovery_budget_limits_repeated_dependency_repairs() -> None:
    bridge = SmartBridge()
    bridge.inventory = [
        {"name": "oak_log", "count": 2},
        {"name": "cobblestone", "count": 12},
    ]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "挖8铁矿"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "挖8铁矿")
    assert agent._active_task is not None
    agent._active_task.recovery_attempts = 3

    recovered = await agent._recover_missing_dependency(
        "collectBlocks",
        ["iron_ore", 8],
        None,
        "FAILED: Need a stone_pickaxe to mine iron_ore.",
    )

    assert recovered is False
    assert not any(call[0] == "craft_recipe" for call in bridge.calls)


async def test_delivery_joint_judgement_keeps_primary_model_weight_higher() -> None:
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    await agent._replace_active_task("Wilson", "做8个火把")
    assert agent._active_task is not None

    agent._apply_delivery_joint_judgement(DispatchDecision(
        route="direct_action",
        task_shape="atomic",
        confidence=0.9,
        reason="do an action in the world",
    ))

    assert agent._active_task.completion_spec is not None
    assert agent._active_task.completion_spec.requires_delivery is True
    assert agent._active_task.completion_spec.delivery_score > 0.5

    await agent._replace_active_task("Wilson", "跟我来")
    assert agent._active_task is not None
    agent._apply_delivery_joint_judgement(DispatchDecision(
        route="direct_item",
        task_shape="atomic",
        confidence=0.9,
        reason="maybe give an item",
        requested_outputs=["torch"],
    ))

    assert agent._active_task.completion_spec is not None
    assert agent._active_task.completion_spec.requires_delivery is False


async def test_observe_keeps_entity_counts_beyond_display_cap() -> None:
    bridge = FakeBridge()
    bridge.entities = [
        {
            "id": i,
            "name": "villager",
            "type": "mob",
            "position": {"x": float(i + 1), "y": 64.0, "z": 0.0},
            "health": 20,
        }
        for i in range(25)
    ]
    bridge.entities.append({
        "id": 99,
        "name": "chicken",
        "type": "animal",
        "position": {"x": 40.0, "y": 64.0, "z": 0.0},
        "health": 4,
    })
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge

    state = await agent.observe()

    assert state.entity_counts["villager"] == 25
    assert state.entity_counts["chicken"] == 1
    assert len(state.entities) == 20
    assert "chicken x1" in state.entities_nearby


async def test_count_entities_command_reports_matching_count() -> None:
    bridge = FakeBridge()
    bridge.entities = [
        {
            "id": 1,
            "name": "chicken",
            "type": "animal",
            "position": {"x": 3.0, "y": 64.0, "z": 0.0},
            "health": 4,
        },
        {
            "id": 2,
            "name": "chicken",
            "type": "animal",
            "position": {"x": 7.0, "y": 64.0, "z": 0.0},
            "health": 4,
        },
        {
            "id": 3,
            "name": "cow",
            "type": "animal",
            "position": {"x": 2.0, "y": 64.0, "z": 0.0},
            "health": 10,
        },
    ]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("countEntities", ["chicken", 64])

    assert "Found 2 chicken within 64" in result
    assert ("count_entities", "chicken", 64) in bridge.calls


async def test_come_to_me_uses_direct_action_without_llm() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fail_plan(*_args: object, **_kwargs: object) -> LLMOutput:
        raise AssertionError("LLM should not be called for come to me")

    agent.plan = fail_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "come to me")

    assert used_command is True
    assert ("go_to_player", "Wilson", 3) in bridge.calls


async def test_follow_me_uses_direct_continuous_follow_without_llm() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fail_plan(*_args: object, **_kwargs: object) -> LLMOutput:
        raise AssertionError("LLM should not be called for follow me")

    agent.plan = fail_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "follow me")

    assert used_command is True
    assert ("follow_player", "Wilson", 4) in bridge.calls


async def test_nearby_block_miss_returns_to_player_then_searches_64() -> None:
    class MissingBlockBridge(FakeBridge):
        async def collect_blocks(self, block_type: str, count: int = 1) -> str:
            self.calls.append(("collect_blocks", block_type, count))
            raise RuntimeError(f"No {block_type} nearby (32 block radius).")

    bridge = MissingBlockBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        assert tool_choice == "auto"
        return LLMOutput(content='!collectBlocks("oak_log", 2)', model="fake")

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "craft torch", max_responses=1)

    assert used_command is True
    assert ("collect_blocks", "oak_log", 2) in bridge.calls
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert ("search_for_block", "oak_log", 64) in bridge.calls


async def test_model_cannot_mute_without_player_request() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_message = "craft a shovel"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("stfu", [])

    assert result == "Ignored stfu because the player did not explicitly ask me to mute."
    assert agent._shut_up is False


async def test_model_cannot_stop_without_player_request() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_message = "craft 5 torch"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("stop", [])

    assert result == "Ignored stop because the player did not explicitly ask me to stop."
    assert ("stop",) not in bridge.calls


async def test_ok_you_can_stop_executes_direct_stop_without_llm() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fail_plan(*_args: object, **_kwargs: object) -> LLMOutput:
        raise AssertionError("LLM should not be called for direct stop requests")

    agent.plan = fail_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "ok you can stop")

    assert used_command is True
    assert ("stop",) in bridge.calls
    assert ("set_control_state", "jump", False) in bridge.calls


async def test_craft_request_already_satisfied_delivers_without_llm_or_crafting() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "torch", "count": 16}]
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fail_plan(*_args: object, **_kwargs: object) -> LLMOutput:
        raise AssertionError("LLM should not be called for already-satisfied craft requests")

    agent.plan = fail_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "craft 5 torch")

    assert used_command is True
    assert any("already have 16 torch" in chat for chat in bridge.chats)
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert ("give_to_player", "Wilson", "torch", 5) in bridge.calls
    assert ("stop",) not in bridge.calls
    assert not any(call[0] == "craft_recipe" for call in bridge.calls)


async def test_active_task_summarizes_success_to_memory() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "torch", "count": 16}]
    memory = FakeTaskMemory()
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = SummaryPrompter()  # type: ignore[assignment]
    agent._memory = memory  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    used_command = await agent.handle_message("Wilson", "craft 5 torch")

    assert used_command is True
    assert agent._active_task is None
    assert any(event[0] == "task_start" for event in memory.events)
    assert any(event[0] == "task_finish" for event in memory.events)
    assert len(memory.records) == 1
    record = memory.records[0]
    assert record.type.value == "episodic"
    assert "task_succeeded" in record.tags
    assert record.description == "Task summary from LLM."


async def test_active_task_marks_hard_craft_failure_failed() -> None:
    bridge = FakeBridge()

    async def craft_recipe(recipe_name: str, num: int = 1) -> str:
        bridge.calls.append(("craft_recipe", recipe_name, num))
        raise RuntimeError("Cannot read properties of undefined (reading 'offhand')")

    bridge.craft_recipe = craft_recipe  # type: ignore[method-assign]
    memory = FakeTaskMemory()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = SummaryPrompter()  # type: ignore[assignment]
    agent._memory = memory  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        assert tool_choice == "auto"
        return LLMOutput(content='!craftRecipe("torch", 5)', model="fake")

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "craft 5 torch")

    assert used_command is True
    assert agent._active_task is None
    assert ("craft_recipe", "torch", 5) in bridge.calls
    finish_events = [payload for event, payload in memory.events if event == "task_finish"]
    assert finish_events[-1]["status"] == "failed"
    assert "task_failed" in memory.records[-1].tags
    assert "memory_quality_lesson_only" in memory.records[-1].tags
    assert memory.records[-1].confidence == 0.45


async def test_successful_llm_cook_task_auto_delivers_result_on_finish() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "cooked_porkchop", "count": 2}]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "cook some pork"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "cook some pork")

    await agent._finish_active_task("succeeded", reason="Plan loop ended.")

    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert ("give_to_player", "Wilson", "cooked_porkchop", 2) in bridge.calls
    assert agent._active_task is None


async def test_memory_quality_gate_skips_unverified_success() -> None:
    memory = FakeTaskMemory()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._running = True
    agent._memory = memory  # type: ignore[assignment]
    agent._prompter = SummaryPrompter()  # type: ignore[assignment]
    await agent._replace_active_task("Wilson", "跟我来")

    await agent._finish_active_task("succeeded", reason="Plan loop ended.")

    assert agent._active_task is None
    assert memory.records == []


async def test_memory_quality_gate_skips_cancelled_task_without_actions() -> None:
    memory = FakeTaskMemory()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._running = True
    agent._memory = memory  # type: ignore[assignment]
    agent._prompter = SummaryPrompter()  # type: ignore[assignment]
    await agent._replace_active_task("Wilson", "跟我来")

    await agent._finish_active_task("cancelled", reason="Replaced by a newer player command.")

    assert agent._active_task is None
    assert memory.records == []


async def test_give_me_some_torch_uses_inventory_without_llm() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "torch", "count": 11}]
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fail_plan(*_args: object, **_kwargs: object) -> LLMOutput:
        raise AssertionError("LLM should not be called for direct torch requests")

    agent.plan = fail_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "give me some torch")

    assert used_command is True
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert ("give_to_player", "Wilson", "torch", 8) in bridge.calls


async def test_want_to_eat_pork_uses_planner_instead_of_direct_food_shortcut() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    plan_calls = 0

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        nonlocal plan_calls
        plan_calls += 1
        assert tool_choice == "auto"
        return LLMOutput(content='!searchForEntity("pig", 64)', model="fake")

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message(
        "Wilson",
        "I want to eat some pork",
        max_responses=1,
    )

    assert used_command is True
    assert plan_calls == 1
    assert ("search_for_entity", "pig", 64) in bridge.calls
    assert ("attack", "pig", 64) not in bridge.calls
    assert ("pickup_nearby_items", 8) not in bridge.calls
    assert not any(call[0] == "give_to_player" for call in bridge.calls)


async def test_cook_some_pork_reuses_furnace_and_delivers_cooked_food() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "porkchop", "count": 3}, {"name": "coal", "count": 1}]

    async def smelt_item(item_name: str, num: int = 1) -> str:
        bridge.calls.append(("smelt_item", item_name, num))
        return f"smelted {num} {item_name}"

    async def craft_recipe(recipe_name: str, num: int = 1) -> str:
        bridge.calls.append(("craft_recipe", recipe_name, num))
        return f"crafted {num} {recipe_name}"

    async def place_block(block_type: str) -> str:
        bridge.calls.append(("place_block", block_type))
        return f"placed {block_type}"

    bridge.smelt_item = smelt_item  # type: ignore[method-assign]
    bridge.craft_recipe = craft_recipe  # type: ignore[method-assign]
    bridge.place_block = place_block  # type: ignore[method-assign]
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fail_plan(*_args: object, **_kwargs: object) -> LLMOutput:
        raise AssertionError("LLM should not be called for direct cook requests")

    agent.plan = fail_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "cook some pork")

    assert used_command is True
    assert ("search_for_block", "furnace", 64) in bridge.calls
    assert ("smelt_item", "porkchop", 3) in bridge.calls
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert ("give_to_player", "Wilson", "cooked_porkchop", 3) in bridge.calls
    assert not any(call[0] == "craft_recipe" for call in bridge.calls)
    assert not any(call[0] == "place_block" for call in bridge.calls)


async def test_compound_cook_request_goes_to_llm_instead_of_direct_shortcuts() -> None:
    bridge = FakeBridge()
    bridge.inventory = []
    bridge.entities = [
        {
            "name": "cow",
            "type": "animal",
            "position": {"x": 5, "y": 64, "z": 0},
            "health": 10,
        },
    ]
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    plan_calls = 0

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        nonlocal plan_calls
        plan_calls += 1
        assert tool_choice == "auto"
        return LLMOutput(content='!searchForEntity("cow", 64)', model="fake")

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message(
        "Wilson",
        "pick up your nearby beef and cook them",
        max_responses=1,
    )

    assert used_command is True
    assert plan_calls == 1
    assert ("search_for_entity", "cow", 64) in bridge.calls
    assert not any(call[0] == "give_to_player" and call[2] == "beef" for call in bridge.calls)
    assert not any("I only have enough raw beef" in chat for chat in bridge.chats)


async def test_failed_direct_cook_falls_back_to_one_llm_attempt() -> None:
    bridge = FakeBridge()
    bridge.inventory = []
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    plan_calls = 0

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        nonlocal plan_calls
        plan_calls += 1
        assert tool_choice == "auto"
        return LLMOutput(content='!searchForEntity("cow", 64)', model="fake")

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "cook some beef")

    assert used_command is True
    assert plan_calls == 1
    assert ("search_for_entity", "cow", 64) in bridge.calls
    assert any("I'll try one fallback plan" in chat for chat in bridge.chats)


async def test_cook_task_retries_smelt_after_successful_furnace_clear() -> None:
    class PlannerGate:
        direct_confidence = 0.85

        async def classify(self, _message: str, _state: BotState) -> DispatchDecision:
            return DispatchDecision(
                route="llm_planner",
                task_shape="compound",
                confidence=0.91,
                reason="Exercise planner clear-furnace recovery.",
            )

    bridge = FakeBridge()
    bridge.inventory = [{"name": "mutton", "count": 6}, {"name": "coal", "count": 1}]
    bridge.nearby_blocks = ["furnace", "stone"]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._dispatch_gate = PlannerGate()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        assert tool_choice == "auto"
        return LLMOutput(content="!clearFurnace()", model="fake")

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "cook 6 mutton", max_responses=1)

    assert used_command is True
    assert ("clear_furnace",) in bridge.calls
    assert ("search_for_block", "furnace", 64) in bridge.calls
    assert ("smelt_item", "mutton", 6) in bridge.calls
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert ("give_to_player", "Wilson", "cooked_mutton", 6) in bridge.calls
    assert agent._active_task is None


async def test_dispatch_gate_can_force_full_planner_over_direct_cook() -> None:
    class PlannerGate:
        direct_confidence = 0.85

        async def classify(self, _message: str, _state: BotState) -> DispatchDecision:
            return DispatchDecision(
                route="llm_planner",
                task_shape="compound",
                confidence=0.93,
                reason="Needs multiple steps.",
                requested_outputs=["cooked_porkchop"],
                required_steps=["collect_porkchop", "smelt", "deliver"],
            )

    bridge = FakeBridge()
    bridge.inventory = [{"name": "porkchop", "count": 3}]
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._dispatch_gate = PlannerGate()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    plan_calls = 0

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        nonlocal plan_calls
        plan_calls += 1
        assert tool_choice == "auto"
        return LLMOutput(content='!searchForEntity("pig", 64)', model="fake")

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "cook some pork", max_responses=1)

    assert used_command is True
    assert plan_calls == 1
    assert ("search_for_entity", "pig", 64) in bridge.calls
    assert not any(call[0] == "smelt_item" for call in bridge.calls)


async def test_preflight_skips_crafting_furnace_when_one_is_nearby() -> None:
    bridge = FakeBridge()
    bridge.nearby_blocks = ["grass_block", "furnace"]

    async def craft_recipe(recipe_name: str, num: int = 1) -> str:
        bridge.calls.append(("craft_recipe", recipe_name, num))
        return f"crafted {num} {recipe_name}"

    bridge.craft_recipe = craft_recipe  # type: ignore[method-assign]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("craftRecipe", ["furnace", 1])

    assert result == (
        "SKIPPED: furnace is already nearby. "
        "Use the existing furnace instead of crafting another one."
    )
    assert not any(call[0] == "craft_recipe" for call in bridge.calls)


async def test_preflight_skips_placing_workstation_when_one_is_nearby() -> None:
    bridge = FakeBridge()
    bridge.nearby_blocks = ["grass_block", "crafting_table"]

    async def place_block(block_type: str) -> str:
        bridge.calls.append(("place_block", block_type))
        return f"placed {block_type}"

    bridge.place_block = place_block  # type: ignore[method-assign]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("placeHere", ["crafting_table"])

    assert result == (
        "SKIPPED: crafting_table is already nearby. "
        "Use the existing crafting_table instead of placing another one."
    )
    assert not any(call[0] == "place_block" for call in bridge.calls)


async def test_get_some_wood_uses_planner_instead_of_direct_item_shortcut() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    plan_calls = 0

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        nonlocal plan_calls
        plan_calls += 1
        assert tool_choice == "auto"
        return LLMOutput(content='!searchForBlock("oak_log", 64)', model="fake")

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "get some wood", max_responses=1)

    assert used_command is True
    assert plan_calls == 1
    assert ("search_for_block", "oak_log", 64) in bridge.calls
    assert ("collect_blocks", "oak_log", 8) not in bridge.calls
    assert not any(call[0] == "give_to_player" for call in bridge.calls)


async def test_get_some_wood_with_nearby_log_still_uses_planner() -> None:
    bridge = FakeBridge()
    bridge.nearby_blocks = ["grass_block", "spruce_log"]
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    plan_calls = 0

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        nonlocal plan_calls
        plan_calls += 1
        assert tool_choice == "auto"
        return LLMOutput(content='!searchForBlock("spruce_log", 64)', model="fake")

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "get some wood", max_responses=1)

    assert used_command is True
    assert plan_calls == 1
    assert ("search_for_block", "spruce_log", 64) in bridge.calls
    assert ("collect_blocks", "spruce_log", 8) not in bridge.calls
    assert not any(call[0] == "give_to_player" for call in bridge.calls)


async def test_iron_request_uses_planner_instead_of_direct_item_shortcut() -> None:
    bridge = SmartBridge()
    bridge.inventory = [
        {"name": "oak_log", "count": 5},
        {"name": "oak_planks", "count": 4},
        {"name": "cobblestone", "count": 12},
    ]
    bridge.nearby_blocks = ["iron_ore", "stone"]
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    plan_calls = 0

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        nonlocal plan_calls
        plan_calls += 1
        assert tool_choice == "auto"
        return LLMOutput(content='!getCraftingPlan("stone_pickaxe", 1)', model="fake")

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "挖8铁矿", max_responses=1)

    assert used_command is True
    assert plan_calls == 1
    assert ("get_crafting_plan", "stone_pickaxe", 1) in bridge.calls
    assert not any(call[0] == "collect_blocks" and call[1] == "iron_ore" for call in bridge.calls)
    assert not any(call[0] == "give_to_player" for call in bridge.calls)


async def test_break_nearby_blocks_command_dispatches() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command(
        "breakNearbyBlocks",
        ["tall_grass", 3],
        {"range": 5},
    )

    assert result == "broke 3 tall_grass"
    assert ("break_nearby_blocks", "tall_grass", 3, 5) in bridge.calls


async def test_seed_gathering_prefers_nearby_short_grass() -> None:
    bridge = FakeBridge()
    bridge.nearby_blocks = ["grass_block", "short_grass"]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_message = "收集8个种子种植它们"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command(
        "breakNearbyBlocks",
        ["tall_grass", 8],
        {"range": 16},
    )

    assert result == "broke 8 short_grass"
    assert ("break_nearby_blocks", "short_grass", 8, 16) in bridge.calls
    assert ("break_nearby_blocks", "tall_grass", 8, 16) not in bridge.calls


async def test_generic_wood_search_prefers_nearby_non_oak_log() -> None:
    bridge = FakeBridge()
    bridge.nearby_blocks = ["grass_block", "spruce_log"]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_message = "make some sticks"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("searchForBlock", ["oak_log", 64])

    assert result == "found spruce_log"
    assert ("search_for_block", "spruce_log", 64) in bridge.calls
    assert ("search_for_block", "oak_log", 64) not in bridge.calls


async def test_generic_planks_craft_prefers_inventory_log_family() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "spruce_log", "count": 1}]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_message = "make planks"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("craftRecipe", ["oak_planks", 1])

    assert result == "Crafted 1x spruce_planks."
    assert ("craft_recipe", "spruce_planks", 1) in bridge.calls
    assert ("craft_recipe", "oak_planks", 1) not in bridge.calls


async def test_explicit_oak_request_does_not_remap_wood_target() -> None:
    bridge = FakeBridge()
    bridge.nearby_blocks = ["spruce_log"]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_message = "find oak log"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("searchForBlock", ["oak_log", 64])

    assert result == "found oak_log"
    assert ("search_for_block", "oak_log", 64) in bridge.calls


async def test_torch_task_defers_log_charcoal_until_coal_searched() -> None:
    bridge = FakeBridge()
    bridge.inventory = [
        {"name": "oak_log", "count": 8},
        {"name": "stick", "count": 12},
        {"name": "furnace", "count": 1},
    ]
    bridge.nearby_blocks = ["furnace", "coal_ore"]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_message = "做32个火把"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("smeltItem", ["oak_log", 8])

    assert result is not None
    assert result.startswith("FAILED: Do not smelt logs into charcoal")
    assert not any(call[0] == "smelt_item" for call in bridge.calls)


async def test_torch_task_allows_charcoal_after_failed_coal_search() -> None:
    bridge = FakeBridge()
    bridge.inventory = [
        {"name": "oak_log", "count": 8},
        {"name": "stick", "count": 12},
        {"name": "furnace", "count": 1},
    ]
    bridge.nearby_blocks = ["furnace"]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_message = "make 32 torches"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    agent._record_action("searchForBlock", ["coal_ore", 64], "FAILED: No coal_ore nearby.")

    result = await agent._execute_agent_command("smeltItem", ["oak_log", 8])

    assert result == "Smelted 8x oak_log."
    assert ("smelt_item", "oak_log", 8) in bridge.calls


async def test_smelt_preflight_reports_missing_furnace() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "raw_iron", "count": 3}, {"name": "coal", "count": 1}]
    bridge.nearby_blocks = ["stone"]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("smeltItem", ["raw_iron", 3])

    assert result is not None
    assert "Missing materials for smeltItem: furnace x1" in result
    assert not any(call[0] == "smelt_item" for call in bridge.calls)


async def test_smelt_preflight_reports_missing_fuel() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "raw_iron", "count": 3}]
    bridge.nearby_blocks = ["furnace"]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("smeltItem", ["raw_iron", 3])

    assert result is not None
    assert "Missing materials for smeltItem: coal x1" in result
    assert not any(call[0] == "smelt_item" for call in bridge.calls)


async def test_smelt_preflight_reports_missing_input() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "raw_iron", "count": 1}, {"name": "coal", "count": 1}]
    bridge.nearby_blocks = ["furnace"]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command("smeltItem", ["raw_iron", 3])

    assert result is not None
    assert "Missing materials for smeltItem: raw_iron x2" in result
    assert not any(call[0] == "smelt_item" for call in bridge.calls)


async def test_breed_animals_command_dispatches() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command(
        "breedAnimals",
        ["chicken", "wheat_seeds"],
        {"range": 16},
    )

    assert result == "bred chicken with wheat_seeds"
    assert ("breed_animals", "chicken", "wheat_seeds", 16) in bridge.calls


async def test_breed_animals_completion_finishes_without_delivery() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "breed two chickens"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "breed two chickens")

    result = await agent._execute_agent_command(
        "breedAnimals",
        ["chicken", "wheat_seeds"],
        {"range": 16},
    )
    agent._active_task_add_event(
        "action_result",
        f"breedAnimals args=['chicken', 'wheat_seeds'] kwargs={{'range': 16}} -> {result}",
        ok=True,
    )
    state = await agent.observe()
    finished = await agent._maybe_finish_after_command_completion(
        "breedAnimals",
        result,
        state,
    )

    assert finished is True
    assert agent._active_task is None
    assert ("give_to_player", "Wilson", "wheat_seeds", 2) not in bridge.calls
    assert not any(call[0] == "give_to_player" for call in bridge.calls)


async def test_break_nearby_blocks_does_not_finish_planting_task() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "种8块小麦田"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "种8块小麦田")

    state = await agent.observe()
    finished = await agent._maybe_finish_after_command_completion(
        "breakNearbyBlocks",
        "Broke 1x tall_grass.\nPicked up 0 items.\nInventory: no inventory change.",
        state,
    )

    assert finished is False
    assert agent._active_task is not None
    assert not any(
        event.kind == "completion_evidence"
        for event in (agent._active_task.events or [])
    )


async def test_till_and_sow_single_plot_does_not_finish_multi_plot_task() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "种8块小麦田"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "种8块小麦田")

    state = await agent.observe()
    finished = await agent._maybe_finish_after_command_completion(
        "tillAndSow",
        "Tilled dirt and planted wheat_seeds at (1, 63, 1).",
        state,
    )

    assert finished is False
    assert agent._active_task is not None


async def test_till_and_sow_completion_finishes_single_plot_task() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "种1块小麦田"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "种1块小麦田")

    state = await agent.observe()
    finished = await agent._maybe_finish_after_command_completion(
        "tillAndSow",
        "Tilled dirt and planted wheat_seeds at (1, 63, 1).",
        state,
    )

    assert finished is True
    assert agent._active_task is None


async def test_till_and_sow_completion_finishes_when_requested_plots_are_done() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._last_message = "在我附近种植8田"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "在我附近种植8田")
    assert agent._active_task is not None
    for i in range(8):
        result = f"Tilled and planted wheat_seeds at ({i}, 62, 335)."
        agent._active_task_add_event(
            "action_result",
            f"tillAndSow args=[{i}, 62, 335, 'wheat_seeds'] kwargs={{}} -> {result}",
            ok=True,
        )

    state = await agent.observe()
    finished = await agent._maybe_finish_after_command_completion(
        "tillAndSow",
        "Tilled and planted wheat_seeds at (7, 62, 335).",
        state,
    )

    assert finished is True
    assert agent._active_task is None


async def test_plan_loop_does_not_mark_partial_planting_as_success() -> None:
    memory = FakeTaskMemory()
    bridge = FakeBridge()

    async def till_and_sow(
        x: float, y: float, z: float, seed_type: str | None = None,
    ) -> str:
        bridge.calls.append(("till_and_sow", x, y, z, seed_type))
        return f"Tilled and planted {seed_type or 'wheat_seeds'} at ({int(x)}, {int(y)}, {int(z)})."

    bridge.till_and_sow = till_and_sow  # type: ignore[attr-defined]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._memory = memory  # type: ignore[assignment]
    agent._last_sender = "Wilson"
    agent._last_message = "种16块田"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    await agent._replace_active_task("Wilson", "种16块田")

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        assert tool_choice == "auto"
        return LLMOutput(content='!tillAndSow(1, 63, 1, "wheat_seeds")', model="fake")

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent._run_plan_loop(max_responses=1, use_tools=True)

    assert used_command is True
    assert agent._active_task is None
    finish_events = [payload for event, payload in memory.events if event == "task_finish"]
    assert finish_events[-1]["status"] == "failed"
    assert "before completion spec was satisfied" in finish_events[-1]["summary"]


async def test_give_item_uses_inventory_and_defaults_to_8() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "coal", "count": 12}]
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fail_plan(*_args: object, **_kwargs: object) -> LLMOutput:
        raise AssertionError("LLM should not be called for direct item requests")

    agent.plan = fail_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "give me coal")

    assert used_command is True
    assert ("collect_blocks", "coal_ore", 8) not in bridge.calls
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert ("give_to_player", "Wilson", "coal", 8) in bridge.calls


async def test_get_crafting_plan_uses_python_knowledge_graph() -> None:
    class FakeKnowledge:
        def get_crafting_plan(self, target_item: str, quantity: int, inventory: object) -> str:
            assert target_item == "bed"
            assert quantity == 1
            return "KG plan for bed"

    bridge = FakeBridge()
    bridge.inventory = [{"name": "white_wool", "count": 3}]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._knowledge = FakeKnowledge()

    result = await agent._execute_agent_command(
        "getCraftingPlan",
        [],
        {"target_item": "bed", "quantity": 1},
    )

    assert result == "KG plan for bed"


async def test_craft_recipe_target_is_normalized_by_knowledge_graph() -> None:
    class FakeKnowledge:
        def resolve_craft_target(self, target_item: str, inventory: object) -> str:
            assert target_item == "bed"
            return "white_bed"

        def get_missing_materials(
            self, target_item: str, inventory: object, quantity: int = 1,
        ) -> dict[str, object]:
            return {
                "target": target_item,
                "have_all": True,
                "missing_counts": {},
                "steps": ["2. All materials available", "3. Craft white_bed"],
            }

    bridge = FakeBridge()
    bridge.inventory = [{"name": "white_wool", "count": 3}]

    async def craft_recipe(recipe_name: str, num: int = 1) -> str:
        bridge.calls.append(("craft_recipe", recipe_name, num))
        return f"crafted {recipe_name}"

    bridge.craft_recipe = craft_recipe  # type: ignore[method-assign]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    agent._knowledge = FakeKnowledge()

    result = await agent._execute_agent_command(
        "craftRecipe",
        [],
        {"recipe_name": "bed", "num": 1},
    )

    assert result == "crafted white_bed"
    assert ("craft_recipe", "white_bed", 1) in bridge.calls


async def test_craft_recipe_preflight_reports_missing_materials() -> None:
    class FakeKnowledge:
        def resolve_craft_target(self, target_item: str, inventory: object) -> str:
            return str(target_item)

        def get_missing_materials(
            self, target_item: str, inventory: object, quantity: int = 1,
        ) -> dict[str, object]:
            return {
                "target": target_item,
                "have_all": False,
                "missing_counts": {"white_wool": 1},
                "steps": ["2. Gather or craft missing: white_wool x1"],
            }

    bridge = FakeBridge()
    bridge.inventory = [{"name": "white_wool", "count": 2}]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    agent._knowledge = FakeKnowledge()

    result = await agent._execute_agent_command(
        "craftRecipe",
        [],
        {"recipe_name": "white_bed", "num": 1},
    )

    assert result is not None
    assert "Missing materials for white_bed" in result
    assert "white_wool x1" in result
    assert not any(call[0] == "craft_recipe" for call in bridge.calls)


async def test_craft_recipe_preflight_reports_missing_crafting_table() -> None:
    class FakeKnowledge:
        def resolve_craft_target(self, target_item: str, inventory: object) -> str:
            return str(target_item)

        def get_missing_materials(
            self, target_item: str, inventory: object, quantity: int = 1,
        ) -> dict[str, object]:
            return {
                "target": target_item,
                "have_all": False,
                "missing_counts": {"crafting_table": 1},
                "steps": ["1. Craft or place a crafting table"],
            }

    bridge = FakeBridge()

    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    agent._knowledge = FakeKnowledge()

    result = await agent._execute_agent_command(
        "craftRecipe",
        [],
        {"recipe_name": "wooden_pickaxe", "num": 1},
    )

    assert result is not None
    assert "Missing materials for wooden_pickaxe" in result
    assert "crafting_table x1" in result
    assert not any(call[0] == "craft_recipe" for call in bridge.calls)


async def test_craft_recipe_preflight_allows_table_missing_when_nearby() -> None:
    class FakeKnowledge:
        def resolve_craft_target(self, target_item: str, inventory: object) -> str:
            return str(target_item)

        def get_missing_materials(
            self, target_item: str, inventory: object, quantity: int = 1,
        ) -> dict[str, object]:
            return {
                "target": target_item,
                "have_all": False,
                "missing_counts": {"crafting_table": 1},
                "steps": ["1. Craft or place a crafting table"],
            }

    bridge = FakeBridge()
    bridge.nearby_blocks = ["crafting_table"]

    async def craft_recipe(recipe_name: str, num: int = 1) -> str:
        bridge.calls.append(("craft_recipe", recipe_name, num))
        return f"crafted {recipe_name}"

    bridge.craft_recipe = craft_recipe  # type: ignore[method-assign]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    agent._knowledge = FakeKnowledge()

    result = await agent._execute_agent_command(
        "craftRecipe",
        [],
        {"recipe_name": "wooden_pickaxe", "num": 1},
    )

    assert result == "crafted wooden_pickaxe"
    assert ("craft_recipe", "wooden_pickaxe", 1) in bridge.calls


async def test_collect_blocks_preflight_reports_missing_required_tool() -> None:
    class FakeKnowledge:
        def select_tool_for_block(self, block_name: str, inventory: object) -> dict[str, object]:
            assert block_name == "iron_ore"
            return {
                "can_mine": False,
                "requires_tool": True,
                "min_tool": "stone_pickaxe",
                "tool_type": "pickaxe",
            }

    bridge = FakeBridge()
    bridge.inventory = [{"name": "cobblestone", "count": 12}]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    agent._knowledge = FakeKnowledge()

    result = await agent._execute_agent_command(
        "collectBlocks",
        [],
        {"block_type": "iron_ore", "count": 8},
    )

    assert result is not None
    assert "Need a stone_pickaxe to mine iron_ore" in result
    assert not any(call[0] == "collect_blocks" for call in bridge.calls)


async def test_breed_animals_in_pen_requires_remembered_home_or_shelter() -> None:
    bridge = FakeBridge()
    bridge.nearby_blocks = ["oak_fence", "oak_fence_gate", "grass_block"]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._memory = FakeRecallMemory([])
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command(
        "breedAnimalsInPenBatch",
        [],
        {"animal_type": "cow", "pairs": 1, "breeding_item": "wheat"},
    )

    assert result is not None
    assert result.startswith(
        "FAILED: breedAnimalsInPenBatch is restricted to a remembered home/base/shelter"
    )
    assert not any(call[0] == "breed_animals" for call in bridge.calls)


async def test_breed_animals_in_pen_allows_marked_home_location() -> None:
    bridge = FakeBridge()
    bridge.position = {"x": 8.0, "y": 64.0, "z": 6.0}
    bridge.nearby_blocks = ["oak_fence", "oak_fence_gate", "grass_block"]
    home = MemoryRecord(
        name="base",
        description="Main home base",
        type=MemoryType.PLACE,
        scope=Scope.WORLD,
        confidence=0.95,
        tags=["home", "player_marked"],
        body="Position: (0, 64, 0)",
    )
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._memory = FakeRecallMemory([home])
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    result = await agent._execute_agent_command(
        "breedAnimalsInPenBatch",
        [],
        {"animal_type": "cow", "pairs": 1, "breeding_item": "wheat"},
    )

    assert result is not None
    assert "Bred 1 animal pairs of cow in nearby fenced pen" in result
    assert ("breed_animals", "cow", "wheat", 8.0) in bridge.calls


async def test_bed_crafting_rejects_wool_to_dye_misconception() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "light_gray_wool", "count": 1}]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._last_message = "craft a bed"

    result = await agent._execute_agent_command(
        "craftRecipe",
        [],
        {"recipe_name": "white_dye", "num": 1},
    )

    assert result is not None
    assert "Wool cannot be crafted into dye" in result
    assert "collect 2 more light_gray_wool" in result


async def test_action_loop_retries_empty_required_tool_response() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    responses = [
        LLMOutput(content="", model="fake"),
        LLMOutput(
            content="",
            model="fake",
            tool_calls=[
                ToolCallOutput(
                    id="call_1",
                    function=ToolCallFunction(name="action:stop", arguments="{}"),
                )
            ],
        ),
    ]

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        assert tool_choice == "auto"
        return responses.pop(0)

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent._run_plan_loop(max_responses=2, use_tools=True)

    assert used_command is False
    assert ("stop",) not in bridge.calls
    assert any(
        turn.role == "system" and "tool call" in turn.content
        for turn in agent._history
    )
    assert any(
        turn.role == "system"
        and "Ignored stop because the player did not explicitly ask me to stop" in turn.content
        for turn in agent._history
    )


async def test_action_loop_blocks_repeated_query_tool_call() -> None:
    class FakeKnowledge:
        calls = 0

        def get_crafting_plan(self, target_item: str, quantity: int, inventory: object) -> str:
            self.calls += 1
            assert target_item == "bed"
            assert quantity == 1
            return "Crafting plan: craft white_bed now."

        def resolve_craft_target(self, target_item: str, inventory: object) -> str:
            assert target_item == "bed"
            return "white_bed"

    bridge = FakeBridge()

    async def craft_recipe(recipe_name: str, num: int = 1) -> str:
        bridge.calls.append(("craft_recipe", recipe_name, num))
        return f"crafted {recipe_name}"

    bridge.craft_recipe = craft_recipe  # type: ignore[method-assign]
    agent = Agent(AgentConfig(name="bot", max_commands=1))
    agent._bridge = bridge
    agent._running = True
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()
    knowledge = FakeKnowledge()
    agent._knowledge = knowledge

    responses = [
        LLMOutput(
            content="",
            model="fake",
            tool_calls=[
                ToolCallOutput(
                    id="call_1",
                    function=ToolCallFunction(
                        name="action:getCraftingPlan",
                        arguments='{"target_item":"bed","quantity":1}',
                    ),
                )
            ],
        ),
        LLMOutput(
            content="",
            model="fake",
            tool_calls=[
                ToolCallOutput(
                    id="call_2",
                    function=ToolCallFunction(
                        name="action:getCraftingPlan",
                        arguments='{"target_item":"bed","quantity":1}',
                    ),
                )
            ],
        ),
        LLMOutput(
            content="",
            model="fake",
            tool_calls=[
                ToolCallOutput(
                    id="call_3",
                    function=ToolCallFunction(
                        name="action:craftRecipe",
                        arguments='{"recipe_name":"bed","num":1}',
                    ),
                )
            ],
        ),
    ]

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        assert tool_choice == "auto"
        return responses.pop(0)

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent._run_plan_loop(max_responses=3, use_tools=True)

    assert used_command is True
    assert knowledge.calls == 1
    assert ("craft_recipe", "white_bed", 1) in bridge.calls
    assert any(
        turn.role == "system" and "Do NOT call the same query again" in turn.content
        for turn in agent._history
    )


async def test_repeated_inventory_on_craft_bed_falls_back_to_sheep_search() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "gray_wool", "count": 1}]
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._last_message = "craft a bed"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    responses = [
        LLMOutput(content='Checking supplies. !inventory', model="fake"),
        LLMOutput(content='!inventory', model="fake"),
        LLMOutput(content='!inventory', model="fake"),
    ]

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        assert tool_choice == "auto"
        return responses.pop(0)

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent._run_plan_loop(max_responses=3, use_tools=True)

    assert used_command is True
    assert ("search_for_entity", "sheep", 64) in bridge.calls


async def test_repeated_inventory_uses_knowledge_wool_gap_for_bed() -> None:
    class BedKnowledge:
        def get_crafting_plan(
            self,
            target_item: str,
            quantity: int = 1,
            inventory: object | None = None,
        ) -> str:
            return "Crafting plan for gray_bed x1\nMissing:\n  - gray_wool x2"

        def get_missing_materials(
            self,
            target_item: str,
            inventory: object | None = None,
            quantity: int = 1,
        ) -> dict[str, object]:
            return {
                "target": "gray_bed",
                "have_all": False,
                "missing_counts": {"gray_wool": 2},
            }

    bridge = FakeBridge()
    bridge.inventory = [{"name": "gray_wool", "count": 1}]
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._knowledge = BedKnowledge()  # type: ignore[assignment]
    agent._running = True
    agent._last_message = "craft a bed"
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    responses = [
        LLMOutput(content='Checking supplies. !inventory', model="fake"),
        LLMOutput(content='!inventory', model="fake"),
        LLMOutput(content='!inventory', model="fake"),
    ]

    async def fake_plan(_state: BotState, *, tool_choice: str | None = None) -> LLMOutput:
        assert tool_choice == "auto"
        return responses.pop(0)

    agent.plan = fake_plan  # type: ignore[method-assign]

    used_command = await agent._run_plan_loop(max_responses=3, use_tools=True)

    assert used_command is True
    assert bridge.calls.count(("search_for_entity", "sheep", 64)) == 2
    assert bridge.calls.count(("attack", "sheep", 64)) == 2


async def test_cowardice_mode_moves_away_when_hurt_near_hostile() -> None:
    bridge = FakeBridge()
    modes = ModeEvaluator()
    state = BotState(
        health=8,
        position=Position(0, 64, 0),
        entities=[EntityInfo(name="zombie", distance=3)],
    )

    triggered = await modes.tick(state, bridge)

    assert triggered is BehaviorMode.COWARDICE
    assert ("move_away", 16) in bridge.action_calls()


async def test_self_defense_attacks_close_hostile() -> None:
    bridge = FakeBridge()
    modes = ModeEvaluator()
    state = BotState(
        health=20,
        position=Position(0, 64, 0),
        entities=[EntityInfo(name="zombie", distance=3)],
    )

    triggered = await modes.tick(state, bridge)

    assert triggered is BehaviorMode.SELF_DEFENSE
    assert ("attack", "zombie", 64) in bridge.action_calls()


async def test_torch_mode_places_torch_when_bridge_says_needed() -> None:
    class TorchBridge(FakeBridge):
        async def should_place_torch(self) -> dict[str, Any]:
            self.calls.append(("should_place_torch",))
            return {"should_place": True}

    bridge = TorchBridge()
    modes = ModeEvaluator()
    modes.set_enabled("torch_placing", True)
    state = BotState(position=Position(0, 64, 0))

    triggered = await modes.tick(state, bridge)

    assert triggered is BehaviorMode.TORCH_PLACING
    assert ("place_torch",) in bridge.action_calls()


async def test_unstuck_does_not_move_when_idle_without_goal() -> None:
    bridge = FakeBridge()
    modes = ModeEvaluator()
    state = BotState(position=Position(0, 64, 0), current_goal=None)

    # Two ticks at same position should not become "stuck" when idle.
    await modes.tick(state, bridge)
    mode_state = modes._states[BehaviorMode.UNSTUCK]
    mode_state._stuck_since = mode_state._stuck_since - 25
    triggered = await modes.tick(state, bridge)

    assert triggered is None
    assert ("move_away", 5) not in bridge.action_calls()


async def test_elbow_room_ignores_visible_player_without_close_distance() -> None:
    bridge = FakeBridge()
    modes = ModeEvaluator()
    modes.set_enabled("elbow_room", True)
    state = BotState(
        position=Position(0, 64, 0),
        players_nearby="  player:Wilson at (150.0, 68.0, 14.0)",
    )

    triggered = await modes.tick(state, bridge)

    assert triggered is None
    assert ("move_away", 3) not in bridge.action_calls()


async def test_torch_mode_throttles_negative_checks() -> None:
    bridge = FakeBridge()
    modes = ModeEvaluator()
    modes.set_enabled("torch_placing", True)
    state = BotState(position=Position(0, 64, 0))

    await modes.tick(state, bridge)
    await modes.tick(state, bridge)

    assert [c[0] for c in bridge.calls].count("should_place_torch") == 1


async def test_idle_optional_modes_are_off_by_default() -> None:
    bridge = FakeBridge()
    modes = ModeEvaluator()
    state = BotState(
        food=8,
        position=Position(0, 64, 0),
        entities_nearby="  item (2m) [id:10]\n  sheep (4m) [id:11]",
        entities=[EntityInfo(name="sheep", distance=4)],
        players_nearby="  player:Wilson at (1.0, 64.0, 1.0) dist:1.5",
    )

    for _ in range(3):
        triggered = await modes.tick(state, bridge)
        assert triggered is None

    actions = bridge.action_calls()
    assert ("pickup_nearby_items", 8) not in actions
    assert ("move_away", 3) not in actions
    assert ("attack", "sheep", 64) not in actions
