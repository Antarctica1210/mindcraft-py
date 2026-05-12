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

    assert used_command is True
    assert ("stop",) in bridge.calls
    assert any(
        turn.role == "system" and "tool call" in turn.content
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
