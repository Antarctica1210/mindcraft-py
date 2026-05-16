#!/usr/bin/env python3
"""
Bot regression test suite — command-level integration tests (no LLM).

Each test case in ``tests/manual_cases.json`` maps to one parametrized
pytest function.  The ``SmartBridge`` is configured with the case's
``setup`` state, then the corresponding bridge method is called directly
and the response is asserted against the ``expect`` block.

No real Minecraft server, no LLM. Most JSON cases verify the mock bridge
contract, while the focused Agent tests at the bottom exercise the direct
action chain that previously regressed: observe, reuse workstations, smelt,
and deliver positive item counts.

Usage::

    uv run pytest tests/test_bot_regression.py -v          # all cases
    uv run pytest tests/test_bot_regression.py -v -k "B01" # single case
    uv run pytest tests/test_bot_regression.py -v -k "give" # keyword filter
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

# Each test case loaded from the JSON spec.
HERE = os.path.dirname(__file__)
CASES_FILE = os.path.join(HERE, "manual_cases.json")

with open(CASES_FILE) as fh:
    _SPEC = json.load(fh)

CASES: list[dict[str, Any]] = _SPEC["cases"]


# ── Per-case command mapping ──────────────────────────────────────────
#
# Maps test-case id → (method_name, args_tuple) so each llm=false case
# hits exactly the bridge method it's meant to exercise.  This keeps the
# tests fast, deterministic, and free of LLM / InputRouter variance.

def _cmd_map(case: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """Return (bridge_method, args) for a test case."""
    mid = case["id"]

    mapping: dict[str, tuple[str, tuple[Any, ...]]] = {
        "B01": ("get_inventory", ()),
        "B02": ("observe", ()),
        "B03": ("get_nearby_blocks", (16,)),
        "B06": ("stop", ()),
        "M01": ("search_for_block", ("oak_log", 64)),
        "M02": ("search_for_block", ("crafting_table", 64)),
        "M03": ("search_for_block", ("furnace", 64)),
        "G02": ("collect_blocks", ("stone", 8)),
        "C01": ("get_crafting_plan", ("oak_planks", 8)),
        "R01": ("get_crafting_plan", ("wooden_shovel", 1)),
        "S01": ("smelt_item", ("porkchop", 2)),
        "C05": ("search_for_block", ("furnace", 64)),
        "D01": ("give_to_player", ("test_player", "dirt", 1)),
        "D03": ("give_to_player", ("test_player", "diamond", 99)),
        "F02": ("get_crafting_plan", ("saddle", 1)),
        "F05": ("search_for_block", ("ancient_debris", 64)),
    }
    if mid in mapping:
        return mapping[mid]

    # Fallback heuristic: use method_called from expect if present
    expect = case.get("expect", {})
    method = expect.get("method_called", "")
    if method:
        return (method, ())

    raise ValueError(f"No command mapping for test case {mid} ({case.get('description', '')})")


def _ids(case: dict[str, Any]) -> str:
    """Pytest parameter ID:  B01 ─ inventory query"""
    return f"{case['id']} ─ {case['description']}"


# ── Fixture ───────────────────────────────────────────────────────────

from conftest import SmartBridge  # noqa: E402


def _apply_setup(bridge: SmartBridge, case: dict[str, Any]) -> None:
    """Configure bridge state from the test case ``setup`` block."""
    setup = case.get("setup", {})
    inv = setup.get("inventory")
    if inv:
        bridge.set_inventory(inv)
    pos = setup.get("position")
    if pos:
        bridge.position = pos
    health = setup.get("health")
    if health is not None:
        bridge.health = float(health)
    food = setup.get("food")
    if food is not None:
        bridge.food = float(food)
    nearby = setup.get("nearby_blocks")
    if nearby:
        bridge.nearby_blocks = list(nearby)
    resp_overrides = setup.get("response_overrides", {})
    for method, response in resp_overrides.items():
        bridge.override(method, response)


async def _call_bridge(bridge: SmartBridge, method: str, args: tuple[Any, ...]) -> Any:
    """Dispatch a method name + args tuple to the bridge."""
    func = getattr(bridge, method, None)
    if func is None:
        raise AttributeError(f"SmartBridge has no method '{method}'")
    return await func(*args)


# ── The test ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", CASES, ids=_ids)
@pytest.mark.asyncio
async def test_regression_case(case: dict[str, Any]) -> None:
    """Run a single command-level regression test case."""
    if case.get("manual_only") or case.get("llm"):
        pytest.skip("Manual in-game/LLM case; not run by the mock bridge suite.")

    bridge = SmartBridge()
    _apply_setup(bridge, case)

    method, args = _cmd_map(case)
    response = await _call_bridge(bridge, method, args)
    response_str = str(response) if response is not None else ""

    expect = case.get("expect", {})

    # ── response_contains — every string must appear ──────────────
    for needle in expect.get("response_contains", []):
        assert needle.lower() in response_str.lower(), (
            f"[{case['id']}] Expected response to contain '{needle}'.\n"
            f"  message: {case.get('message')}\n"
            f"  method:  {method}{args}\n"
            f"  got:     {response_str[:500]}"
        )

    # ── response_contains_any — at least one must appear ──────────
    any_list = expect.get("response_contains_any", [])
    if any_list:
        found = any(n.lower() in response_str.lower() for n in any_list)
        assert found, (
            f"[{case['id']}] Expected response to contain at least one of {any_list}.\n"
            f"  message: {case.get('message')}\n"
            f"  method:  {method}{args}\n"
            f"  got:     {response_str[:500]}"
        )

    # ── response_not_contains — none of these may appear ──────────
    for needle in expect.get("response_not_contains", []):
        assert needle.lower() not in response_str.lower(), (
            f"[{case['id']}] Response must NOT contain '{needle}'.\n"
            f"  message: {case.get('message')}\n"
            f"  method:  {method}{args}\n"
            f"  got:     {response_str[:500]}"
        )

    # ── method_called — verify bridge method was invoked ──────────
    expected_method = expect.get("method_called")
    if expected_method:
        assert bridge.method_called(expected_method), (
            f"[{case['id']}] Expected method '{expected_method}' to be called.\n"
            f"  Actual calls: {[c[0] for c in bridge.calls]}"
        )

    # ── method_not_called — verify bridge method was NOT invoked ──
    not_called = expect.get("method_not_called")
    if not_called:
        assert not bridge.method_called(not_called), (
            f"[{case['id']}] Method '{not_called}' was called but should NOT have been.\n"
            f"  Actual calls: {[c[0] for c in bridge.calls]}"
        )
    for blocked_method in expect.get("method_not_called_any", []):
        assert not bridge.method_called(blocked_method), (
            f"[{case['id']}] Method '{blocked_method}' was called but should NOT have been.\n"
            f"  Actual calls: {[c[0] for c in bridge.calls]}"
        )

    # ── log_must_contain — check response for key strings ─────────
    for needle in expect.get("log_must_contain", []):
        assert needle in response_str, (
            f"[{case['id']}] Response must contain '{needle}'.\n"
            f"  got: {response_str[:500]}"
        )

    # ── log_must_not_contain — check response lacks bad strings ───
    for needle in expect.get("log_must_not_contain", []):
        assert needle not in response_str, (
            f"[{case['id']}] Response must NOT contain '{needle}'.\n"
            f"  got: {response_str[:500]}"
        )


# ── Smoke tests that don't need JSON cases ────────────────────────────


@pytest.mark.asyncio
async def test_socket_server_no_crash_patterns() -> None:
    """Verify error handling doesn't produce crash-like output."""
    bridge = SmartBridge()

    # Unknown block should produce a clear error, not a crash
    resp = await bridge.search_for_block("nonexistent_block_xyz", 64)
    assert "socket" not in resp.lower()
    assert "closed connection" not in resp.lower()
    assert "64" in resp  # range mentioned

    # Uncraftable item should produce a clear "no recipe" message
    resp = await bridge.get_crafting_plan("nonexistent_item_xyz", 1)
    assert "No recipe" in resp or "no recipe" in resp.lower()


