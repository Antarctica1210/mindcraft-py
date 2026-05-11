"""
Shared pytest fixtures and import setup.

Adds ``src/`` to ``sys.path`` so tests can import packages by their flat
name (``from llm.input_router import ...``). When ``litellm`` is not
installed the LLM completion calls are stubbed so the prompter / agent
can be imported and exercised without the heavy dependency.

Real-end-to-end tests (``test_e2e.py``) bring their own dependencies and
do not need these stubs — the stub is only installed when the real
package is missing.
"""
from __future__ import annotations

import os
import sys
import types
from typing import Any

# ── Path setup ──────────────────────────────────────────────────────

HERE = os.path.dirname(__file__)
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# ── Optional litellm stub ───────────────────────────────────────────

try:  # pragma: no cover — only the install-state matters
    import litellm  # noqa: F401
except ImportError:
    _stub = types.ModuleType("litellm")
    _stub.suppress_debug_info = True

    class _StubMsg:
        content = "stubbed reply"
        tool_calls: list[Any] = []

    class _StubChoice:
        message = _StubMsg()
        finish_reason = "stop"

    class _StubResp:
        choices = [_StubChoice()]
        usage = None

    async def _acompletion(**kwargs: Any) -> _StubResp:
        return _StubResp()

    async def _aembedding(**kwargs: Any) -> Any:
        return types.SimpleNamespace(data=[{"embedding": [0.0] * 128}])

    _stub.acompletion = _acompletion  # type: ignore[attr-defined]
    _stub.aembedding = _aembedding    # type: ignore[attr-defined]

    class _CtxErr(Exception):
        """Stub for litellm.exceptions.ContextWindowExceededError."""

    _stub.exceptions = types.SimpleNamespace(  # type: ignore[attr-defined]
        ContextWindowExceededError=_CtxErr,
    )
    sys.modules["litellm"] = _stub
    sys.modules["litellm.exceptions"] = _stub.exceptions  # type: ignore[attr-defined]


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
