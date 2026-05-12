"""Phase 5a scope checks for workflow skills."""
from __future__ import annotations

import re

from .loader import SkillFile

_COORD_RE = re.compile(
    r"(?<!\w)(?:x\s*[:=]\s*-?\d+(?:\.\d+)?|"
    r"y\s*[:=]\s*-?\d+(?:\.\d+)?|"
    r"z\s*[:=]\s*-?\d+(?:\.\d+)?|"
    r"\(-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?\))",
    re.IGNORECASE,
)
_WORLD_WORD_RE = re.compile(r"\b(base|home|chest|village|portal|mine|farm)\b", re.IGNORECASE)


class ScopeValidationError(ValueError):
    """Raised when a skill contains information outside its declared scope."""


class ScopeValidator:
    """Reject cross-world facts in universal bot skills."""

    def validate(self, skill: SkillFile) -> None:
        text = "\n".join([
            skill.name,
            skill.description,
            " ".join(skill.triggers),
            " ".join(skill.requires),
            skill.body,
        ])
        if skill.scope == "bot" and _COORD_RE.search(text):
            raise ScopeValidationError(
                f"bot-scope skill '{skill.name}' contains world coordinates"
            )
        if skill.scope == "minecraft" and _WORLD_WORD_RE.search(text):
            raise ScopeValidationError(
                f"minecraft-scope skill '{skill.name}' appears to contain world facts"
            )