@pytest.mark.asyncio
async def test_search_range_never_exceeds_64() -> None:
    """searchForBlock should respect the 64-block radius limit."""
    bridge = SmartBridge()

    resp = await bridge.search_for_block("diamond_ore", 65)
    # Range > 64 should be clamped or produce an error
    assert "64" in resp or "exceed" in resp.lower() or "not found" in resp.lower(), (
        f"search_range=65 should not silently pass: {resp}"
    )


@pytest.mark.asyncio
async def test_give_player_zero_count_detected() -> None:
    """Gave 0x should never appear in successful deliveries."""
    bridge = SmartBridge()
    bridge.inventory = [{"name": "dirt", "count": 3}]

    # Requesting more than we have should not claim Gave Nx when N=0
    resp = await bridge.give_to_player("player", "dirt", 3)
    assert "Gave 0x" not in resp
    # Should give what we have (3) since count=3 and we have 3
    assert "Gave 3x" in resp

    # Requesting item we don't have
    resp = await bridge.give_to_player("player", "diamond", 1)
    assert "Gave 0x" not in resp
    assert "No" in resp or "FAILED" in resp


@pytest.mark.asyncio
async def test_collect_blocks_tool_check_clear() -> None:
    """When tool is missing, error message names the exact tool needed."""
    bridge = SmartBridge()
    bridge.inventory = [
        {"name": "wooden_axe", "count": 1},
        {"name": "stick", "count": 10},
    ]

    resp = await bridge.collect_blocks("stone", 8)
    assert "pickaxe" in resp.lower(), (
        f"Should tell user they need a pickaxe: {resp}"
    )
    assert "wooden_pickaxe" in resp.lower(), (
        f"Should name the simplest pickaxe: {resp}"
    )

    # Adding a wooden_pickaxe should make it succeed
    bridge.inventory.append({"name": "wooden_pickaxe", "count": 1})
    resp = await bridge.collect_blocks("stone", 8)
    assert "Collected" in resp


