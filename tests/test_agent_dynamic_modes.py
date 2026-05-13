"""Focused tests for agent-owned runtime commands and behavior modes."""
from __future__ import annotations

from typing import Any

from conftest import FakeBridge

from agent.core import Agent
from agent.models import AgentConfig, BotState, EntityInfo, Position
from agent.modes import BehaviorMode, ModeEvaluator
from llm.message_builder import MessageBuilder
from llm.prompter import LLMInput, LLMOutput, ToolCallFunction, ToolCallOutput


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


async def test_want_to_eat_pork_uses_direct_food_workflow_without_llm() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fail_plan(*_args: object, **_kwargs: object) -> LLMOutput:
        raise AssertionError("LLM should not be called for direct food requests")

    agent.plan = fail_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "I want to eat some pork")

    assert used_command is True
    assert ("search_for_entity", "pig", 64) in bridge.calls
    assert ("attack", "pig", 64) in bridge.calls
    assert ("pickup_nearby_items", 8) in bridge.calls
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert ("give_to_player", "Wilson", "porkchop", 8) in bridge.calls


async def test_cook_some_pork_reuses_furnace_and_delivers_cooked_food() -> None:
    bridge = FakeBridge()
    bridge.inventory = [{"name": "porkchop", "count": 3}]

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
    assert ("search_for_block", "furnace", 16) in bridge.calls
    assert ("smelt_item", "porkchop", 3) in bridge.calls
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert ("give_to_player", "Wilson", "cooked_porkchop", 3) in bridge.calls
    assert not any(call[0] == "craft_recipe" for call in bridge.calls)
    assert not any(call[0] == "place_block" for call in bridge.calls)


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


async def test_get_some_wood_defaults_to_8_and_delivers_to_player() -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="bot", max_commands=3))
    agent._bridge = bridge
    agent._running = True
    agent._prompter = object()  # type: ignore[assignment]
    agent._executor.set_bridge(bridge)
    agent._executor._register_builtins()

    async def fail_plan(*_args: object, **_kwargs: object) -> LLMOutput:
        raise AssertionError("LLM should not be called for direct item requests")

    agent.plan = fail_plan  # type: ignore[method-assign]

    used_command = await agent.handle_message("Wilson", "get some wood")

    assert used_command is True
    assert ("search_for_block", "oak_log", 64) in bridge.calls
    assert ("collect_blocks", "oak_log", 8) in bridge.calls
    assert ("go_to_player", "Wilson", 3) in bridge.calls
    assert ("give_to_player", "Wilson", "oak_log", 8) in bridge.calls


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


async def test_craft_recipe_preflight_allows_table_only_missing() -> None:
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
