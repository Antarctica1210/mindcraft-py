"""Phase 4a file memory system tests."""
from __future__ import annotations

from pathlib import Path

from conftest import FakeBridge

from agent.core import Agent
from agent.models import AgentConfig
from memory.file_store import FileMemoryStore, ScopedFileMemoryStore
from memory.models import MemoryQuery, MemoryRecord, MemoryType, Scope
from memory.service import MemoryService


async def test_file_memory_store_writes_capped_memory_index(tmp_path: Path) -> None:
    store = FileMemoryStore(tmp_path / "memory")
    for i in range(205):
        await store.put(MemoryRecord(
            name=f"fact_{i}",
            description=f"fact number {i}",
            type=MemoryType.FACT,
            scope=Scope.WORLD,
            body=f"body {i}",
        ))

    index = (tmp_path / "memory" / "MEMORY.md").read_text()
    memory_lines = [line for line in index.splitlines() if line.startswith("- [")]

    assert len(memory_lines) == 200
    assert all(len(line) <= 150 for line in memory_lines)


async def test_scoped_file_store_routes_bot_and_world_memories(tmp_path: Path) -> None:
    store = ScopedFileMemoryStore(tmp_path / "bots" / "andy", world_id="main")
    await store.put(MemoryRecord(
        name="avoid_night_caving",
        description="Avoid caving at night without a shield",
        type=MemoryType.FEEDBACK,
        scope=Scope.BOT,
        body="**Why:** risky\n\n**How to apply:** check time and shield.",
    ))
    await store.put(MemoryRecord(
        name="base",
        description="Main base",
        type=MemoryType.PLACE,
        scope=Scope.WORLD,
        world_id="main",
        body="Position: (1, 64, 2)",
    ))

    assert (
        tmp_path / "bots" / "andy" / "universal" / "memory" / "feedback"
        / "avoid_night_caving.md"
    ).exists()
    assert (
        tmp_path / "bots" / "andy" / "worlds" / "main" / "memory" / "place"
        / "base.md"
    ).exists()

    results = await store.search(MemoryQuery(keywords=["base"], limit=5))
    assert results[0].record.name == "base"


async def test_meta_command_persists_marked_place(tmp_path: Path) -> None:
    bridge = FakeBridge()
    bridge.position = {"x": 10, "y": 65, "z": -4}
    agent = Agent(AgentConfig(name="andy"))
    agent._bridge = bridge
    agent._running = True

    store = ScopedFileMemoryStore(tmp_path / "bots" / "andy")
    memory = MemoryService(store, bot_name="andy", base_dir=tmp_path / "bots" / "andy")
    await memory.start()
    agent._memory = memory

    await agent._handle_meta_command("remember here as base", "wilson")

    record = await store.get("base", scope=Scope.WORLD, type=MemoryType.PLACE.value)
    assert record is not None
    assert "Position: (10, 65, -4)" in record.body
    assert any("Remembered base" in chat for chat in bridge.chats)


async def test_feedback_handler_persists_recent_action_trace(tmp_path: Path) -> None:
    bridge = FakeBridge()
    agent = Agent(AgentConfig(name="andy"))
    agent._bridge = bridge
    agent._running = True
    agent._record_action("digDown", [10], "FAILED: lava nearby")

    store = ScopedFileMemoryStore(tmp_path / "bots" / "andy")
    memory = MemoryService(store, bot_name="andy", base_dir=tmp_path / "bots" / "andy")
    await memory.start()
    agent._memory = memory

    await agent._handle_feedback("never dig straight down near lava", "wilson")

    results = await store.search(MemoryQuery(
        type=MemoryType.FEEDBACK,
        keywords=["lava"],
        scope=Scope.BOT,
    ))
    assert results
    assert "digDown" in results[0].record.body
    assert any("saved feedback" in chat for chat in bridge.chats)


async def test_go_to_coordinates_validates_world_height() -> None:
    agent = Agent(AgentConfig(name="andy"))
    result = await agent._execute_agent_command(
        "goToCoordinates",
        [],
        {"x": 1, "y": 999, "z": 2},
    )

    assert result is not None
    assert result.startswith("FAILED:")
    assert "outside the valid world height" in result
