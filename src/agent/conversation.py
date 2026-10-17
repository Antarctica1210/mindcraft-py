"""
Phase 2 — Conversation history manager.

Handles in-queue message buffering, turn compression, and
conversation lifecycle. Mirrors the JS `ConversationManager` and
`Conversation` classes from conversation.js.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .models import ConversationTurn

logger = logging.getLogger(__name__)

# Max turns before oldest are summarised / dropped
DEFAULT_MAX_TURNS = 40
# Turns to keep after compression (JS keeps ~9-14 turns for a 19-iter task;
# 20 gives enough room for multi-step tasks while preventing prompt bloat).
KEEP_RECENT = 20


@dataclass
class Conversation:
    """A single conversation with a player or system."""

    name: str
    active: bool = False
    in_queue: list[str] = field(default_factory=list)
    blocked: bool = False

    def queue(self, message: str) -> None:
        self.in_queue.append(message)

    def flush(self) -> str:
        """Return the concatenated queued messages and clear the queue."""
        msg = " ".join(self.in_queue)
        self.in_queue.clear()
        return msg

    def reset(self) -> None:
        self.active = False
        self.in_queue.clear()
        self.blocked = False


class ConversationManager:
    """Manages conversation state across multiple conversation partners.

    Mirrors the JS ``ConversationManager`` class.
    """

    def __init__(self, max_turns: int = DEFAULT_MAX_TURNS):
        self._convos: dict[str, Conversation] = {}
        self._active: str | None = None  # name of active conversation partner
        self.max_turns = max_turns

    # ── Public API ──────────────────────────────────────────────────

    @property
    def active_conversation(self) -> Conversation | None:
        if self._active is None:
            return None
        return self._convos.get(self._active)

    def get_or_create(self, name: str) -> Conversation:
        if name not in self._convos:
            self._convos[name] = Conversation(name=name)
        return self._convos[name]

    def start(self, name: str) -> Conversation:
        """Begin (or resume) a conversation with *name*."""
        convo = self.get_or_create(name)
        convo.active = True
        self._active = name
        return convo

    def end(self, name: str) -> None:
        """End a conversation and flush queued messages into history."""
        convo = self._convos.get(name)
        if convo is None:
            return
        convo.active = False
        if self._active == name:
            self._active = None

    def end_all(self) -> None:
        for convo in self._convos.values():
            convo.active = False
        self._active = None

    def is_active(self, name: str) -> bool:
        convo = self._convos.get(name)
        return convo is not None and convo.active

    # ── Turn compression ────────────────────────────────────────────

    def compress_history(
        self, history: list[ConversationTurn]
    ) -> list[ConversationTurn]:
        """Drop oldest turns when the history exceeds ``max_turns``.

        Keeps the first system message (if any) and the most recent
        ``KEEP_RECENT`` turns.
        """
        if len(history) <= self.max_turns:
            return history

        system_turns = [t for t in history if t.role == "system"][:1]
        recent = history[-(KEEP_RECENT):]
        return system_turns + recent

    def estimate_tokens(self, text: str) -> int:
        """Rough token count estimate (4 chars ≈ 1 token)."""
        return len(text) // 4
