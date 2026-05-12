"""
Phase 5a — File-based skill registry.

Skills are markdown files with YAML frontmatter stored in ``skills/``.
The ``SkillRegistry`` loads them and provides keyword-based lookup for
the MessageBuilder's ``$CODE_DOCS`` placeholder in coding mode.
"""

from .registry import SkillRegistry

__all__ = ["SkillRegistry"]
