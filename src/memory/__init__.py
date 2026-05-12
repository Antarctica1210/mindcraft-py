"""
Phase 3 — MemoryLayer unified entry point.

Public API surface: MemoryService, MemoryStore, and all Pydantic models.
"""

from .file_store import FileMemoryStore, ScopedFileMemoryStore
from .index import MemoryIndex
from .models import (
    MemoryQuery,
    MemoryRecord,
    MemoryResult,
    MemoryType,
    Scope,
    WorkingContext,
)
from .store import MemoryStore

__all__ = [
    "FileMemoryStore",
    "ScopedFileMemoryStore",
    "MemoryIndex",
    "MemoryStore",
    "MemoryRecord",
    "MemoryQuery",
    "MemoryResult",
    "MemoryType",
    "Scope",
    "WorkingContext",
]
