"""Local Minecraft Wiki hybrid retrieval store.

The store is deliberately optional-dependency friendly.  If the wiki index or
heavy retrieval packages are missing, ``load()`` returns ``False`` and callers
can fall back to HTTP search.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WikiChunk:
    """A single indexed Minecraft Wiki text chunk."""

    title: str
    url: str
    text: str
    chunk_id: int


def _top_n(scores: np.ndarray, n: int) -> list[tuple[int, float]]:
    if scores.size == 0:
        return []
    limit = min(n, scores.size)
    if limit <= 0:
        return []
    idx = np.argpartition(-scores, limit - 1)[:limit]
    ranked = sorted(((int(i), float(scores[i])) for i in idx), key=lambda item: -item[1])
    return ranked


def _rrf_fuse(
    ranked_lists: list[list[tuple[int, float]]],
    *,
    rrf_k: int = 60,
    limit: int = 20,
) -> list[int]:
    """Fuse ranked chunk ids with Reciprocal Rank Fusion."""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, (chunk_id, _score) in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank)
    return [
        chunk_id
        for chunk_id, _score in sorted(scores.items(), key=lambda item: -item[1])[:limit]
    ]


class WikiVectorStore:
    """Hybrid dense+sparse Minecraft Wiki retrieval with optional reranking."""

    def __init__(
        self,
        index_dir: str | Path = "data/wiki",
        *,
        embed_model: str = "BAAI/bge-m3",
        rerank_model: str = "BAAI/bge-reranker-v2-m3",
        use_fp16: bool = True,
        device: str = "auto",
        rerank_candidates: int = 20,
        dense_candidates: int = 50,
        min_rerank_score: float = 0.1,
    ) -> None:
        self.index_dir = Path(index_dir)
        self.embed_model = embed_model
        self.rerank_model = rerank_model
        self.use_fp16 = use_fp16
        self.device = device
        self.rerank_candidates = rerank_candidates
        self.dense_candidates = dense_candidates
        self.min_rerank_score = min_rerank_score

        self._loaded = False
        self._chunks: list[WikiChunk] = []
        self._vocab: dict[str, int] = {}
        self._dense_index: Any = None
        self._sparse_matrix: Any = None
        self._embedder: Any = None
        self._reranker: Any = None
        self._faiss: Any = None
        self._sp: Any = None

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def chunks(self) -> list[WikiChunk]:
        return list(self._chunks)

    def load(self) -> bool:
        """Load index files and retrieval models.

        Returns ``False`` on missing dependencies/files so production startup can
        continue and ``searchWiki`` can use HTTP fallback.
        """
        required = [
            self.index_dir / "index.faiss",
            self.index_dir / "sparse.npz",
            self.index_dir / "vocab.json",
            self.index_dir / "chunks.jsonl",
        ]
        missing = [path.name for path in required if not path.exists()]
        if missing:
            logger.info("WikiVectorStore disabled; missing files: %s", ", ".join(missing))
            return False

        try:
            from FlagEmbedding import BGEM3FlagModel, FlagReranker  # type: ignore
        except Exception as exc:
            logger.info("WikiVectorStore disabled; optional dependency missing: %s", exc)
            return False

        try:
            self._chunks = self._load_chunks(self.index_dir / "chunks.jsonl")
            self._vocab = json.loads((self.index_dir / "vocab.json").read_text())
            model_kwargs = self._model_kwargs()
            self._embedder = self._construct_model(
                BGEM3FlagModel,
                self.embed_model,
                use_fp16=self.use_fp16,
                **model_kwargs,
            )
            self._reranker = self._construct_model(
                FlagReranker,
                self.rerank_model,
                use_fp16=self.use_fp16,
                **model_kwargs,
            )
            import faiss  # type: ignore
            import scipy.sparse as sp  # type: ignore

            self._faiss = faiss
            self._sp = sp
            self._dense_index = faiss.read_index(str(self.index_dir / "index.faiss"))
            self._sparse_matrix = sp.load_npz(self.index_dir / "sparse.npz")
        except Exception:
            logger.exception("WikiVectorStore failed to load")
            self._loaded = False
            return False

        self._loaded = True
        logger.info(
            "WikiVectorStore loaded: chunks=%d index_dir=%s device=%s",
            len(self._chunks),
            self.index_dir,
            self._model_device(),
        )
        return True

    def search(self, query: str, k: int = 5) -> list[WikiChunk]:
        """Return top-k chunks for a query, or [] when unavailable."""
        if not self._loaded or not query.strip():
            return []

        output = self._embedder.encode(
            [query],
            batch_size=1,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        ranked_lists: list[list[tuple[int, float]]] = []
        dense_ranked = self._dense_search(output)
        sparse_ranked = self._sparse_search(output)
        if dense_ranked:
            ranked_lists.append(dense_ranked)
        if sparse_ranked:
            ranked_lists.append(sparse_ranked)
        if not ranked_lists:
            return []

        candidate_ids = _rrf_fuse(
            ranked_lists,
            limit=max(k, self.rerank_candidates),
        )
        if not candidate_ids:
            return []
        ranked_ids = self._rerank(query, candidate_ids)
        return [
            self._chunks[chunk_id]
            for chunk_id in ranked_ids[:k]
            if 0 <= chunk_id < len(self._chunks)
        ]

    def format_results(self, chunks: list[WikiChunk]) -> str:
        """Format chunks for a bot command response."""
        parts = []
        for chunk in chunks:
            header = f"## {chunk.title}\nSource: {chunk.url}"
            parts.append(f"{header}\n{chunk.text.strip()}")
        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _load_chunks(path: Path) -> list[WikiChunk]:
        chunks: list[WikiChunk] = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            chunks.append(WikiChunk(
                title=str(data.get("title", "")),
                url=str(data.get("url", "")),
                text=str(data.get("text", "")),
                chunk_id=int(data.get("chunk_id", len(chunks))),
            ))
        chunks.sort(key=lambda chunk: chunk.chunk_id)
        return chunks

    @staticmethod
    def _construct_model(factory: Any, model_name: str, **kwargs: Any) -> Any:
        try:
            return factory(model_name, **kwargs)
        except TypeError:
            kwargs.pop("device", None)
            return factory(model_name, **kwargs)

    def _model_kwargs(self) -> dict[str, str]:
        device = self._model_device()
        return {"device": device} if device else {}

    def _model_device(self) -> str:
        if self.device != "auto":
            return self.device
        try:
            import torch  # type: ignore

            if torch.backends.mps.is_available():
                return "mps"
            if torch.cuda.is_available():
                return "cuda"
        except Exception:
            return ""
        return ""

    def _dense_search(self, output: dict[str, Any]) -> list[tuple[int, float]]:
        if self._dense_index is None:
            return []
        dense = np.asarray(output.get("dense_vecs"), dtype=np.float32)
        if dense.ndim == 1:
            dense = dense.reshape(1, -1)
        if dense.size == 0:
            return []
        if self._faiss is not None:
            self._faiss.normalize_L2(dense)
        else:
            norm = np.linalg.norm(dense, axis=1, keepdims=True)
            dense = dense / np.maximum(norm, 1e-12)
        scores, indexes = self._dense_index.search(
            dense,
            min(self.dense_candidates, len(self._chunks)),
        )
        return [
            (int(chunk_id), float(score))
            for chunk_id, score in zip(indexes[0], scores[0], strict=False)
            if int(chunk_id) >= 0
        ]

    def _sparse_search(self, output: dict[str, Any]) -> list[tuple[int, float]]:
        if self._sp is None or self._sparse_matrix is None or not self._vocab:
            return []
        lexical = output.get("lexical_weights") or [{}]
        weights = lexical[0] if isinstance(lexical, list) else lexical
        rows: list[int] = []
        cols: list[int] = []
        vals: list[float] = []
        for token, weight in weights.items():
            col = self._vocab.get(str(token))
            if col is None:
                continue
            rows.append(0)
            cols.append(col)
            vals.append(float(weight))
        if not vals:
            return []
        query_vec = self._sp.csr_matrix(
            (vals, (rows, cols)),
            shape=(1, len(self._vocab)),
            dtype=np.float32,
        )
        scores = (self._sparse_matrix @ query_vec.T).toarray().ravel()
        return _top_n(scores, self.dense_candidates)

    def _rerank(self, query: str, candidate_ids: list[int]) -> list[int]:
        if self._reranker is None:
            return candidate_ids
        pairs = [
            (query, self._chunks[chunk_id].text)
            for chunk_id in candidate_ids
            if 0 <= chunk_id < len(self._chunks)
        ]
        if not pairs:
            return []
        try:
            scores = self._reranker.compute_score(pairs, normalize=True)
        except Exception as exc:
            logger.warning("Wiki reranker disabled after failure; using RRF order: %s", exc)
            self._reranker = None
            return candidate_ids
        if isinstance(scores, (int, float)):
            scores = [float(scores)]
        ranked: list[tuple[int, float]] = []
        for chunk_id, score in zip(candidate_ids, scores, strict=False):
            score_float = float(score)
            if math.isnan(score_float) or score_float < self.min_rerank_score:
                continue
            ranked.append((chunk_id, score_float))
        return [
            chunk_id
            for chunk_id, _score in sorted(ranked, key=lambda item: -item[1])
        ]
