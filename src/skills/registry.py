"""
Phase 5a — SkillRegistry.

Loads skill definitions from .md files with YAML frontmatter, provides
keyword-based relevance search for code-generation mode.

    reg = SkillRegistry("src/skills/skills")
    await reg.load()
    docs = reg.get_relevant("craft a bed", limit=5)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class SkillDef:
    """One loaded skill definition."""

    def __init__(self, name: str, description: str, keywords: list[str], body: str) -> None:
        self.name = name
        self.description = description
        self.keywords = keywords
        self.body = body
        self._search_text = (name + " " + description + " " + " ".join(keywords)).lower()

    def relevance(self, query: str) -> float:
        """Simple keyword-overlap score against *query*."""
        q = query.lower()
        score = 0.0
        for word in q.split():
            if len(word) < 3:
                continue
            if word in self._search_text:
                score += 1.0
        return score

    def to_doc_string(self) -> str:
        """Format as a code-doc string for the LLM prompt."""
        return (
            f"## {self.name}\n"
            f"{self.description}\n\n"
            f"{self.body}\n"
        )


class SkillRegistry:
    """Load and query skill definitions from .md files."""

    def __init__(self, skills_dir: str | Path = "src/skills/skills") -> None:
        self._dir = Path(skills_dir)
        self._skills: list[SkillDef] = []
        self._loaded = False

    async def load(self) -> None:
        """Load all .md files from the skills directory."""
        if self._loaded:
            return
        if not self._dir.exists():
            logger.warning("Skills directory not found: %s", self._dir)
            self._loaded = True
            return

        for md_file in sorted(self._dir.glob("*.md")):
            try:
                skill = self._parse_skill(md_file)
                if skill:
                    self._skills.append(skill)
            except Exception:
                logger.exception("Failed to load skill: %s", md_file)

        self._loaded = True
        logger.info("SkillRegistry loaded: %d skills", len(self._skills))

    def get_relevant(self, query: str, limit: int = 5) -> str:
        """Return formatted skill docs most relevant to *query*."""
        if not query:
            return self.get_all(limit)

        scored = [(s.relevance(query), s) for s in self._skills]
        scored.sort(key=lambda x: x[0], reverse=True)

        parts: list[str] = []
        for score, skill in scored[:limit]:
            if score > 0 or limit <= 3:
                parts.append(skill.to_doc_string())
        return "\n---\n".join(parts) if parts else self.get_all(limit)

    def get_all(self, limit: int = -1) -> str:
        """Return all skill docs (or up to *limit*)."""
        skills = self._skills[:limit] if limit > 0 else self._skills
        return "\n---\n".join(s.to_doc_string() for s in skills)

    def get_by_name(self, name: str) -> SkillDef | None:
        for s in self._skills:
            if s.name == name:
                return s
        return None

    @property
    def count(self) -> int:
        return len(self._skills)

    # ── Parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_skill(path: Path) -> SkillDef | None:
        raw = path.read_text()
        m = _FRONTMATTER_RE.match(raw)
        if not m:
            # No frontmatter — use filename as name and full content as body
            name = path.stem
            return SkillDef(
                name=name,
                description="",
                keywords=[name],
                body=raw.strip(),
            )

        # Minimal YAML frontmatter parse
        meta: dict[str, str] = {}
        for line in m.group(1).splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()

        name = meta.get("name", path.stem)
        description = meta.get("description", "")
        keywords_raw = meta.get("keywords", name)
        keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]

        body = raw[m.end():].strip()
        return SkillDef(
            name=name,
            description=description,
            keywords=keywords,
            body=body,
        )
