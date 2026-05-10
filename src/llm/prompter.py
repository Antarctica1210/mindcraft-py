"""
Phase 1 — LLM provider abstraction (LiteLLM backend).

Uses LiteLLM as the unified interface for 100+ LLM providers.
Supports LM Studio, DeepSeek, Qwen, OpenAI, Anthropic, and any
OpenAI-compatible endpoint through a single `acompletion()` call.

Replaces direct `openai.OpenAI` client construction with LiteLLM's
built-in provider routing, retry, and error handling.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import litellm
from litellm import acompletion
from litellm.exceptions import ContextWindowExceededError

from settings import EmbeddingSettings, LLMSettings

logger = logging.getLogger(__name__)

# ── LiteLLM configuration ───────────────────────────────────────────

# Drop noisy LiteLLM internal logs (set to False to debug provider issues)
litellm.suppress_debug_info = True


# ── Response dataclass ──────────────────────────────────────────────


@dataclass
class LLMResponse:
    """Structured result from an LLM call."""

    content: str
    model: str
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


# ── Prompter ────────────────────────────────────────────────────────


class Prompter:
    """Unified LLM interface backed by LiteLLM.

    Handles:
    - Async chat completion via `litellm.acompletion()`
    - Context-length recovery (trim oldest turns)
    - Tool calling (OpenAI format)
    - Think‑tag stripping for reasoning models
    - Embeddings via LiteLLM or compatible endpoint

    Usage:
        prompter = Prompter(settings.llm, embedding=settings.embedding)
        response = await prompter.send_request(turns=[...], system_message="...")
    """

    def __init__(
        self,
        llm: LLMSettings,
        *,
        embedding: EmbeddingSettings | None = None,
    ):
        self._llm = llm
        self._embedding = embedding
        self._tools: list[dict[str, Any]] | None = None

    # ── properties ──────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self._llm.model

    # ── tools ───────────────────────────────────────────────────────

    def register_tools(self, tools: list[dict[str, Any]]) -> None:
        """Register OpenAI-format tool definitions for tool-calling mode."""
        self._tools = tools

    # ── chat completion ─────────────────────────────────────────────

    async def send_request(
        self,
        turns: list[dict[str, str]],
        system_message: str,
        *,
        stop_sequence: str = "***",
    ) -> LLMResponse:
        """Send a chat completion request through LiteLLM.

        Args:
            turns: Conversation history as [{"role": ..., "content": ...}].
            system_message: System prompt.
            stop_sequence: Token appended to each user/assistant message.

        Returns:
            LLMResponse with content and metadata.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_message}
        ]
        for turn in turns:
            content = turn.get("content", "")
            messages.append({
                "role": turn.get("role", "user"),
                "content": f"{content}{stop_sequence}",
            })

        kwargs = self._build_kwargs(messages)

        logger.info(
            "LiteLLM → model=%s msgs=%d temp=%.1f tools=%s",
            self._llm.model,
            len(messages),
            self._llm.temperature,
            bool(self._tools),
        )

        try:
            response = await acompletion(**kwargs)
        except ContextWindowExceededError:
            if len(turns) > 1:
                logger.warning(
                    "Context window exceeded, trimming oldest turn (%d→%d)",
                    len(turns), len(turns) - 1,
                )
                return await self.send_request(
                    turns[1:], system_message, stop_sequence=stop_sequence
                )
            logger.exception("Context window exceeded with single turn")
            return LLMResponse(
                content="Message too long, please shorten your request.",
                model=self._llm.model,
            )
        except Exception as exc:
            err_msg = str(exc)
            if "context_length_exceeded" in err_msg.lower() and len(turns) > 1:
                logger.warning("Context length exceeded, trimming oldest turn")
                return await self.send_request(
                    turns[1:], system_message, stop_sequence=stop_sequence
                )
            logger.exception("LiteLLM request failed")
            return LLMResponse(
                content="My brain disconnected, try again.",
                model=self._llm.model,
            )

        return self._parse_response(response)

    # ── embeddings ──────────────────────────────────────────────────

    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector via LiteLLM.

        Falls back to the chat model if no embedding model is configured.
        """
        model = self._embedding.model if self._embedding else ""
        if not model:
            model = self._llm.model

        dim = self._embedding.dimension if self._embedding else 1536

        # Truncate text (rough token estimate: 4 chars ≈ 1 token)
        text = text[:8191] if len(text) > 8191 else text

        try:
            resp = await litellm.aembedding(
                model=model,
                input=text,
                **(self._extra_provider_kwargs(for_embedding=True)),
            )
            return list(resp.data[0]["embedding"])
        except Exception:
            logger.exception("Embedding failed, returning zero vector")
            return [0.0] * dim

    # ── internal ────────────────────────────────────────────────────

    def _build_kwargs(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Build the kwargs dict for litellm.acompletion()."""
        kwargs: dict[str, Any] = {
            "model": self._llm.model,
            "messages": messages,
            "temperature": self._llm.temperature,
            "max_tokens": self._llm.max_tokens,
            "timeout": self._llm.timeout,
            "num_retries": self._llm.num_retries,
        }

        # LiteLLM reads api_base / api_key for custom OpenAI-compatible endpoints
        if self._llm.base_url:
            kwargs["api_base"] = self._llm.base_url
        if self._llm.api_key:
            kwargs["api_key"] = self._llm.api_key

        if self._tools:
            kwargs["tools"] = self._tools
            kwargs["tool_choice"] = "auto"

        return kwargs

    def _extra_provider_kwargs(
        self, *, for_embedding: bool = False
    ) -> dict[str, Any]:
        """Extra kwargs for embedding calls (api_base, api_key)."""
        extra: dict[str, Any] = {}
        if for_embedding and self._embedding:
            if self._embedding.base_url:
                extra["api_base"] = self._embedding.base_url
            if self._embedding.api_key:
                extra["api_key"] = self._embedding.api_key
        elif not for_embedding:
            if self._llm.base_url:
                extra["api_base"] = self._llm.base_url
            if self._llm.api_key:
                extra["api_key"] = self._llm.api_key
        return extra

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse a LiteLLM ModelResponse into our LLMResponse."""
        choice = response.choices[0]

        if choice.finish_reason == "length":
            return LLMResponse(
                content="Response too long, please simplify your request.",
                model=self._llm.model,
                finish_reason="length",
            )

        content = choice.message.content or ""

        # Strip <think>...</think> blocks from reasoning models
        content = _strip_think_tags(content)

        # Extract tool calls
        tool_calls_raw: list[dict[str, Any]] = []
        if hasattr(choice.message, "tool_calls") and choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls_raw.append({
                    "id": getattr(tc, "id", ""),
                    "name": tc.function.name if hasattr(tc, "function") else "",
                    "arguments": tc.function.arguments if hasattr(tc, "function") else "{}",
                })

        usage: dict[str, int] = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(response.usage, "total_tokens", 0) or 0,
            }

        logger.info(
            "LiteLLM ← model=%s chars=%d finish=%s",
            self._llm.model, len(content), choice.finish_reason,
        )
        return LLMResponse(
            content=content,
            model=self._llm.model,
            finish_reason=choice.finish_reason,
            tool_calls=tool_calls_raw,
            usage=usage,
        )


# ── Think-tag stripping ─────────────────────────────────────────────


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks from reasoning model output."""
    import re

    result = re.sub(r"<think>[\s\S]*?</think>", "", text)
    if "<think>" in result:
        result = result.split("<think>")[0]
    return result.strip()
