"""
Phase 2 — Pydantic v2 domain models for the agent system.

Mirrors data structures from the JS codebase:
  - mcdata.js bot state helpers
  - history.js conversation records
  - commands/index.js parsed commands
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Re-export the canonical ConversationTurn from the LLM layer
from llm.message_builder import ConversationTurn as ConversationTurn


@dataclass
class Position:
    """3D position in the Minecraft world."""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def offset(self, dx: float = 0, dy: float = 0, dz: float = 0) -> Position:
        return Position(self.x + dx, self.y + dy, self.z + dz)

    def distance_to(self, other: Position) -> float:
        dx = self.x - other.x
        dy = self.y - other.y
        dz = self.z - other.z
        return float((dx * dx + dy * dy + dz * dz) ** 0.5)

    def __iter__(self) -> Any:
        yield self.x
        yield self.y
        yield self.z


@dataclass
class InventoryItem:
    """A single item in the bot's inventory."""
    name: str = "unknown"
    count: int = 0
    slot: int = 0


@dataclass
class EntityInfo:
    """A nearby entity observed by the bot."""
    name: str = ""
    entity_type: str = ""
    position: Position | None = None
    distance: float = 0.0
    health: float = 0.0


@dataclass
class BotState:
    """Snapshot of the bot's current state, gathered each observation cycle.

    Schema aligned with ``SocketBridge.observe()`` + parallel inventory /
    entities / nearby-block queries. The string summary fields
    (``entities_nearby`` / ``blocks_nearby`` / ``players_nearby``) come
    straight from ``observe()`` and are suited for prompt injection; the
    structured fields below them carry the typed details for tools,
    SafetyMonitor, and rule-based modes.
    """
    # ── Direct from bridge.observe() ────────────────────────────────
    health: float = 20.0
    food: float = 20.0
    position: Position = field(default_factory=Position)
    gamemode: str = "survival"
    time_of_day: str = ""
    biome: str = ""
    entities_nearby: str = ""      # human-readable summary
    blocks_nearby: str = ""        # human-readable summary
    players_nearby: str = ""       # human-readable summary
    # ── Parallel-fetched structured details ─────────────────────────
    inventory: list[InventoryItem] = field(default_factory=list)
    entities: list[EntityInfo] = field(default_factory=list)
    entity_counts: dict[str, int] = field(default_factory=dict)
    nearby_block_types: list[str] = field(default_factory=list)
    current_goal: str | None = None

    @property
    def is_alive(self) -> bool:
        return self.health > 0


@dataclass
class CommandCall:
    """A parsed command extracted from LLM output.

    Mirrors the JS `parseCommandMessage()` result from commands/index.js.
    """
    name: str          # e.g. "collectBlocks"
    args: list[Any] = field(default_factory=list)
    raw: str = ""      # original matched text


@dataclass
class AgentConfig:
    """Configuration for an Agent instance.

    Mirrors settings.js + profile system.
    """
    name: str = ""
    profile: dict[str, Any] = field(default_factory=dict)
    max_commands: int = -1  # -1 = unlimited, matches JS behavior
    verbose: bool = True
    narrate_behavior: bool = False
    show_command_syntax: str = "full"  # "full", "shortened", or "none"
    blocked_actions: list[str] = field(default_factory=list)
    allow_vision: bool = False
