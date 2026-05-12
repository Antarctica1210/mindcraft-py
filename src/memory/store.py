"""
Phase 3 — MemoryStore abstract interface.

All memory backends (file, SQLite, FAISS) must implement this protocol.
The contract is deliberately broad — concrete stores can optimise for
their storage model while keeping the caller API unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import MemoryQuery, MemoryRecord, MemoryResult, Scope


class MemoryStore(ABC):
    """Abstract interface for memory storage backends.

    Phase 3 defines the contract.  Concrete implementations:
      - Phase 4a: ``FileMemoryStore``  (YAML frontmatter + .md files)
      - Phase 3b: ``SQLiteMemoryStore`` (structured index + full-text search)
      - Phase 4b: ``FAISSStore``        (dense vector retrieval, optional accelerator)
    """

    # ── CRUD ────────────────────────────────────────────────────────

    @abstractmethod
    async def put(self, record: MemoryRecord) -> str:
        """Store a memory record. Returns a stable identifier (path/key)."""
        ...

    @abstractmethod
    async def get(
        self, name: str, *, scope: Scope | None = None, type: str | None = None,
    ) -> MemoryRecord | None:
        """Retrieve a single record by name (optionally scoped)."""
        ...

    @abstractmethod
    async def search(self, query: MemoryQuery) -> list[MemoryResult]:
        """Search / recall records matching the query.

        Results are ordered by relevance (score descending) and must not
        exceed ``query.limit``.
        """
        ...

    @abstractmethod
    async def update(self, record: MemoryRecord) -> str:
        """Update an existing record. The ``name`` field must match an existing entry."""
        ...

    @abstractmethod
    async def delete(
        self, name: str, *, scope: Scope | None = None, type: str | None = None,
    ) -> bool:
        """Delete a record. Returns True if the record existed."""
        ...

    @abstractmethod
    async def compact(self) -> int:
        """Run maintenance (TTL expiry, index rebuild, etc.). Returns number of records removed."""
        ...

    @abstractmethod
    async def list(
        self, *, scope: Scope | None = None, type: str | None = None,
        limit: int = 200,
    ) -> list[MemoryRecord]:
        """List records, optionally filtered by scope/type. For index generation."""
        ...

    @abstractmethod
    async def count(self, *, scope: Scope | None = None) -> int:
        """Return total record count (for scale decisions — Phase 4b trigger)."""
        ...
