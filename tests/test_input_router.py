"""Unit tests for :mod:`llm.input_router`.

Covers the six intent categories with bilingual (en / zh) cases and
verifies the priority-order semantics (meta > feedback > question >
action > chitchat > ambiguous).
"""
from __future__ import annotations

import pytest

from llm.input_router import (
    InputIntent,
    InputRouter,
    PlanningStrategy,
    RouterDecision,
)

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def router() -> InputRouter:
    """A single router using the built-in keyword tables.

    Built-ins are used (not the YAML config) so tests are deterministic
    even when ``config/intent_keywords.yaml`` is edited.
    """
    return InputRouter()


# ── Parametrised classification ─────────────────────────────────────

# (message, expected_intent) — covers all six intents in both languages.
CASES: list[tuple[str, InputIntent]] = [
    # meta_command
    ("remember this is my base", InputIntent.META_COMMAND),
    ("forget diamond_mine", InputIntent.META_COMMAND),
    ("save skill safe_tunnel", InputIntent.META_COMMAND),
    ("save route to nether", InputIntent.META_COMMAND),
    ("avoid the lava field", InputIntent.META_COMMAND),
    ("记住这是基地", InputIntent.META_COMMAND),
    ("忘记钻石矿", InputIntent.META_COMMAND),
    ("保存技能 mine_diamonds", InputIntent.META_COMMAND),
    # feedback
    ("wrong, don't do that", InputIntent.FEEDBACK),
    ("that was too risky", InputIntent.FEEDBACK),
    ("never dig straight down", InputIntent.FEEDBACK),
    ("不对", InputIntent.FEEDBACK),
    ("太危险", InputIntent.FEEDBACK),
    ("别这样", InputIntent.FEEDBACK),
    # question
    ("where is the base?", InputIntent.QUESTION),
    ("what time is it", InputIntent.QUESTION),
    ("how do I craft a pickaxe", InputIntent.QUESTION),
    ("can you find a cow", InputIntent.QUESTION),
    ("你在哪里", InputIntent.QUESTION),
    ("现在几点？", InputIntent.QUESTION),
    ("有没有钻石", InputIntent.QUESTION),
    # action_command
    ("go to spawn", InputIntent.ACTION_COMMAND),
    ("collect 10 oak_log", InputIntent.ACTION_COMMAND),
    ("attack the zombie", InputIntent.ACTION_COMMAND),
    ("follow me", InputIntent.ACTION_COMMAND),
    ("I want to eat some pork", InputIntent.ACTION_COMMAND),
    ("I need to get wood", InputIntent.ACTION_COMMAND),
    ("mine", InputIntent.ACTION_COMMAND),  # single action verb
    ("挖10个橡木", InputIntent.ACTION_COMMAND),
    ("采5朵花", InputIntent.ACTION_COMMAND),
    ("攻击僵尸", InputIntent.ACTION_COMMAND),
    ("砍树", InputIntent.ACTION_COMMAND),
    ("跟我来", InputIntent.ACTION_COMMAND),
    # chitchat
    ("hi", InputIntent.CHITCHAT),
    ("hello", InputIntent.CHITCHAT),
    ("thanks", InputIntent.CHITCHAT),
    ("ok", InputIntent.CHITCHAT),
    ("lol", InputIntent.CHITCHAT),
    ("你好", InputIntent.CHITCHAT),
    ("谢谢", InputIntent.CHITCHAT),
    ("好的", InputIntent.CHITCHAT),
    # ambiguous (short tokenless input)
    ("base", InputIntent.AMBIGUOUS),
    ("矿", InputIntent.AMBIGUOUS),
    ("xx", InputIntent.AMBIGUOUS),
]


@pytest.mark.parametrize(("message", "expected"), CASES)
def test_classify_intent(
    router: InputRouter, message: str, expected: InputIntent,
) -> None:
    decision = router.classify(message)
    assert isinstance(decision, RouterDecision)
    assert decision.intent is expected, (
        f"{message!r} → {decision.intent.value} "
        f"(expected {expected.value}, matched={decision.matched_pattern!r})"
    )


# ── Priority semantics ──────────────────────────────────────────────


def test_meta_overrides_action(router: InputRouter) -> None:
    """`save skill ...` includes an action-shaped verb but stays meta."""
    decision = router.classify("save skill safe_tunnel")
    assert decision.intent is InputIntent.META_COMMAND


def test_feedback_overrides_question(router: InputRouter) -> None:
    """`wrong, how?` could be a question; feedback wins (it appears first)."""
    decision = router.classify("wrong, how could you do that?")
    assert decision.intent is InputIntent.FEEDBACK


def test_question_with_action_verb(router: InputRouter) -> None:
    """`can you go ...?` → question, not action_command."""
    decision = router.classify("can you go to spawn?")
    assert decision.intent is InputIntent.QUESTION


# ── Empty / edge cases ──────────────────────────────────────────────


def test_empty_message_is_chitchat(router: InputRouter) -> None:
    assert router.classify("").intent is InputIntent.CHITCHAT
    assert router.classify("   ").intent is InputIntent.CHITCHAT


def test_whitespace_only_returns_low_confidence(router: InputRouter) -> None:
    decision = router.classify("")
    assert decision.confidence < 1.0


def test_decision_carries_matched_pattern_on_regex_hit(
    router: InputRouter,
) -> None:
    decision = router.classify("remember this is my base")
    assert decision.matched_pattern is not None


# ── Custom keyword tables ───────────────────────────────────────────


def test_custom_keywords_override_builtins() -> None:
    """A user-supplied keyword table is used verbatim."""
    custom = {
        "meta_command": {"en": [r"\bsnapshot\b"]},
        "feedback": {"en": []},
        "question": {"en": [r"\?"]},
        "action_verb": {"en": []},
    }
    r = InputRouter(keywords=custom)
    assert r.classify("snapshot the world").intent is InputIntent.META_COMMAND
    # action verb table is empty, so `mine` should NOT be action_command
    assert r.classify("mine").intent is InputIntent.AMBIGUOUS


def test_from_config_falls_back_when_file_missing(tmp_path: object) -> None:
    """Missing config file → built-ins still work."""
    missing = "/tmp/does_not_exist_intent_keywords.yaml"
    r = InputRouter.from_config(missing)
    # Built-in defaults still classify standard cases.
    assert r.classify("remember base").intent is InputIntent.META_COMMAND


@pytest.mark.parametrize(
    "message",
    [
        "craft a furnace",
        "smelt 4 raw iron",
        "breed two chickens",
        "种小麦",
        "养鸡",
    ],
)
def test_action_workflows_default_to_react(
    router: InputRouter,
    message: str,
) -> None:
    decision = router.classify(message)
    assert decision.intent is InputIntent.ACTION_COMMAND
    assert decision.planning_strategy is PlanningStrategy.REACT


@pytest.mark.parametrize(
    "message",
    [
        "attack the zombie",
        "follow me",
        "search for a chicken",
        "stop",
        "攻击僵尸",
    ],
)
def test_dynamic_tasks_use_react(router: InputRouter, message: str) -> None:
    decision = router.classify(message)
    assert decision.intent is InputIntent.ACTION_COMMAND
    assert decision.planning_strategy is PlanningStrategy.REACT
