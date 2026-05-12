"""
Phase 3 — L1 NDJSON append-only event sink.

``SessionSink`` is the fire-and-forget writer called by
``MemoryService.record_event()``.  It appends one JSON line per event
to ``bots/<name>/worlds/<world>/sessions/<date>/<session_id>.ndjson``.

The sink also exposes slicing helpers so the reflection worker can read back
a time range or session-scoped chunk without parsing the full file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SessionSink:
    """Append-only NDJSON writer for L1 raw event traces."""

    def __init__(self, base_dir: Path, world_id: str = "default") -> None:
        self._base = base_dir  # bots/<name>/worlds/<world_id>/sessions
        self._world_id = world_id

    # ── path helpers ───────────────────────────────────────────────────

    def _session_path(self, session_id: str) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._base / self._world_id / "sessions" / today / f"{session_id}.ndjson"

    @staticmethod
    def _ensure_parent(p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)

    # ── append ──────────────────────────────────────────────────────────

    async def append(self, event: dict[str, Any]) -> None:
        """Append one JSON line.  Fire-and-forget — does not block the game loop."""
        session_id = event.get("session_id", "default")
        path = self._session_path(session_id)
        self._ensure_parent(path)
        line = json.dumps(event, ensure_ascii=False, default=str)
        with open(path, "a") as fh:
            fh.write(line + "\n")

    async def append_many(self, events: list[dict[str, Any]]) -> None:
        """Batch append — one syscall instead of N."""
        if not events:
            return
        # Group by session_id to minimise file open/close
        by_session: dict[str, list[dict[str, Any]]] = {}
        for ev in events:
            sid = ev.get("session_id", "default")
            by_session.setdefault(sid, []).append(ev)
        for sid, batch in by_session.items():
            path = self._session_path(sid)
            self._ensure_parent(path)
            lines = "\n".join(
                json.dumps(e, ensure_ascii=False, default=str) for e in batch
            )
            with open(path, "a") as fh:
                fh.write(lines + "\n")

    # ── read-back (for reflection worker) ───────────────────────────────

    async def read_session(self, session_id: str) -> list[dict[str, Any]]:
        """Read all events for a session.  Small enough to load into memory."""
        # Search across date directories
        sessions_root = self._base / self._world_id / "sessions"
        if not sessions_root.exists():
            return []
        for date_dir in sorted(sessions_root.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            candidate = date_dir / f"{session_id}.ndjson"
            if candidate.exists():
                return self._parse_ndjson(candidate)
        return []

    async def read_range(
        self, since: datetime, until: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Read events in a time window across all sessions."""
        results: list[dict[str, Any]] = []
        sessions_root = self._base / self._world_id / "sessions"
        if not sessions_root.exists():
            return results
        for date_dir in sorted(sessions_root.iterdir()):
            if not date_dir.is_dir():
                continue
            for ndjson_file in sorted(date_dir.glob("*.ndjson")):
                events = self._parse_ndjson(ndjson_file)
                for ev in events:
                    ts_str = ev.get("ts", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except (ValueError, TypeError):
                        continue
                    if ts >= since and (until is None or ts < until):
                        results.append(ev)
        return results

    async def list_sessions(self) -> list[str]:
        """Return all known session IDs."""
        ids: set[str] = set()
        sessions_root = self._base / self._world_id / "sessions"
        if not sessions_root.exists():
            return []
        for date_dir in sorted(sessions_root.iterdir()):
            if not date_dir.is_dir():
                continue
            for f in date_dir.glob("*.ndjson"):
                ids.add(f.stem)
        return sorted(ids)

    async def session_size(self, session_id: str) -> int:
        """Return approximate event count for a session."""
        events = await self.read_session(session_id)
        return len(events)

    @staticmethod
    def _parse_ndjson(path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except FileNotFoundError:
            return []
        return rows

    # ── cleanup ─────────────────────────────────────────────────────────

    async def purge_before(self, cutoff: datetime) -> int:
        """Delete NDJSON files whose date directory is before *cutoff*.
        Returns count of removed files."""
        removed = 0
        sessions_root = self._base / self._world_id / "sessions"
        if not sessions_root.exists():
            return 0
        for date_dir in sorted(sessions_root.iterdir()):
            if not date_dir.is_dir():
                continue
            try:
                dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            if dir_date < cutoff:
                for f in date_dir.glob("*.ndjson"):
                    f.unlink()
                    removed += 1
                # Remove directory if empty
                try:
                    date_dir.rmdir()
                except OSError:
                    pass
        return removed
