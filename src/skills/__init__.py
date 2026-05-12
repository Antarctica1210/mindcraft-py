"""
Phase 5a — File-based skill registry and workflow matcher.

Skills are markdown files with YAML frontmatter stored in ``skills/``.
The ``SkillRegistry`` loads them and provides keyword-based lookup for
the MessageBuilder's ``$CODE_DOCS`` placeholder in coding mode.
"""

from .loader import SkillFile, SkillStep, SkillStore
from .matcher import SkillMatch, SkillMatcher
from .registry import SkillRegistry

__all__ = [
    "SkillFile",
    "SkillMatch",
    "SkillMatcher",
    "SkillRegistry",
    "SkillStep",
    "SkillStore",
]
