"""
Phase 1 — Input intent classifier (InputRouter).

Classifies player messages into discrete intents BEFORE they reach the main
LLM, so the AgentLoop can dispatch to the right downstream path:

  - meta_command   → MemoryService write candidate (skips main LLM)
  - feedback       → feedback candidate + recent action trace
  - action_command → main LLM with tool_choice="auto"
  - question       → main LLM with tool_choice="none"
  - chitchat       → main LLM with tool_choice="none"
  - ambiguous      → AgentLoop asks player to clarify

Pure regex/keyword classifier — no LLM call, sub-millisecond. Keyword tables
live in ``config/intent_keywords.yaml`` and are hot-loadable. If the YAML
file or pyyaml is missing, the built-in defaults below are used.

See ``roadmap/05-python-refactor-plan.md`` Phase 1 for the design rationale.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class InputIntent(str, Enum):
    """Discrete intent categories for player input."""

    META_COMMAND = "meta_command"
    FEEDBACK = "feedback"
    ACTION_COMMAND = "action_command"
    QUESTION = "question"
    CHITCHAT = "chitchat"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RouterDecision:
    """Result of classification — passed to AgentLoop dispatcher."""

    intent: InputIntent
    matched_pattern: str | None = None  # which regex fired, for debugging
    confidence: float = 1.0             # 1.0 = regex hit, lower = heuristic


# Built-in fallback keywords (used when YAML config is missing).
_BUILTIN_KEYWORDS: dict[str, dict[str, list[str]]] = {
    "meta_command": {
        "en": [
            r"\bremember\b",
            r"\bforget\b",
            r"\bunforget\b",
            r"\bsave\s+skill\b",
            r"\bsave\s+route\b",
            r"\bprefer\b",
            r"\bavoid\b",
            r"\bmark\s+(this|that|here)\b",
        ],
        "zh": [
            "记住", "忘记", "记得", "别忘",
            "保存技能", "保存路线", "标记", "偏好",
        ],
    },
    "feedback": {
        "en": [
            r"\bwrong\b",
            r"\bdon'?t\b",
            r"\btoo risky\b",
            r"\btoo dangerous\b",
            r"\bnever\b",
            r"\bstop doing\b",
            r"\bbad idea\b",
            r"\bnot like that\b",
        ],
        "zh": [
            "不对", "错了", "别这样",
            "太冒险", "太危险",
            "不要那样", "不要这样",
        ],
    },
    "question": {
        "en": [
            r"\?",
            r"^(what|where|when|why|how|who|which)\b",
            r"^(is|are|can|do|does|will|could|would|should|did)\b",
        ],
        "zh": [
            "？",
            r"(什么|哪里|哪儿|何时|为什么|怎么|谁|哪个)",
            r"(是不是|有没有|能否|是吗|吗\?|吗$)",
        ],
    },
    "action_verb": {
        "en": [
            r"^\s*(go|move|come|collect|gather|get|bring|fetch|mine|dig|break|"
            r"craft|build|make|attack|kill|fight|equip|wear|use|eat|drink|"
            r"consume|place|put|drop|follow|escort|find|search|give|hand|"
            r"take|grab|stop|stay|wait|sleep|rest)\b",
        ],
        "zh": [
            # Multi-char verbs and prefixes
            r"^(去|走到|过来|跟我来|采集|拾取|开采|破坏|合成|制作|建造|"
            r"攻击|装备|跟随|跟着|护送|寻找|搜索|放置|停下|不动|休息)",
            # Single-char verbs followed by a quantity / object (the common
            # Chinese imperative pattern, e.g. "挖10个橡木", "采5朵花").
            r"^(挖|采|砍|捡|打|杀|拿|放|找|搜|看|说|给|做|建|造|烧|喂|吃|喝)",
        ],
    },
}

# Greetings / acks / single-word affirmatives → chitchat (default LLM, no tools).
_CHITCHAT_PATTERNS = [
    r"^(hi|hello|hey|yo|sup|hola|你好|嗨|哈喽)[\s\.\,\!]*$",
    r"^(bye|goodbye|cya|再见|拜拜)[\s\.\,\!]*$",
    r"^(thanks?|thx|thank\s+you|谢谢|多谢)[\s\.\,\!]*$",
    r"^(ok|okay|cool|nice|好|好的|嗯|哦)[\s\.\,\!]*$",
    r"^(lol|haha|hehe|哈+|嘿+)[\s\.\,\!]*$",
]


@dataclass
class _CompiledTables:
    """Pre-compiled regex tables for fast matching."""

    meta_command: list[tuple[str, re.Pattern[str]]]
    feedback: list[tuple[str, re.Pattern[str]]]
    question: list[tuple[str, re.Pattern[str]]]
    action_verb: list[tuple[str, re.Pattern[str]]]
    chitchat: list[tuple[str, re.Pattern[str]]]


class InputRouter:
    """Classify player messages by intent — regex/keyword only.

    Priority order (first match wins):

      1. ``meta_command``    (highest — overrides everything)
      2. ``feedback``
      3. ``question``        (interrogative markers / question mark)
      4. ``action_command``  (action verb at start of message)
      5. ``chitchat``        (greetings, thanks, short acks)
      6. ``ambiguous``       (default — short tokenless input)

    Usage::

        router = InputRouter.from_config("config/intent_keywords.yaml")
        decision = router.classify("挖10个橡木")
        # → RouterDecision(intent=ACTION_COMMAND, matched_pattern="^(去|采集|...)")
    """

    def __init__(self, keywords: dict[str, dict[str, list[str]]] | None = None):
        kw = keywords or _BUILTIN_KEYWORDS
        self._tables = _CompiledTables(
            meta_command=self._compile_section(kw.get("meta_command", {})),
            feedback=self._compile_section(kw.get("feedback", {})),
            question=self._compile_section(kw.get("question", {})),
            action_verb=self._compile_section(kw.get("action_verb", {})),
            chitchat=[
                (p, re.compile(p, re.IGNORECASE)) for p in _CHITCHAT_PATTERNS
            ],
        )

    # ── Factory ────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, path: str | Path) -> InputRouter:
        """Load keyword tables from YAML; fall back to built-ins on any error.

        Does NOT require pyyaml to be installed — missing dependency or
        file silently falls back to defaults.
        """
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            logger.info("InputRouter: pyyaml not installed, using built-in keywords")
            return cls()

        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return cls(keywords=data)
        except FileNotFoundError:
            logger.info(
                "InputRouter: config %s not found, using built-in keywords", path,
            )
            return cls()
        except Exception:
            logger.exception(
                "InputRouter: failed to load %s, falling back to built-ins", path,
            )
            return cls()

    # ── Classification ─────────────────────────────────────────────

    def classify(self, message: str) -> RouterDecision:
        """Return the classified intent for ``message``.

        - Empty / whitespace-only → ``CHITCHAT`` (low confidence)
        - Short single token without verb → ``AMBIGUOUS``
        - Otherwise: first-match priority order above
        """
        text = (message or "").strip()
        if not text:
            return RouterDecision(intent=InputIntent.CHITCHAT, confidence=0.5)

        # 1. meta_command — highest priority
        if (m := self._first_match(text, self._tables.meta_command)):
            return RouterDecision(
                intent=InputIntent.META_COMMAND, matched_pattern=m,
            )

        # 2. feedback
        if (m := self._first_match(text, self._tables.feedback)):
            return RouterDecision(intent=InputIntent.FEEDBACK, matched_pattern=m)

        # 3. question (markers are strong signal)
        if (m := self._first_match(text, self._tables.question)):
            return RouterDecision(intent=InputIntent.QUESTION, matched_pattern=m)

        # 4. action verb at start
        if (m := self._first_match(text, self._tables.action_verb)):
            return RouterDecision(
                intent=InputIntent.ACTION_COMMAND, matched_pattern=m,
            )

        # 5. chitchat (greetings, thanks, acks)
        if (m := self._first_match(text, self._tables.chitchat)):
            return RouterDecision(
                intent=InputIntent.CHITCHAT, matched_pattern=m, confidence=0.8,
            )

        # 6. Heuristics for ambiguity:
        #    - 1-token AND short → ambiguous (e.g. "base", "mine")
        #    - Otherwise treat as chitchat (LLM no-tools)
        tokens = text.split()
        cjk_chars = sum(1 for c in text if "一" <= c <= "鿿")
        if len(tokens) <= 1 and len(text) <= 6 and cjk_chars <= 2:
            return RouterDecision(intent=InputIntent.AMBIGUOUS, confidence=0.6)

        return RouterDecision(intent=InputIntent.CHITCHAT, confidence=0.4)

    # ── Internal ───────────────────────────────────────────────────

    @staticmethod
    def _compile_section(
        section: dict[str, Iterable[str]],
    ) -> list[tuple[str, re.Pattern[str]]]:
        """Compile ``{lang: [patterns]}`` into ordered ``(pattern, compiled)``."""
        compiled: list[tuple[str, re.Pattern[str]]] = []
        for _lang, patterns in (section or {}).items():
            for p in patterns:
                try:
                    compiled.append((p, re.compile(p, re.IGNORECASE)))
                except re.error:
                    logger.warning("InputRouter: invalid regex %r, skipped", p)
        return compiled

    @staticmethod
    def _first_match(
        text: str, table: list[tuple[str, re.Pattern[str]]],
    ) -> str | None:
        for pattern_str, regex in table:
            if regex.search(text):
                return pattern_str
        return None
