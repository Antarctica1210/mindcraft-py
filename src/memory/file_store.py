"""
Phase 4a — File-based memory store.

Implements the ``MemoryStore`` ABC using YAML frontmatter + markdown files
in ``bots/<name>/memory/<type>/<name>.md``, with a ``MEMORY.md`` index file.

    store = FileMemoryStore("bots/andy/memory")
    await store.put(record)       # → writes place/base.md
    record = await store.get("base", type="place")  # → MemoryRecord
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .index import MemoryIndex
from .models import MemoryQuery, MemoryRecord, MemoryResult, Scope
from .store import MemoryStore

_YAML_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# ── Minimal YAML frontmatter helpers (no PyYAML dependency) ────────


def _yaml_value(v: object) -> str:
    """Format a single YAML value."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        if not v:
            return "[]"
        items = ", ".join(_yaml_value(i) for i in v)
        return f"[{items}]"
    if isinstance(v, str):
        # Quote if it contains special chars
        yaml_special_chars = (
            '"', "\n", ":", "#", "{", "}", "[", "]", ",", "&", "*", "?",
            "|", "-", "<", ">", "=", "!", "%", "@", "`",
        )
        if any(c in v for c in yaml_special_chars):
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        if v == "":
            return '""'
        return v
    return str(v)


def _dump_yaml(data: dict) -> str:
    """Minimal YAML dict serializer — handles the frontmatter types we use."""
    lines: list[str] = []
    for key, value in data.items():
        lines.append(f"{key}: {_yaml_value(value)}")
    return "\n".join(lines)


def _parse_yaml(text: str) -> dict:
    """Minimal YAML parser for simple key: value frontmatter."""
    result: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, raw_val = line.partition(":")
        key = key.strip()
        raw_val = raw_val.strip()
        if raw_val == "null" or raw_val == "~":
            result[key] = None
        elif raw_val == "true":
            result[key] = True
        elif raw_val == "false":
            result[key] = False
        elif raw_val.startswith("[") and raw_val.endswith("]"):
            inner = raw_val[1:-1].strip()
            if inner:
                items = [_parse_yaml_value(i.strip()) for i in inner.split(",")]
            else:
                items = []
            result[key] = items
        elif raw_val.startswith('"') and raw_val.endswith('"'):
            result[key] = raw_val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        else:
            # Try int, float, then string
            try:
                if "." in raw_val:
                    result[key] = float(raw_val)
                else:
                    result[key] = int(raw_val)
            except ValueError:
                result[key] = raw_val
    return result


def _parse_yaml_value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return raw