@pytest.mark.asyncio
async def test_nearby_furnace_prevents_craft_furnace() -> None:
    """If furnace is nearby, craft furnace should skip."""
    bridge = SmartBridge()
    bridge.inventory = [{"name": "cobblestone", "count": 64}]
    bridge.nearby_blocks = ["furnace", "stone", "dirt", "granite"]

    # Searching for furnace should find it
    resp = await bridge.search_for_block("furnace", 64)
    assert "furnace" in resp.lower()

    # The bridge method was called — in real E2E this means the LLM
    # would see the furnace and skip crafting.  For command-level
    # we just verify the search works correctly.
    assert bridge.method_called("search_for_block")


@pytest.mark.asyncio
async def test_recipe_crafting_table_reuse() -> None:
    """Nearby crafting_table should be used, not re-placed."""
    bridge = SmartBridge()
    bridge.inventory = [
        {"name": "oak_planks", "count": 6},
        {"name": "stick", "count": 12},
        {"name": "crafting_table", "count": 1},
    ]
    bridge.nearby_blocks = ["crafting_table", "grass_block", "dirt"]

    # Getting a crafting plan should work without triggering place_here
    resp = await bridge.get_crafting_plan("wooden_shovel", 1)
    assert "Crafting" in resp or "Can craft" in resp

    # place_here should NOT have been called (reusing nearby table)
    assert not bridge.method_called("place_here"), (
        "place_here was called but nearby crafting_table should be reused"
    )


def _make_agent(bridge: SmartBridge):
    """Create an Agent wired to SmartBridge without starting LLM services."""
    from agent.core import Agent
    from agent.models import AgentConfig

    agent = Agent(AgentConfig(name="mindcraft-bot"))
    agent._bridge = bridge
    agent._running = True
    agent._last_sender = "Wilson"
    agent._executor._register_builtins()
    agent._executor.set_bridge(bridge)
    return agent


@pytest.mark.asyncio
async def test_agent_cook_reuses_nearby_furnace_and_delivers() -> None:
    """Direct cook path should not craft/place a furnace when one is nearby."""
    bridge = SmartBridge()
    bridge.inventory = [
        {"name": "porkchop", "count": 2},
        {"name": "stick", "count": 10},
    ]
    bridge.nearby_blocks = ["furnace", "stone", "granite"]
    agent = _make_agent(bridge)
    agent._last_message = "cook some pork"
    await agent._replace_active_task("Wilson", "cook some pork")

    state = await agent.observe()
    handled = await agent._try_direct_cook_request(state)

    assert handled is True
    assert bridge.method_called("search_for_block")
    assert bridge.method_called("smelt_item")
    assert bridge.method_called("go_to_player")
    assert bridge.method_called("give_to_player")
    assert not bridge.method_called("craft_recipe")
    assert not bridge.method_called("place_block")
    give_calls = [c for c in bridge.calls if c[0] == "give_to_player"]
    assert give_calls[-1][2] == "cooked_porkchop"
    assert give_calls[-1][3] > 0
    assert agent._active_task is None


@pytest.mark.asyncio
async def test_agent_finish_auto_delivers_successful_task_result() -> None:
    """A succeeded task that expects an item should deliver before finishing."""
    bridge = SmartBridge()
    bridge.inventory = [{"name": "cooked_porkchop", "count": 2}]
    bridge.nearby_blocks = ["furnace", "stone"]
    agent = _make_agent(bridge)
    agent._last_message = "cook some pork"
    await agent._replace_active_task("Wilson", "cook some pork")
    agent._active_task_add_event(
        "action_result",
        "smeltItem kwargs={'item_name': 'porkchop', 'num': 2} -> Smelted 2x porkchop.",
        ok=True,
    )

    await agent._finish_active_task("succeeded", reason="Cooked requested pork.")

    assert bridge.method_called("go_to_player")
    assert bridge.method_called("give_to_player")
    give_calls = [c for c in bridge.calls if c[0] == "give_to_player"]
    assert give_calls[-1][2] == "cooked_porkchop"
    assert give_calls[-1][3] == 2
    assert agent._active_task is None
