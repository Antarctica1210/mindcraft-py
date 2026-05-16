#!/usr/bin/env python3
"""Build a local Minecraft Wiki RAG index.

This is the Stage 3a entrypoint.  It fails fast with an actionable dependency
message when heavy optional packages are missing, so it is safe to keep in the
repo before every developer has the RAG toolchain installed.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CATEGORIES = [
    "Crafting",
    "Blocks",
    "Items",
    "Mobs",
    "Biomes",
    "Food",
    "Enchantments",
    "Potions",
    "Status effects",
    "Mechanics",
    "Structures",
]
FIXED_PAGES = [
    "Crafting",
    "Smelting",
    "Brewing",
    "Enchanting",
    "Fishing",
    "Farming",
    "Mining",
    "Trading",
    "Inventory",
    "Experience",
    "Hunger",
    "Health",
    "Damage",
    "Armor",
    "Shield",
    "Sword",
    "Pickaxe",
    "Axe",
    "Shovel",
    "Hoe",
    "Bow",
    "Crossbow",
    "Bed",
    "Chest",
    "Furnace",
    "Crafting table",
    "The Nether",
    "The End",
    "Overworld",
]
API_URL = "https://minecraft.wiki/api.php"
USER_AGENT = "MindcraftPyWikiRAG/0.1"


@dataclass(frozen=True)
class RawPage:
    title: str
    wikitext: str


@dataclass(frozen=True)
class Chunk:
    title: str
    url: str
    text: str
    chunk_id: int


def _require_optional_deps() -> tuple[Any, Any, Any, Any]:
    try:
        import mwparserfromhell  # type: ignore
        import numpy as np  # type: ignore
        import scipy.sparse as sp  # type: ignore
        from FlagEmbedding import BGEM3FlagModel  # type: ignore
    except Exception as exc:
        raise SystemExit(
            "Wiki ingestion requires optional dependencies. Install with:\n"
            "  uv sync --extra wiki-rag\n"
            "or:\n"
            "  pip install FlagEmbedding faiss-cpu scipy mwparserfromhell\n"
            f"Missing dependency detail: {exc}"
        ) from exc
    return BGEM3FlagModel, mwparserfromhell, np, sp


def _require_faiss() -> Any:
    try:
        import faiss  # type: ignore
    except Exception as exc:
        raise SystemExit(
            "Wiki ingestion requires faiss-cpu. Install with:\n"
            "  uv sync --extra wiki-rag\n"
            f"Missing dependency detail: {exc}"
        ) from exc
    return faiss


def _resolve_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import torch  # type: ignore

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        return ""
    return ""


def _construct_bge_model(factory: Any, model_name: str, *, use_fp16: bool, device: str) -> Any:
    kwargs: dict[str, Any] = {"use_fp16": use_fp16}
    if device:
        kwargs["device"] = device
    try:
        return factory(model_name, **kwargs)
    except TypeError:
        kwargs.pop("device", None)
        return factory(model_name, **kwargs)


def _api_get(params: dict[str, Any]) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{API_URL}?{encoded}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_category_members(category: str, *, delay: float = 0.3) -> list[str]:
    titles: list[str] = []
    cmcontinue = ""
    while True:
        params: dict[str, Any] = {
            "action": "query",
            "format": "json",
            "list": "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmlimit": 500,
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        data = _api_get(params)
        titles.extend(
            item["title"]
            for item in data.get("query", {}).get("categorymembers", [])
            if item.get("ns") == 0 and item.get("title")
        )
        cmcontinue = data.get("continue", {}).get("cmcontinue", "")
        if not cmcontinue:
            break
        time.sleep(delay)
    return titles


def fetch_page(title: str) -> RawPage | None:
    data = _api_get({
        "action": "query",
        "format": "json",
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "titles": title,
    })
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        revisions = page.get("revisions", [])
        if not revisions:
            return None
        slots = revisions[0].get("slots", {})
        content = slots.get("main", {}).get("*", "")
        if content:
            return RawPage(title=page.get("title", title), wikitext=content)
    return None


def clean_wikitext(wikitext: str, mwparserfromhell: Any) -> str:
    wikicode = mwparserfromhell.parse(wikitext)
    text = wikicode.strip_code(normalize=True, collapse=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def chunk_text(
    title: str,
    text: str,
    *,
    chunk_size: int = 400,
    chunk_overlap: int = 50,
    min_words: int = 30,
) -> list[Chunk]:
    words = text.split()
    if len(words) < min_words:
        return []
    chunks: list[Chunk] = []
    step = max(1, chunk_size - chunk_overlap)
    url_title = title.replace(" ", "_")
    for start in range(0, len(words), step):
        piece = words[start:start + chunk_size]
        if len(piece) < min_words:
            continue
        chunks.append(Chunk(
            title=title,
            url=f"https://minecraft.wiki/w/{urllib.parse.quote(url_title)}",
            text=" ".join(piece),
            chunk_id=-1,
        ))
        if start + chunk_size >= len(words):
            break
    return chunks


def collect_titles(
    categories: list[str],
    max_pages: int | None,
    pages: list[str] | None = None,
    *,
    delay: float = 0.3,
) -> list[str]:
    titles: list[str] = []
    seen: set[str] = set()
    if pages:
        for title in pages:
            clean = title.strip().replace("_", " ")
            if clean and clean not in seen:
                titles.append(clean)
                seen.add(clean)
        return titles[:max_pages] if max_pages else titles

    for title in FIXED_PAGES:
        if title not in seen:
            titles.append(title)
            seen.add(title)
    for category in categories:
        for title in fetch_category_members(category, delay=delay):
            if title in seen:
                continue
            titles.append(title)
            seen.add(title)
            if max_pages and len(titles) >= max_pages:
                return titles[:max_pages]
        time.sleep(delay)
    return titles[:max_pages] if max_pages else titles


def build_index(args: argparse.Namespace) -> None:
    bge_m3_flag_model, mwparserfromhell, np, sp = _require_optional_deps()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    pages = [p.strip() for p in args.pages.split(",") if p.strip()]
    titles = collect_titles(categories, args.max_pages, pages, delay=args.delay)

    chunks: list[Chunk] = []
    raw_dir = output / "raw"
    raw_dir.mkdir(exist_ok=True)
    raw_pages: list[RawPage] = []
    if args.from_raw:
        for path in sorted(raw_dir.glob("*.txt")):
            raw_pages.append(RawPage(title=path.stem.replace("_", " "), wikitext=path.read_text()))
    else:
        existing_raw = {path.stem.replace("_", " ") for path in raw_dir.glob("*.txt")}
        fetched = 0
        skipped = 0
        for title in titles:
            if args.skip_existing and title in existing_raw:
                skipped += 1
                continue
            page = fetch_page(title)
            if page is None:
                continue
            (raw_dir / f"{title.replace('/', '_')}.txt").write_text(page.wikitext)
            existing_raw.add(page.title)
            fetched += 1
            time.sleep(args.delay)
        if args.skip_existing:
            print(f"Fetch complete: fetched={fetched} skipped_existing={skipped}", flush=True)
        for path in sorted(raw_dir.glob("*.txt")):
            raw_pages.append(RawPage(title=path.stem.replace("_", " "), wikitext=path.read_text()))
    print(f"Loaded raw pages: {len(raw_pages)}", flush=True)

    for page in raw_pages:
        text = clean_wikitext(page.wikitext, mwparserfromhell)
        chunks.extend(chunk_text(
            page.title,
            text,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
        ))

    chunks = [
        Chunk(title=c.title, url=c.url, text=c.text, chunk_id=i)
        for i, c in enumerate(chunks)
    ]
    if not chunks:
        raise SystemExit("No chunks were produced.")
    print(f"Prepared chunks: {len(chunks)}", flush=True)

    device = _resolve_device(args.device)
    print(
        f"Loading embed model: {args.embed_model} device={device or 'cpu'} "
        f"fp16={args.fp16}",
        flush=True,
    )
    model = _construct_bge_model(
        bge_m3_flag_model,
        args.embed_model,
        use_fp16=args.fp16,
        device=device,
    )
    print("Encoding chunks...", flush=True)
    encoded = model.encode(
        [chunk.text for chunk in chunks],
        batch_size=args.batch_size,
        return_dense=True,
        return_sparse=True,
        return_colbert_vecs=False,
    )
    dense_vecs = np.asarray(encoded["dense_vecs"], dtype=np.float32)
    print("Writing dense FAISS index...", flush=True)
    faiss = _require_faiss()
    faiss.normalize_L2(dense_vecs)
    index = faiss.IndexFlatIP(dense_vecs.shape[1])
    index.add(dense_vecs)
    faiss.write_index(index, str(output / "index.faiss"))

    print("Writing sparse index and metadata...", flush=True)
    lexical_weights = encoded["lexical_weights"]
    tokens = sorted({str(token) for lw in lexical_weights for token in lw})
    vocab = {token: i for i, token in enumerate(tokens)}
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    for row_id, weights in enumerate(lexical_weights):
        for token, weight in weights.items():
            rows.append(row_id)
            cols.append(vocab[str(token)])
            vals.append(float(weight))
    sparse = sp.csr_matrix(
        (vals, (rows, cols)),
        shape=(len(chunks), len(vocab)),
        dtype=np.float32,
    )
    sp.save_npz(output / "sparse.npz", sparse)

    with (output / "chunks.jsonl").open("w") as fh:
        for chunk in chunks:
            fh.write(json.dumps(asdict(chunk), ensure_ascii=False) + "\n")
    (output / "vocab.json").write_text(json.dumps(vocab, ensure_ascii=False))
    (output / "manifest.json").write_text(json.dumps({
        "embed_model": args.embed_model,
        "dim": int(dense_vecs.shape[1]),
        "num_chunks": len(chunks),
        "num_pages_requested": len(titles),
        "categories": categories,
        "device": device or "cpu",
        "created_at": datetime.now(UTC).isoformat(),
    }, indent=2))
    print(f"Wrote wiki index: {output}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="data/wiki")
    parser.add_argument(
        "--from-raw",
        action="store_true",
        help="Skip network fetch and build from existing <output>/raw/*.txt",
    )
    parser.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES))
    parser.add_argument(
        "--pages",
        default="",
        help="Comma-separated exact pages to ingest, e.g. Chicken,Wheat Seeds",
    )
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--chunk-size", type=int, default=400)
    parser.add_argument("--chunk-overlap", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help=(
            "Do not re-fetch pages already present in <output>/raw. "
            "After fetching missing pages, build the index from all raw files."
        ),
    )
    parser.add_argument("--embed-model", default="BAAI/bge-m3")
    parser.add_argument("--device", default="auto", help="auto, mps, cuda, cpu, or empty")
    precision = parser.add_mutually_exclusive_group()
    precision.add_argument("--fp16", action="store_true", default=True)
    precision.add_argument("--fp32", action="store_false", dest="fp16")
    return parser.parse_args()


if __name__ == "__main__":
    build_index(parse_args())