class FileMemoryStore(MemoryStore):
    """YAML frontmatter + markdown files on the local filesystem."""

    def __init__(self, root_dir: str | Path = "bots/default/memory") -> None:
        self._root = Path(root_dir)
        self._index = MemoryIndex(self._index_path())

    # ── Path helpers ──────────────────────────────────────────────────

    def _file_path(self, name: str, type: str) -> Path:
        """Return the path for a memory record file."""
        safe_name = name.replace("/", "_").replace("..", "_")
        return self._root / type / f"{safe_name}.md"

    def _index_path(self) -> Path:
        return self._root / "MEMORY.md"

    def _root_exists(self) -> bool:
        return self._root.exists()

    def _list_type_dirs(self) -> list[Path]:
        """Return existing type subdirectories, or empty list if root missing."""
        if not self._root_exists():
            return []
        return [d for d in self._root.iterdir() if d.is_dir()]

    # ── Read / write helpers ──────────────────────────────────────────

    @staticmethod
    def _serialize(record: MemoryRecord) -> str:
        """Encode a MemoryRecord as YAML frontmatter + markdown body."""
        frontmatter: dict = {
            "name": record.name,
            "description": record.description,
            "type": record.type.value,
            "scope": record.scope.value,
            "world_id": record.world_id,
            "confidence": record.confidence,
            "ttl": record.ttl,
            "source": record.source,
            "evidence": record.evidence,
            "tags": record.tags,
            "valid_when": record.valid_when,
            "invalid_when": record.invalid_when,
            "created": record.created.isoformat(),
            "updated": record.updated.isoformat(),
        }
        # Remove None values for cleaner output
        frontmatter = {k: v for k, v in frontmatter.items() if v is not None}

        yaml_str = _dump_yaml(frontmatter)
        body = record.body or ""
        return f"---\n{yaml_str}\n---\n\n{body}\n"

    @staticmethod
    def _deserialize(raw: str) -> MemoryRecord | None:
        """Parse a YAML frontmatter + markdown file into a MemoryRecord."""
        m = _YAML_FRONTMATTER_RE.match(raw)
        if not m:
            return None
        try:
            data = _parse_yaml(m.group(1))
        except Exception:
            return None
        if not isinstance(data, dict) or "name" not in data:
            return None

        body = raw[m.end():].strip()

        created = data.get("created")
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        else:
            created = datetime.now(timezone.utc)

        updated = data.get("updated")
        if isinstance(updated, str):
            updated = datetime.fromisoformat(updated)
        else:
            updated = datetime.now(timezone.utc)

        return MemoryRecord(
            name=data["name"],
            description=data.get("description", ""),
            type=data.get("type", "fact"),
            scope=data.get("scope", "world"),
            world_id=data.get("world_id", "default"),
            created=created,
            updated=updated,
            confidence=data.get("confidence", 1.0),
            ttl=data.get("ttl"),
            valid_when=data.get("valid_when"),
            invalid_when=data.get("invalid_when"),
            source=data.get("source", "bot_observed"),
            evidence=data.get("evidence"),
            tags=data.get("tags", []),
            body=body,
        )

    # ── Index management ──────────────────────────────────────────────

    async def _rebuild_index(self) -> None:
        """Rebuild MEMORY.md from all record files."""
        await self._index.rebuild(await self.list(limit=10_000))

    # ── CRUD ──────────────────────────────────────────────────────────

    async def put(self, record: MemoryRecord) -> str:
        filepath = self._file_path(record.name, record.type.value)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(self._serialize(record))
        await self._rebuild_index()
        return str(filepath)

    async def get(
        self, name: str, *, scope: Scope | None = None, type: str | None = None,
    ) -> MemoryRecord | None:
        if type is None:
            for type_dir in self._list_type_dirs():
                candidate = type_dir / f"{name}.md"
                if candidate.exists():
                    record = self._deserialize(candidate.read_text())
                    if record and (scope is None or record.scope == scope):
                        return record
            return None

        filepath = self._file_path(name, type)
        if not filepath.exists():
            return None
        record = self._deserialize(filepath.read_text())
        if record and scope is not None and record.scope != scope:
            return None
        return record

    async def search(self, query: MemoryQuery) -> list[MemoryResult]:
        records = await self.list(
            scope=query.scope, type=query.type.value if query.type else None,
        )
        results: list[MemoryResult] = []

        keywords_lower = [k.lower() for k in query.keywords]
        tags_set = set(query.tags)

        for record in records:
            # Name exact match takes priority
            if query.name is not None and record.name != query.name:
                continue

            # Confidence floor
            if record.confidence < query.min_confidence:
                continue

            # World filter
            if query.world_id is not None and record.world_id != query.world_id:
                continue

            # Score computation
            score = 0.0
            matched_on: list[str] = []

            if query.name is not None and record.name == query.name:
                score += 10.0
                matched_on.append("name")

            # Keyword match on name
            name_lower = record.name.lower()
            for kw in keywords_lower:
                if kw in name_lower:
                    score += 5.0
                    matched_on.append("name")
                    break

            # Keyword match on description + body
            text = (record.description + " " + record.body).lower()
            for kw in keywords_lower:
                if kw in text:
                    score += 3.0
                    matched_on.append("keyword")
                    break

            # Tag match
            if tags_set:
                record_tags = {t.lower() for t in record.tags}
                overlap = tags_set & record_tags
                if overlap:
                    score += 4.0 * len(overlap)
                    matched_on.append("tag")
                elif query.tags:  # tags requested but none matched → skip
                    continue

            # Boost by confidence
            score *= record.confidence

            if score > 0 or query.name is not None:
                results.append(MemoryResult(
                    record=record,
                    score=score,
                    matched_on=",".join(matched_on) if matched_on else "name",
                ))

        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)
        return results[: query.limit]

    async def update(self, record: MemoryRecord) -> str:
        filepath = self._file_path(record.name, record.type.value)
        if not filepath.exists():
            raise FileNotFoundError(f"Memory record '{record.name}' not found")
        filepath.write_text(self._serialize(record))
        await self._rebuild_index()
        return str(filepath)

    async def delete(
        self, name: str, *, scope: Scope | None = None, type: str | None = None,
    ) -> bool:
        if type is None:
            deleted = False
            for type_dir in self._list_type_dirs():
                candidate = type_dir / f"{name}.md"
                if candidate.exists():
                    candidate.unlink()
                    deleted = True
            if deleted:
                await self._rebuild_index()
            return deleted

        filepath = self._file_path(name, type)
        if not filepath.exists():
            return False
        filepath.unlink()
        await self._rebuild_index()
        return True

    async def compact(self) -> int:
        """Expire TTL'd records. Returns count of removed files."""
        removed = 0
        now = datetime.now(timezone.utc)
        for type_dir in self._list_type_dirs():
            for md_file in type_dir.glob("*.md"):
                try:
                    record = self._deserialize(md_file.read_text())
                except Exception:
                    continue
                if record is None:
                    continue
                if record.is_expired(now):
                    md_file.unlink()
                    removed += 1
        if removed:
            await self._rebuild_index()
        return removed

    async def list(
        self, *, scope: Scope | None = None, type: str | None = None,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        dirs = [type] if type else None

        if dirs:
            type_dirs = [self._root / d for d in dirs]
        else:
            type_dirs = self._list_type_dirs()

        for type_dir in type_dirs:
            if not type_dir.exists():
                continue
            for md_file in sorted(type_dir.glob("*.md")):
                if len(records) >= limit:
                    break
                try:
                    record = self._deserialize(md_file.read_text())
                except Exception:
                    continue
                if record is None:
                    continue
                if scope is not None and record.scope != scope:
                    continue
                records.append(record)
            if len(records) >= limit:
                break

        return records[:limit]

    async def count(self, *, scope: Scope | None = None) -> int:
        records = await self.list(scope=scope, limit=10_000)
        return len(records)


class ScopedFileMemoryStore(MemoryStore):
    """File store using the Phase 4a bot/world directory layout.

    Layout:
      - ``bots/<name>/universal/memory`` for scope=bot
      - ``bots/<name>/worlds/<world_id>/memory`` for scope=world
    """

    def __init__(self, bot_root: str | Path, *, world_id: str = "default") -> None:
        self._bot_root = Path(bot_root)
        self._world_id = world_id
        self._bot_store = FileMemoryStore(self._bot_root / "universal" / "memory")
        self._world_store = FileMemoryStore(
            self._bot_root / "worlds" / world_id / "memory"
        )

    def _store_for_scope(self, scope: Scope | None) -> FileMemoryStore:
        if scope == Scope.BOT:
            return self._bot_store
        return self._world_store

    def _stores_for_query(self, scope: Scope | None) -> list[FileMemoryStore]:
        if scope == Scope.BOT:
            return [self._bot_store]
        if scope == Scope.WORLD:
            return [self._world_store]
        return [self._world_store, self._bot_store]

    async def put(self, record: MemoryRecord) -> str:
        if record.scope == Scope.WORLD:
            record.world_id = self._world_id
        return await self._store_for_scope(record.scope).put(record)

    async def get(
        self, name: str, *, scope: Scope | None = None, type: str | None = None,
    ) -> MemoryRecord | None:
        for store in self._stores_for_query(scope):
            record = await store.get(name, scope=scope, type=type)
            if record is not None:
                return record
        return None

    async def search(self, query: MemoryQuery) -> list[MemoryResult]:
        results: list[MemoryResult] = []
        for store in self._stores_for_query(query.scope):
            results.extend(await store.search(query))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[: query.limit]

    async def update(self, record: MemoryRecord) -> str:
        return await self._store_for_scope(record.scope).update(record)

    async def delete(
        self, name: str, *, scope: Scope | None = None, type: str | None = None,
    ) -> bool:
        deleted = False
        for store in self._stores_for_query(scope):
            deleted = await store.delete(name, scope=scope, type=type) or deleted
        return deleted

    async def compact(self) -> int:
        return await self._world_store.compact() + await self._bot_store.compact()

    async def list(
        self, *, scope: Scope | None = None, type: str | None = None,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        for store in self._stores_for_query(scope):
            records.extend(await store.list(scope=scope, type=type, limit=limit))
        records.sort(key=lambda r: (r.updated.timestamp(), r.confidence), reverse=True)
        return records[:limit]

    async def count(self, *, scope: Scope | None = None) -> int:
        return len(await self.list(scope=scope, limit=10_000))
