"""
Phase 3 — Working memory aggregation.

``WorkingMemoryBuilder`` collects the current L0 context (conversation window,
bot snapshot, goal/skill, nearby entities) and produces a ``WorkingContext``
that ``MessageBuilder`` injects into the LLM prompt each turn.
"""

from __future__ import annotations

from .models import WorkingContext


class WorkingMemoryBuilder:
    """Aggregate L0 working context from live agent state."""

    def __init__(self, token_budget: int = 8000) -> None:
        self._token_budget = token_budget
        self._conversation: list[str] = []
        self._recalled: list[str] = []

    # ═══════════════════════════════════════════════════════════════
    #  Setters — called by AgentLoop each tick
    # ═══════════════════════════════════════════════════════════════

    def set_conversation(self, turns: list[str]) -> None:
        """Set the L0 ring-buffer conversation window."""
        self._conversation = list(turns)

    def append_conversation(self, turn: str) -> None:
        self._conversation.append(turn)

    def set_recalled(self, memories: list[str]) -> None:
        self._recalled = list(memories)

    # ═══════════════════════════════════════════════════════════════
    #  Build
    # ═══════════════════════════════════════════════════════════════

    def build(
        self,
        health: float = 20.0,
        food: float = 20.0,
        position: tuple[float, float, float] = (0.0, 0.0, 0.0),
        gamemode: str = "survival",
        time_of_day: str = "",
        biome: str = "",
        inventory_summary: str = "",
        current_goal: str = "",
        current_plan: str = "",
        active_skill: str = "",
        entities_nearby: str = "",
        blocks_nearby: str = "",
        players_nearby: str = "",
    ) -> WorkingContext:
        return WorkingContext(
            recent_conversation=list(self._conversation),
            health=health,
            food=food,
            position=position,
            gamemode=gamemode,
            time_of_day=time_of_day,
            biome=biome,
            inventory_summary=inventory_summary,
            current_goal=current_goal,
            current_plan=current_plan,
            active_skill=active_skill,
            entities_nearby=entities_nearby,
            blocks_nearby=blocks_nearby,
            players_nearby=players_nearby,
            recalled_memories=list(self._recalled),
            token_budget=self._token_budget,
        )
