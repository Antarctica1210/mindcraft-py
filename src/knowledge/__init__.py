"""
Phase 7a — Minecraft static knowledge package.

Provides a read-only KnowledgeGraph backed by exported minecraft-data JSON
and optional Minecraft Wiki retrieval.
"""

from .knowledge_graph import KnowledgeGraph
from .wiki_store import WikiChunk, WikiVectorStore

__all__ = ["KnowledgeGraph", "WikiChunk", "WikiVectorStore"]
