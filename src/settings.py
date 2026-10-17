"""
Settings — runtime configuration loaded from environment variables.

Loads a .env file (if present) and exposes typed settings for:
  - LLM provider (via LangChain ChatOpenAI)
  - Embedding provider (via OpenAIEmbeddings)
  - Bot identity and behaviour

Usage:
    from settings import settings
    print(settings.llm_model)  # e.g. "openai/qwen/qwen3.6-35b-a3b"
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── Load .env ───────────────────────────────────────────────────────

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if _ENV_FILE.exists():
    with open(_ENV_FILE) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# ── Settings ────────────────────────────────────────────────────────


@dataclass
class LLMSettings:
    """LLM provider configuration consumed by LangChain ChatOpenAI."""

    # LangChain ChatOpenAI model string: "gpt-4o", "qwen/qwen3.6-35b-a3b", etc.
    # For custom OpenAI-compatible endpoints (LM Studio), set LLM_BASE_URL
    model: str = field(default_factory=lambda: _get("LLM_MODEL", "qwen/qwen3.6-35b-a3b"))

    # API base URL — only needed for custom providers (LM Studio, local proxies)
    base_url: str = field(default_factory=lambda: _get("LLM_BASE_URL", "http://localhost:1234/v1"))

    # API key — LiteLLM reads os.environ["OPENAI_API_KEY"] etc. automatically,
    # but we store a fallback here for custom providers
    api_key: str = field(default_factory=lambda: _get("LLM_API_KEY", ""))

    max_tokens: int = field(default_factory=lambda: _get_int("LLM_MAX_TOKENS", 512))
    temperature: float = field(default_factory=lambda: _get_float("LLM_TEMPERATURE", 0.7))
    disable_thinking: bool = field(
        default_factory=lambda: _get_bool("LLM_DISABLE_THINKING", True)
    )
    reasoning_effort: str = field(default_factory=lambda: _get("LLM_REASONING_EFFORT", "none"))

    # Timeout in seconds
    timeout: int = field(default_factory=lambda: _get_int("LLM_TIMEOUT", 120))

    # Retry count for transient failures
    num_retries: int = field(default_factory=lambda: _get_int("LLM_NUM_RETRIES", 2))

    # Tool-calling format. Default: False = JS mindcraft pattern (LLM emits
    # plain text with `!cmd("arg", 5)` syntax, parsed by regex). The JS
    # original never sent a `tools[]` field to providers, and small models
    # like Gemma-4 26B / Qwen3-32B handle the plain-text pattern better
    # than structured tool calling. Set LLM_USE_TOOL_CALLING=true to enable
    # OpenAI tool_calls (recommended only for GPT-4 / Claude / DeepSeek-V3).
    use_tool_calling: bool = field(
        default_factory=lambda: _get_bool("LLM_USE_TOOL_CALLING", False)
    )


@dataclass
class EmbeddingSettings:
    """Embedding provider configuration."""

    model: str = field(default_factory=lambda: _get("EMBEDDING_MODEL", ""))
    base_url: str = field(default_factory=lambda: _get("EMBEDDING_BASE_URL", ""))
    api_key: str = field(default_factory=lambda: _get("EMBEDDING_API_KEY", ""))

    # Dimension of embedding vectors (used as fallback zero-vector size)
    dimension: int = field(default_factory=lambda: _get_int("EMBEDDING_DIMENSION", 1536))


@dataclass
class DispatchGateSettings:
    """Optional local classifier used before direct shortcuts vs full planner."""

    base_url: str = field(default_factory=lambda: _get("DISPATCH_GATE_BASE_URL", ""))
    model: str = field(default_factory=lambda: _get("DISPATCH_GATE_MODEL", ""))
    api_key: str = field(default_factory=lambda: _get("DISPATCH_GATE_API_KEY", "lm-studio"))
    max_tokens: int = field(default_factory=lambda: _get_int("DISPATCH_GATE_MAX_TOKENS", 192))
    timeout: float = field(default_factory=lambda: _get_float("DISPATCH_GATE_TIMEOUT", 5.0))
    direct_confidence: float = field(
        default_factory=lambda: _get_float("DISPATCH_GATE_DIRECT_CONFIDENCE", 0.85)
    )
    enabled: bool = field(default_factory=lambda: _get_bool(
        "DISPATCH_GATE_ENABLED",
        bool(_get("DISPATCH_GATE_BASE_URL", "") and _get("DISPATCH_GATE_MODEL", "")),
    ))

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.base_url and self.model)


@dataclass
class WikiRagSettings:
    """Optional local Minecraft Wiki RAG index settings."""

    enabled: bool = field(default_factory=lambda: _get_bool("WIKI_RAG_ENABLED", True))
    index_dir: str = field(default_factory=lambda: _get("WIKI_RAG_INDEX_DIR", "data/wiki"))
    embed_model: str = field(default_factory=lambda: _get("WIKI_RAG_EMBED_MODEL", "BAAI/bge-m3"))
    rerank_model: str = field(
        default_factory=lambda: _get("WIKI_RAG_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    )
    use_fp16: bool = field(default_factory=lambda: _get_bool("WIKI_RAG_USE_FP16", True))
    device: str = field(default_factory=lambda: _get("WIKI_RAG_DEVICE", "auto"))
    dense_candidates: int = field(
        default_factory=lambda: _get_int("WIKI_RAG_DENSE_CANDIDATES", 50)
    )
    rerank_candidates: int = field(
        default_factory=lambda: _get_int("WIKI_RAG_RERANK_CANDIDATES", 20)
    )


@dataclass
class BotSettings:
    """Bot identity and runtime behaviour."""

    name: str = field(default_factory=lambda: _get("BOT_NAME", "mindcraft-bot"))
    max_commands: int = field(default_factory=lambda: _get_int("BOT_MAX_COMMANDS", -1))
    narrate_behavior: bool = field(
        default_factory=lambda: _get_bool("BOT_NARRATE_BEHAVIOR", False)
    )
    show_command_syntax: str = field(
        default_factory=lambda: _get("BOT_SHOW_COMMAND_SYNTAX", "full")
    )
    blocked_actions: list[str] = field(default_factory=list)
    allow_vision: bool = field(
        default_factory=lambda: _get_bool("BOT_ALLOW_VISION", False)
    )
    spawn_timeout: int = field(
        default_factory=lambda: _get_int("BOT_SPAWN_TIMEOUT", 60)
    )

    def __post_init__(self) -> None:
        raw = _get("BOT_BLOCKED_ACTIONS", "")
        if raw:
            self.blocked_actions = [
                a.strip() for a in raw.split(",") if a.strip()
            ]


@dataclass
class Settings:
    """Top-level settings aggregator."""

    llm: LLMSettings = field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    dispatch_gate: DispatchGateSettings = field(default_factory=DispatchGateSettings)
    wiki_rag: WikiRagSettings = field(default_factory=WikiRagSettings)
    bot: BotSettings = field(default_factory=BotSettings)

    # Convenience: log level
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))


# ── Module-level singleton ──────────────────────────────────────────

settings = Settings()
