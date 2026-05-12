"""Regression tests for named tool-call argument dispatch."""
from __future__ import annotations

from typing import Any

from agent.executor import ActionExecutor
from llm.tool_parser import parse_tool_calls


def test_tool_call_preserves_named_arguments() -> None:
    commands = parse_tool_calls([{
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "action:craftRecipe",
            "arguments": '{"num": 1, "recipe_name": "white_bed"}',
        },
    }])

    assert len(commands) == 1
    cmd = commands[0]
    assert cmd.name == "craftRecipe"
    assert cmd.args == []
    assert cmd.kwargs == {"num": 1, "recipe_name": "white_bed"}


async def test_executor_dispatches_tool_call_kwargs_by_name() -> None:
    seen: dict[str, Any] = {}

    async def handler(_bridge: Any, recipe_name: str, num: int = 1) -> str:
        seen["recipe_name"] = recipe_name
        seen["num"] = num
        return f"{recipe_name}:{num}"

    executor = ActionExecutor()
    executor.register("craftRecipe", handler)

    result = await executor.execute(
        "craftRecipe",
        [],
        {"num": 1, "recipe_name": "white_bed"},
    )

    assert result == "white_bed:1"
    assert seen == {"recipe_name": "white_bed", "num": 1}


async def test_executor_normalizes_camel_case_tool_kwargs() -> None:
    seen: dict[str, Any] = {}

    async def handler(_bridge: Any, block_type: str, count: int = 1) -> str:
        seen["block_type"] = block_type
        seen["count"] = count
        return f"{block_type}:{count}"

    executor = ActionExecutor()
    executor.register("collectBlocks", handler)

    result = await executor.execute(
        "collectBlocks",
        [],
        {"blockType": "oak_log", "count": 3},
    )

    assert result == "oak_log:3"
    assert seen == {"block_type": "oak_log", "count": 3}


async def test_executor_maps_attack_range_kwarg_to_range_val() -> None:
    seen: dict[str, Any] = {}

    async def handler(_bridge: Any, entity_type: str, range_val: float = 64) -> str:
        seen["entity_type"] = entity_type
        seen["range_val"] = range_val
        return f"{entity_type}:{range_val}"

    executor = ActionExecutor()
    executor.register("attack", handler)

    result = await executor.execute(
        "attack",
        [],
        {"entity_type": "sheep", "range": 64},
    )

    assert result == "sheep:64"
    assert seen == {"entity_type": "sheep", "range_val": 64}
