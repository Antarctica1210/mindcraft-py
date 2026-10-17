"""
Phase 3 — Memory data models.

All memory types share a common schema (YAML frontmatter + markdown body).
Defines the contract for MemoryLayer reads/writes before any specific
storage backend (files, SQLite, FAISS) is plugged in.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

# ── Scope ──────────────────────────────────────────────────────────────


class Scope(str, enum.Enum):
    """Memory scope — controls recall order and write routing."""

    MINECRAFT = "minecraft"  # static game mechanics (Phase 7a read-only)
    BOT = "bot"              # cross-world skills, preferences, lessons
    WORLD = "world"          # per-world facts (coordinates, chests, players)


# Recall priority: more specific wins
SCOPE_PRIORITY: dict[Scope, int] = {Scope.WORLD: 0, Scope.BOT: 1, Scope.MINECRAFT: 2}


# ── Memory type ────────────────────────────────────────────────────────


class MemoryType(str, enum.Enum):
    PLACE = "place"
    FEEDBACK = "feedback"
    FACT = "fact"
    EPISODIC = "episodic"
    SKILL = "skill"


# ── Core models ────────────────────────────────────────────────────────


class MemoryRecord(BaseModel):
    """One memory entry — maps to a .md file with YAML frontmatter.

    The ``body`` field holds the markdown content below the frontmatter.
    All other fields come from the YAML frontmatter.
    """

    name: str = Field(description="Unique name within namespace/type (e.g. 'base')")
    description: str = Field(default="", description="One-line summary for MEMORY.md index")
    type: MemoryType
    scope: Scope = Field(default=Scope.WORLD)

    # World context
    world_id: str = Field(default="default", description="Only meaningful when scope=world")

    # Timestamps
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Lifecycle
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    ttl: int | None = Field(default=None, description="Seconds until expiry (None = permanent)")

    # Validity conditions (Pydantic-serialisable expressions)
    valid_when: str | None = Field(default=None, description="Condition string for validity")
    invalid_when: str | None = Field(default=None, description="Condition string for invalidation")

    # Provenance
    source: str = Field(default="bot_observed", description="Provenance source tag")
    evidence: str | None = Field(default=None, description="Observation evidence, e.g. L1 trace ID")
    tags: list[str] = Field(default_factory=list)

    # Content
    body: str = Field(default="", description="Markdown body below the frontmatter")

    @field_validator("source")
    @classmethod
    def _validate_source(cls, v: str) -> str:
        allowed = {
            "bot_observed", "player_confirmed", "player_marked",
            "llm_reflected", "imported",
        }
        if v not in allowed:
            raise ValueError(f"source must be one of {allowed}, got {v!r}")
        return v

    def index_line(self, max_len: int = 150) -> str:
        """Render a single line for MEMORY.md."""
        return f"- [{self.name}]({self.type.value}/{self.name}.md) — {self.description}"[
            :max_len
        ]

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.ttl is None:
            return False
        now = now or datetime.now(timezone.utc)
        return (now - self.updated).total_seconds() > self.ttl


class MemoryQuery(BaseModel):
    """Search / recall parameters passed to MemoryService.recall()."""

    namespace: str | None = Field(
        default=None, description="e.g. 'semantic', 'episodic', 'procedural'"
    )
    scope: Scope | None = Field(default=None, description="Filter by scope")
    type: MemoryType | None = Field(default=None, description="Filter by MemoryType")
    name: str | None = Field(default=None, description="Exact name match")
    keywords: list[str] = Field(
        default_factory=list, description="Keyword match on body + description"
    )
    tags: list[str] = Field(default_factory=list, description="Filter by tags")
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    limit: int = Field(default=10, ge=1, le=100)
    world_id: str | None = Field(default=None, description="Only for scope=world queries")


class MemoryResult(BaseModel):
    """A single recall hit with relevance metadata."""

    record: MemoryRecord
    score: float = Field(default=0.0, description="Relevance score (higher = better)")
    matched_on: str = Field(default="", description="What field matched (name/keyword/tag)")


# ── Working context ────────────────────────────────────────────────────


class WorkingContext(BaseModel):
    """Aggregated context injected into the LLM prompt each turn.

    Built by ``MemoryService.get_working_context()`` and consumed by
    ``MessageBuilder``.  All fields are plain strings / lists so the
    builder only does token-budget truncation, no deep inspection.
    """

    # L0 conversation window
    recent_conversation: list[str] = Field(default_factory=list)

    # Current state snapshot
    health: float = 20.0
    food: float = 20.0
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    gamemode: str = "survival"
    time_of_day: str = ""
    biome: str = ""
    inventory_summary: str = ""

    # Current goal / plan
    current_goal: str = ""
    current_plan: str = ""
    active_skill: str = ""

    # Nearby context
    entities_nearby: str = ""
    blocks_nearby: str = ""
    players_nearby: str = ""

    # Recalled long-term memory (injected as text snippets)
    recalled_memories: list[str] = Field(default_factory=list)

    # Metadata
    token_budget: int = 8000
