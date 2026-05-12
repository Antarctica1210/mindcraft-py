"""
Phase 3 — Tombstone / forget deny-list.

When a player calls ``forget(name)``, the entry is added to a persistent
deny-list so that background reflection workers never re-write it from
raw L1 trace data.  ``unforget(name)`` removes the tombstone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TombstoneStore:
    """Manage a persistent deny-list of forgotten entity keys.

    File: ``bots/<name>/worlds/<world_id>/memory/.tombstones.json``
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: set[str] = set()

    async def load(self) -> None:
        """Load the tombstone set from disk (idempotent)."""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._entries = set(data.get("forgotten", []))
            except (json.JSONDecodeError, KeyError):
                self._entries = set()

    async def save(self) -> None:
        """Persist the current tombstone set."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"forgotten": sorted(self._entries)}, indent=2)
        )

    def is_forgotten(self, key: str) -> bool:
        """Check whether a key (type/name) has been explicitly forgotten."""
        return key in self._entries

    async def forget(self, key: str) -> None:
        """Add a key to the deny-list."""
        self._entries.add(key)
        await self.save()

    async def unforget(self, key: str) -> bool:
        """Remove a key from the deny-list.  Returns True if it existed."""
        if key in self._entries:
            self._entries.discard(key)
            await self.save()
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {"forgotten": sorted(self._entries)}
