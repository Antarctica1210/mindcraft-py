"""Phase 5a workflow skill matcher."""
from __future__ import annotations

import logging
import operator
import re
from dataclasses import dataclass
from typing import Any

from .loader import SkillFile, SkillStore
from .scope_validator import ScopeValidationError, ScopeValidator

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-z_]{3,}")
_COMPARE_RE = re.compile(r"^(?P<field>[\w.]+)\s*(?P<op>>=|<=|==|!=|>|<)\s*(?P<value>.+)$")
_OPS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


@dataclass(frozen=True)
class SkillMatch:
    skill: SkillFile
    score: float
    matched_triggers: list[str]
    reason: str = ""


class SkillMatcher:
    """Select executable skills from situation text and BotState."""

    def __init__(self, store: SkillStore, *, validator: ScopeValidator | None = None) -> None:
        self.store = store
        self.validator = validator or ScopeValidator()

    async def match(
        self,
        situation: str,
        state: Any,
        *,
        min_score: float = 0.9,
    ) -> SkillMatch | None:
        scored = self.rank(situation, state)
        if not scored:
            return None
        best = scored[0]
        return best if best.score >= min_score else None

    def rank(self, situation: str, state: Any) -> list[SkillMatch]:
        terms = set(_WORD_RE.findall(situation.lower()))
        matches: list[SkillMatch] = []
        for skill in self.store.skills:
            try:
                self.validator.validate(skill)
            except ScopeValidationError as exc:
                logger.warning("Skipping invalid skill scope: %s", exc)
                continue
            if not skill.steps:
                continue
            if not self._requirements_met(skill, state):
                continue

            matched: list[str] = []
            trigger_score = 0.0
            for trigger in skill.triggers:
                trigger_terms = set(_WORD_RE.findall(trigger.lower()))
                if not trigger_terms:
                    continue
                overlap = terms & trigger_terms
                if trigger.lower() in situation.lower() or overlap == trigger_terms:
                    matched.append(trigger)
                    trigger_score += 1.0
                elif overlap:
                    trigger_score += len(overlap) / len(trigger_terms) * 0.45

            name_terms = set(_WORD_RE.findall(skill.name.lower().replace("_", " ")))
            if terms & name_terms:
                trigger_score += 0.25

            if trigger_score <= 0:
                continue
            # Strong trigger match + learned confidence.  The cap keeps a
            # vague one-word overlap from bypassing the LLM.
            normalized = 1.0 if matched else min(
                1.0, trigger_score / max(1, min(2, len(skill.triggers)))
            )
            score = (normalized * 0.65) + (skill.confidence * 0.35)
            scope_bonus = 0.03 if skill.scope == "world" else 0.0
            matches.append(SkillMatch(
                skill=skill,
                score=min(1.0, score + scope_bonus),
                matched_triggers=matched,
                reason="trigger+confidence",
            ))
        matches.sort(key=lambda m: (m.score, m.skill.scope == "world"), reverse=True)
        return matches

    def _requirements_met(self, skill: SkillFile, state: Any) -> bool:
        return all(_eval_condition(req, state) for req in skill.requires)


def _eval_condition(condition: str, state: Any) -> bool:
    cond = condition.strip()
    if not cond or cond.lower() in {"true", "always"}:
        return True
    if cond.lower().startswith("not "):
        return not _eval_condition(cond[4:], state)

    lowered = cond.lower()
    if lowered.startswith(("inventory has ", "inventory 有 ")):
        item = cond.split(maxsplit=2)[-1].strip()
        return _inventory_count(state, item) > 0
    if lowered.startswith(("nearby has ", "nearby_block has ", "nearby 有 ")):
        block = cond.split(maxsplit=2)[-1].strip()
        return block in {str(b) for b in getattr(state, "nearby_block_types", [])}

    match = _COMPARE_RE.match(cond)
    if match:
        left = _state_value(state, match.group("field"))
        right = _parse_value(match.group("value"))
        op = _OPS[match.group("op")]
        try:
            return bool(op(left, right))
        except TypeError:
            return bool(op(str(left), str(right)))

    logger.debug("Unknown skill condition %r treated as false", condition)
    return False


def _inventory_count(state: Any, item_name: str) -> int:
    for item in getattr(state, "inventory", []):
        if getattr(item, "name", "") == item_name:
            return int(getattr(item, "count", 0))
    return 0


def _state_value(state: Any, field: str) -> Any:
    value: Any = state
    for part in field.split("."):
        if part == "pos":
            part = "position"
        value = getattr(value, part)
    return value


def _parse_value(value: str) -> Any:
    raw = value.strip().strip("\"'")
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw
