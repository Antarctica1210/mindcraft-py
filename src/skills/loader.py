"""
Phase 5a — executable workflow skill loading.

These skills are distinct from ``src/skills/registry.py``.  The registry keeps
API reference docs for code-generation prompts; this loader reads action-mode
workflow skills from bot/world directories and optional procedural memory.
"""
from __future__ import annotations

import ast
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_COMMAND_RE = re.compile(r"^!?(?P<name>[A-Za-z_][\w]*)\s*(?:\((?P<args>.*)\))?$")


class SkillStep(BaseModel):
    """One executable action step in a workflow skill."""

    command: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class FailureMode(BaseModel):
    """A known failure branch for a skill."""

    pattern: str
    next_skill: str | None = None
    hint: str = ""


class SkillFile(BaseModel):
    """Phase 5a skill schema stored as YAML frontmatter + markdown body."""

    name: str
    description: str = ""
    scope: str = Field(default="bot", pattern="^(bot|world|minecraft)$")
    triggers: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    success_count: int = Field(default=0, ge=0)
    fail_count: int = Field(default=0, ge=0)
    tags: list[str] = Field(default_factory=list)
    body: str = ""
    source_path: Path | None = None
    steps: list[SkillStep] = Field(default_factory=list)
    failure_modes: list[FailureMode] = Field(default_factory=list)
    updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = {"arbitrary_types_allowed": True}

    @field_validator("triggers", "requires", "tags", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [str(value).strip()]

    @field_validator("scope")
    @classmethod
    def _normalize_scope(cls, value: str) -> str:
        return value.strip().lower()

    @property
    def search_text(self) -> str:
        return " ".join([self.name, self.description, *self.triggers, *self.tags]).lower()

    def update_outcome(self, *, success: bool) -> None:
        if success:
            self.success_count += 1
        else:
            self.fail_count += 1
        total = self.success_count + self.fail_count
        observed = self.success_count / total if total else self.confidence
        # Keep hand-authored priors meaningful while allowing repeated traces
        # to pull confidence up/down over time.
        self.confidence = max(0.05, min(0.99, (self.confidence * 0.6) + (observed * 0.4)))
        self.updated = datetime.now(timezone.utc)


def parse_skill_file(path: Path) -> SkillFile | None:
    raw = path.read_text()
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        logger.warning("Skipping workflow skill without frontmatter: %s", path)
        return None

    meta = _parse_yaml(match.group(1))
    body = raw[match.end():].strip()
    skill = SkillFile(
        name=str(meta.get("name", path.stem)),
        description=str(meta.get("description", "")),
        scope=str(meta.get("scope", "bot")),
        triggers=meta.get("triggers", []),
        requires=meta.get("requires", []),
        confidence=float(meta.get("confidence", 0.5)),
        success_count=int(meta.get("success_count", 0)),
        fail_count=int(meta.get("fail_count", 0)),
        tags=meta.get("tags", []),
        body=body,
        source_path=path,
    )
    sections = _split_sections(body)
    skill.steps = _parse_steps(sections.get("执行步骤", sections.get("steps", "")))
    skill.failure_modes = _parse_failure_modes(
        sections.get("失败模式", sections.get("failure modes", ""))
    )
    return skill


class ProceduralMemoryAdapter:
    """Recall skill memories through Phase 3 MemoryService."""

    def __init__(self, memory: Any | None) -> None:
        self._memory = memory

    async def recall(self, query: str = "", *, limit: int = 20) -> list[SkillFile]:
        if self._memory is None:
            return []

        from memory.models import MemoryQuery, MemoryType

        keywords = [w for w in re.findall(r"[A-Za-z_]{3,}", query.lower())]
        results = await self._memory.recall(
            MemoryQuery(type=MemoryType.SKILL, keywords=keywords, limit=limit)
        )
        skills: list[SkillFile] = []
        for result in results:
            record = result.record
            try:
                skill = SkillFile(
                    name=record.name,
                    description=record.description,
                    scope=record.scope.value,
                    triggers=record.tags,
                    requires=[],
                    confidence=record.confidence,
                    tags=record.tags,
                    body=record.body,
                    source_path=None,
                )
                sections = _split_sections(record.body)
                skill.steps = _parse_steps(sections.get("执行步骤", sections.get("steps", "")))
                skill.failure_modes = _parse_failure_modes(
                    sections.get("失败模式", sections.get("failure modes", ""))
                )
                skills.append(skill)
            except Exception:
                logger.exception("Failed to parse procedural skill memory: %s", record.name)
        return skills


class SkillStore:
    """Loads workflow skills with world-over-bot routing."""

    def __init__(
        self,
        *,
        bot_name: str = "default",
        world_id: str = "default",
        root_dir: str | Path = "bots",
        memory: Any | None = None,
    ) -> None:
        self.bot_name = bot_name
        self.world_id = world_id
        self.root_dir = Path(root_dir)
        self.memory = ProceduralMemoryAdapter(memory)
        self._skills: list[SkillFile] = []

    @classmethod
    def for_bot(
        cls,
        bot_name: str,
        *,
        world_id: str = "default",
        root_dir: str | Path = "bots",
        memory: Any | None = None,
    ) -> SkillStore:
        return cls(bot_name=bot_name, world_id=world_id, root_dir=root_dir, memory=memory)

    @property
    def skills(self) -> list[SkillFile]:
        return list(self._skills)

    async def load(self, *, query: str = "") -> None:
        self._skills = []
        for directory in self._skill_dirs():
            if not directory.exists():
                continue
            for md_file in sorted(directory.glob("*.md")):
                try:
                    skill = parse_skill_file(md_file)
                    if skill is not None:
                        self._skills.append(skill)
                except Exception:
                    logger.exception("Failed to load workflow skill: %s", md_file)
        self._skills.extend(await self.memory.recall(query=query))
        logger.info("SkillStore loaded: %d workflow skills", len(self._skills))

    async def record_outcome(self, skill: SkillFile, *, success: bool) -> None:
        skill.update_outcome(success=success)
        if skill.source_path is None:
            return
        skill.source_path.write_text(serialize_skill_file(skill))

    def _skill_dirs(self) -> list[Path]:
        base = self.root_dir / self.bot_name
        # World first: MemoryService recall uses the same priority, and this
        # lets a world-specific skill shadow a universal habit in tie-breaks.
        return [
            base / "worlds" / self.world_id / "skills",
            base / "universal" / "skills",
        ]


def serialize_skill_file(skill: SkillFile) -> str:
    data = {
        "name": skill.name,
        "description": skill.description,
        "scope": skill.scope,
        "triggers": skill.triggers,
        "requires": skill.requires,
        "confidence": round(skill.confidence, 4),
        "success_count": skill.success_count,
        "fail_count": skill.fail_count,
        "tags": skill.tags,
        "updated": skill.updated.isoformat(),
    }
    frontmatter = "\n".join(f"{key}: {_yaml_value(value)}" for key, value in data.items())
    return f"---\n{frontmatter}\n---\n\n{skill.body.strip()}\n"


def _split_sections(body: str) -> dict[str, str]:
    matches = list(_SECTION_RE.finditer(body))
    if not matches:
        return {}
    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[match.group(1).strip().lower()] = body[start:end].strip()
    return sections


def _parse_steps(text: str) -> list[SkillStep]:
    steps: list[SkillStep] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw.startswith(("-", "*", "1.")):
            continue
        raw = raw.lstrip("-*0123456789. ").strip()
        match = _COMMAND_RE.match(raw)
        if not match:
            continue
        kwargs: dict[str, Any] = {}
        args: list[Any] = []
        for part in _split_args(match.group("args") or ""):
            if "=" in part:
                key, _, value = part.partition("=")
                kwargs[key.strip()] = _parse_scalar(value.strip())
            elif part:
                args.append(_parse_scalar(part))
        steps.append(SkillStep(command=match.group("name"), args=args, kwargs=kwargs))
    return steps


def _parse_failure_modes(text: str) -> list[FailureMode]:
    modes: list[FailureMode] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw.startswith(("-", "*")):
            continue
        raw = raw.lstrip("-* ").strip()
        parts = [p.strip() for p in raw.split("|")]
        if not parts or not parts[0]:
            continue
        modes.append(FailureMode(
            pattern=parts[0],
            next_skill=parts[1] if len(parts) > 1 and parts[1] else None,
            hint=parts[2] if len(parts) > 2 else "",
        ))
    return modes


def _split_args(text: str) -> list[str]:
    if not text.strip():
        return []
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for char in text:
        if char in {"'", '"'}:
            quote = None if quote == char else char if quote is None else quote
        if char == "," and quote is None:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(char)
    if buf:
        parts.append("".join(buf).strip())
    return parts


def _parse_scalar(value: str) -> Any:
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.lower() in {"none", "null"}:
        return None
    try:
        return ast.literal_eval(value)
    except Exception:
        return value.strip("\"'")


def _parse_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_items: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith("  - ") and current_key is not None:
            current_items.append(line[4:].strip())
            result[current_key] = current_items
            continue
        current_key = None
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()
        if raw == "":
            current_key = key
            current_items = []
            result[key] = current_items
        else:
            result[key] = _parse_yaml_scalar(raw)
    return result


def _parse_yaml_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_parse_scalar(p) for p in _split_args(inner)] if inner else []
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    if raw.lower() in {"null", "none", "~"}:
        return None
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw.strip("\"'")


def _yaml_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_value(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text or any(c in text for c in [":", "#", "[", "]", ",", "\n"]):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text
