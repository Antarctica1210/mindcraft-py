from __future__ import annotations

import numpy as np

from agent.executor import ActionExecutor
from knowledge.wiki_store import WikiChunk, WikiVectorStore, _rrf_fuse


def test_rrf_fuse_prefers_consistent_hits() -> None:
    fused = _rrf_fuse([
        [(1, 0.9), (2, 0.8), (3, 0.7)],
        [(2, 10.0), (4, 9.0), (1, 8.0)],
    ], limit=3)

    assert fused[:2] == [2, 1]


def test_wiki_vector_store_missing_files_falls_back(tmp_path) -> None:
    store = WikiVectorStore(index_dir=tmp_path)

    assert store.load() is False
    assert store.loaded is False
    assert store.search("chicken breeding") == []


def test_wiki_vector_store_search_dense_and_rerank() -> None:
    class FakeEmbedder:
        def encode(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                "dense_vecs": np.array([[1.0, 0.0]], dtype=np.float32),
                "lexical_weights": [{}],
            }

    class FakeIndex:
        def search(self, _dense: np.ndarray, _k: int) -> tuple[np.ndarray, np.ndarray]:
            return (
                np.array([[0.9, 0.8]], dtype=np.float32),
                np.array([[1, 0]], dtype=np.int64),
            )

    class FakeReranker:
        def compute_score(
            self, _pairs: list[tuple[str, str]], *, normalize: bool = True,
        ) -> list[float]:
            assert normalize is True
            return [0.2, 0.95]

    store = WikiVectorStore()
    store._loaded = True
    store._chunks = [
        WikiChunk(
            title="Wheat Seeds",
            url="https://minecraft.wiki/w/Wheat_Seeds",
            text="Break grass for seeds.",
            chunk_id=0,
        ),
        WikiChunk(
            title="Chicken",
            url="https://minecraft.wiki/w/Chicken",
            text="Chickens can be bred with seeds.",
            chunk_id=1,
        ),
    ]
    store._embedder = FakeEmbedder()
    store._dense_index = FakeIndex()
    store._reranker = FakeReranker()

    results = store.search("怎么繁殖鸡", k=1)

    assert [chunk.title for chunk in results] == ["Wheat Seeds"]


async def test_search_wiki_uses_local_store_before_http() -> None:
    class FakeStore:
        loaded = True

        def search(self, query: str, k: int = 5) -> list[WikiChunk]:
            assert query == "床的配方"
            assert k == 5
            return [
                WikiChunk(
                    title="Bed",
                    url="https://minecraft.wiki/w/Bed",
                    text="A bed is crafted with wool and planks.",
                    chunk_id=0,
                ),
            ]

        def format_results(self, chunks: list[WikiChunk]) -> str:
            return "\n".join(chunk.text for chunk in chunks)

    executor = ActionExecutor()
    executor.set_wiki_store(FakeStore())
    executor._register_builtins()

    result = await executor.execute("searchWiki", ["床的配方"])

    assert result == "A bed is crafted with wool and planks."
