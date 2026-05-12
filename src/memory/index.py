"""Phase 4a MEMORY.md index helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .models import MemoryRecord


class MemoryIndex:
    """Read/write a compact human-readable MEMORY.md index."""

    def __init__(self, path: str | Path, *, max_lines: int = 200) -> None:
        self.path = Path(path)
        self.max_lines = max_lines

    async def rebuild(self, records: list[MemoryRecord]) -> None:
        ordered = sorted(
            records,
            key=lambda r: (r.updated.timestamp(), r.confidence),
            reverse=True,
        )
        lines = [
            "# Memory Index",
            "",
            f"_Last rebuilt: {datetime.now(timezone.utc).isoformat()}_",
            "",
        ]
        for record in ordered[: self.max_lines]:
            lines.append(record.index_line(max_len=150))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(lines) + "\n")

    def read(self) -> str:
        if not self.path.exists():
            return ""
        return self.path.read_text()
