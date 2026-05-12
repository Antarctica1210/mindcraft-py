"""
Phase 7a — Minecraft static knowledge package.

Provides a read-only KnowledgeGraph backed by exported minecraft-data JSON.
"""

from .knowledge_graph import KnowledgeGraph

__all__ = ["KnowledgeGraph"]
