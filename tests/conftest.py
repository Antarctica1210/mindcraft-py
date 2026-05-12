"""
Shared pytest fixtures and import setup.

Adds ``src/`` to ``sys.path`` so tests can import packages by their flat
name (``from llm.input_router import ...``).

LLM calls go through ``langchain-openai`` (ChatOpenAI / OpenAIEmbeddings)
which is a required runtime dependency — no stubs needed.
"""
from __future__ import annotations

import os
import sys
from typing import Any

# ── Path setup ──────────────────────────────────────────────────────

HERE = os.path.dirname(__file__)
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


# ── Shared fakes ────────────────────────────────────────────────────


class FakeBridge:
    """Minimal in-process stand-in for :class:`mc.SocketBridge`.

    Captures call names and chat output so tests can assert routing
    decisions without a real Mineflayer connection.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.chats: list[str] = []
        # Mutable state — tests tweak these before calling observe()
        self.health: float = 20.0
        self.food: float = 20.0
        self.position: dict[str, float] = {"x": 0.0, "y": 64.0, "z": 0.0}
        self.entities_nearby: str = ""
        self.blocks_nearby: str = "standing on: grass_block"
        self.players_nearby: str = ""
        self.inventory: list[dict[str, Any]] = []
        self.entities: list[dict[str, Any]] = []
        self.nearby_blocks: list[str] = ["grass_block"]
        self.goal: str = "No current goal"

    # ── observation ────────────────────────────────────────────────
    async def observe(self) -> dict[str, Any]:
        self.calls.append(("observe",))
        return {
            "health": self.health,
            "food": self.food,
            "position": dict(self.position),
            "gamemode": "survival",
            "time_of_day": "day",
            "biome": "plains",
            "entities_nearby": self.entities_nearby,
            "blocks_nearby": self.blocks_nearby,
            "players_nearby": self.players_nearby,
        }

    async def get_inventory(self) -> list[dict[str, Any]]:
        self.calls.append(("get_inventory",))
        return list(self.inventory)

    async def get_entities(self) -> list[dict[str, Any]]:
        self.calls.append(("get_entities",))
        return list(self.entities)

    async def get_nearby_blocks(self, distance: int = 16) -> list[str]:
        self.calls.append(("get_nearby_blocks", distance))
        return list(self.nearby_blocks)

    async def get_goal(self) -> str:
        self.calls.append(("get_goal",))
        return self.goal

    # ── communication ─────────────────────────────────────────────
    async def chat(self, message: str) -> None:
        self.chats.append(message)
        self.calls.append(("chat", message))

    # ── actions (record-only) ─────────────────────────────────────
    async def stop(self) -> str:
        self.calls.append(("stop",))
        return "stopped"

    async def collect_blocks(self, block_type: str, count: int = 1) -> str:
        self.calls.append(("collect_blocks", block_type, count))
        return f"collected {count} {block_type}"

    async def move_to(self, x: float, y: float, z: float) -> str:
        self.calls.append(("move_to", x, y, z))
        return f"moved to ({x},{y},{z})"

    async def go_to_player(self, player_name: str, closeness: float = 3) -> str:
        self.calls.append(("go_to_player", player_name, closeness))
        return f"went to {player_name}"

    async def follow_player(self, player_name: str, follow_dist: float = 4) -> str:
        self.calls.append(("follow_player", player_name, follow_dist))
        return f"following {player_name}"

    async def move_away(self, distance: float = 16) -> str:
        self.calls.append(("move_away", distance))
        return f"moved away {distance}"

    async def search_for_entity(self, entity_type: str, search_range: float = 64) -> str:
        self.calls.append(("search_for_entity", entity_type, search_range))
        return f"found {entity_type}"

    async def set_control_state(self, control: str, state: bool = True) -> None:
        self.calls.append(("set_control_state", control, state))

    async def attack(self, entity_type: str, range_val: float = 64) -> str:
        self.calls.append(("attack", entity_type, range_val))
        return f"attacking {entity_type}"

    async def pickup_nearby_items(self, distance: float = 8) -> str:
        self.calls.append(("pickup_nearby_items", distance))
        return f"picked up items within {distance}"

    async def get_crafting_plan(self, target_item: str, quantity: int = 1) -> str:
        self.calls.append(("get_crafting_plan", target_item, quantity))
        return f"plan for {quantity} {target_item}"

    async def should_place_torch(self) -> dict[str, Any]:
        self.calls.append(("should_place_torch",))
        return {"should_place": False}

    async def place_torch(self) -> str:
        self.calls.append(("place_torch",))
        return "placed torch"

    async def look_at_position(self, x: float, y: float, z: float) -> str:
        self.calls.append(("look_at_position", x, y, z))
        return "looked"

    async def execute_code(self, code: str, timeout_ms: int = 60000) -> dict[str, Any]:
        self.calls.append(("execute_code", code, timeout_ms))
        return {"ok": True, "output": "ran code"}

    # ── helpers for tests ─────────────────────────────────────────
    def action_calls(self) -> list[tuple[Any, ...]]:
        """Return calls excluding observation/chat/query traffic."""
        obs_methods = {
            "observe", "get_inventory", "get_entities",
            "get_nearby_blocks", "get_goal", "chat",
        }
        return [c for c in self.calls if c[0] not in obs_methods]


# Re-export for `from conftest import FakeBridge` in test files.
__all__ = ["FakeBridge"]
