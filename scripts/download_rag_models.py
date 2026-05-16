#!/usr/bin/env python3
"""Download Wiki RAG embedding/reranker models into the project tree."""
from __future__ import annotations

import argparse
from pathlib import Path

DEFAULT_MODELS = {
    "embed": "BAAI/bge-m3",
    "rerank": "BAAI/bge-reranker-v2-m3",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _model_path(output_dir: Path, repo_id: str) -> Path:
    namespace, name = repo_id.split("/", 1)
    return output_dir / namespace / name


def _download(repo_id: str, target_dir: Path, *, revision: str | None = None) -> None:
    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:
        raise SystemExit(
            "Missing dependency: huggingface_hub. Install with:\n"
            "  uv sync --extra wiki-rag\n"
            "or:\n"
            "  uv add huggingface-hub\n"
            f"Import error: {exc}"
        ) from exc

    target_dir.mkdir(parents=True, exist_ok=True)
    kwargs = {
        "repo_id": repo_id,
        "local_dir": str(target_dir),
        "resume_download": True,
    }
    if revision:
        kwargs["revision"] = revision
    try:
        snapshot_download(local_dir_use_symlinks=False, **kwargs)
    except TypeError:
        snapshot_download(**kwargs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(_project_root() / "models"),
        help="Directory to place models in. Default: ./models",
    )
    parser.add_argument("--embed-model", default=DEFAULT_MODELS["embed"])
    parser.add_argument("--rerank-model", default=DEFAULT_MODELS["rerank"])
    parser.add_argument("--revision", default=None)
    parser.add_argument(
        "--only",
        choices=["all", "embed", "rerank"],
        default="all",
        help="Download both models or just one.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output).resolve()
    selected = []
    if args.only in {"all", "embed"}:
        selected.append(("embed", args.embed_model))
    if args.only in {"all", "rerank"}:
        selected.append(("rerank", args.rerank_model))

    resolved_paths: dict[str, Path] = {}
    for kind, repo_id in selected:
        target = _model_path(output_dir, repo_id)
        print(f"Downloading {repo_id} -> {target}")
        _download(repo_id, target, revision=args.revision)
        resolved_paths[kind] = target

    embed_path = resolved_paths.get("embed") or _model_path(output_dir, args.embed_model)
    rerank_path = resolved_paths.get("rerank") or _model_path(output_dir, args.rerank_model)
    print("\nDone. Add these to .env to use local model paths:")
    print(f"WIKI_RAG_EMBED_MODEL={embed_path.relative_to(_project_root())}")
    print(f"WIKI_RAG_RERANK_MODEL={rerank_path.relative_to(_project_root())}")


if __name__ == "__main__":
    main()
