"""
Phase 1 — Few-shot example retrieval for the conversing prompt.

Mirrors the JS mindcraft pattern: the system prompt's ``$EXAMPLES``
placeholder is filled per-turn with the top-N most-relevant conversation
examples. JS does this via embedding similarity (``utils/examples.js``);
this module does it via keyword overlap as a cheap stand-in until Phase
4b FAISS retrieval lands. Drop-in swappable later — the public API is
just :py:meth:`ExampleStore.select`.

JSON corpus shape (``config/conversation_examples.json``)::

    [
      {
        "name": "craft_a_plank",
        "keywords": ["craft", "plank", "wood"],
        "turns": [{"role": "user", "content": "..."}, ...]
      },
      ...
    ]
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .message_builder import ConversationTurn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Example:
    """One conversation example loaded from the JSON corpus."""

    name: str
    keywords: list[str]
    turns: list[ConversationTurn]
    _search_text: str = field(default="", repr=False)

    def relevance(self, query: str) -> float:
        """Keyword-overlap score against ``query``.

        Counts how many query words (length ≥3, lowercased) appear in
        the example's name + keywords blob. Ties broken by ordering of
        the JSON file (stable).
        """
        if not query:
            return 0.0
        q = query.lower()
        score = 0.0
        for keyword in self.keywords:
            if len(keyword) >= 4 and keyword in q:
                score += 3.0
        for word in q.split():
            if len(word) < 3:
                continue
            if word in self._search_text:
                score += 1.0
        return score


class ExampleStore:
    """Load and rank conversation examples for the ``$EXAMPLES`` slot.

    Usage::

        store = ExampleStore.from_config("config/conversation_examples.json")
        store.load()
        examples = store.select("craft a bed", limit=2)
        text = MessageBuilder.format_examples(examples)
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._examples: list[Example] = []
        self._loaded = False

    # ── Factory ────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, path: str | Path) -> ExampleStore:
        return cls(path=path)

    # ── Loading ────────────────────────────────────────────────────

    def load(self) -> None:
        """Read the JSON corpus. Silently no-ops if the file is missing.

        Failures here must not crash the agent — the conversing prompt
        works (worse) with an empty examples slot.
        """
        if self._loaded:
            return
        self._loaded = True

        if self._path is None or not self._path.exists():
            logger.info(
                "ExampleStore: corpus %s not found, $EXAMPLES will be empty",
                self._path,
            )
            return

        try:
            with self._path.open(encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            logger.exception(
                "ExampleStore: failed to load %s, $EXAMPLES will be empty",
                self._path,
            )
            return

        if not isinstance(raw, list):
            logger.warning(
                "ExampleStore: %s is not a JSON list, ignoring", self._path,
            )
            return

        for entry in raw:
            try:
                ex = self._parse_entry(entry)
                if ex is not None:
                    self._examples.append(ex)
            except Exception:
                logger.exception(
                    "ExampleStore: failed to parse entry %r", entry.get("name"),
                )

        logger.info(
            "ExampleStore: loaded %d examples from %s",
            len(self._examples), self._path,
        )

    # ── Selection ──────────────────────────────────────────────────

    def select(
        self, query: str, limit: int = 2,
    ) -> list[list[ConversationTurn]]:
        """Return the top-``limit`` examples ranked by keyword overlap.

        If no example scores above zero, falls back to the first
        ``limit`` examples in the corpus (so the slot is never empty
        when examples exist).

        Returns:
            A list of conversation-turn lists, ready to hand to
            :py:meth:`MessageBuilder.format_examples`.
        """
        if not self._examples:
            return []

        scored = [(ex.relevance(query), ex) for ex in self._examples]
        scored.sort(key=lambda x: x[0], reverse=True)

        hits = [ex for score, ex in scored if score > 0][:limit]
        if not hits:
            hits = [ex for _, ex in scored[:limit]]

        return [ex.turns for ex in hits]

    @property
    def count(self) -> int:
        return len(self._examples)

    # ── Parsing ────────────────────────────────────────────────────

    @staticmethod
    def _parse_entry(entry: dict) -> Example | None:
        if not isinstance(entry, dict):
            return None
        name = str(entry.get("name", "")).strip()
        if not name:
            return None
        keywords = [
            str(k).strip().lower()
            for k in entry.get("keywords", [])
            if isinstance(k, (str, int, float))
        ]
        turns_raw = entry.get("turns", [])
        turns: list[ConversationTurn] = []
        for t in turns_raw:
            if not isinstance(t, dict):
                continue
            role = str(t.get("role", "user"))
            content = str(t.get("content", ""))
            turns.append(ConversationTurn(role=role, content=content))
        if not turns:
            return None
        search_text = (
            name.lower() + " " + " ".join(keywords) + " "
            + " ".join(t.content.lower() for t in turns)
        )
        return Example(
            name=name,
            keywords=keywords,
            turns=turns,
            _search_text=search_text,
        )
