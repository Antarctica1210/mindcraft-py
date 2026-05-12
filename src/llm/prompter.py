"""
Phase 1 — LLM provider abstraction (LangChain backend).

Uses LangChain 1.0+ ChatOpenAI as the unified interface for OpenAI-compatible
LLM providers. Supports LM Studio, DeepSeek, Qwen, OpenAI, and any
OpenAI-compatible endpoint through the chain pattern:

    prompt | model | parser

Replaces the LiteLLM backend with Pydantic-typed I/O models — no **kwargs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.output_parsers import BaseOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import BaseModel, Field, SecretStr

from settings import EmbeddingSettings, LLMSettings

logger = logging.getLogger(__name__)


# ── Pydantic I/O models ────────────────────────────────────────────


class ToolCallFunction(BaseModel):
    """Function definition within a tool call (OpenAI format)."""

    name: str = ""
    arguments: str = "{}"


class ToolCallOutput(BaseModel):
    """A single tool call extracted from LLM output (OpenAI format)."""

    id: str = ""
    type: str = "function"
    function: ToolCallFunction = Field(default_factory=ToolCallFunction)


class UsageOutput(BaseModel):
    """Token usage statistics."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMInput(BaseModel):
    """Structured input for an LLM call — no **kwargs.

    Every parameter is an explicit, typed field.  The ``history`` field
    accepts plain dicts matching the existing ``MessageBuilder`` output
    format so callers don't need to construct LangChain message objects.
    """

    system_message: str = Field(description="System prompt for the LLM")
    history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Conversation turns as {role, content, tool_calls?, tool_call_id?}",
    )
    tool_choice: Literal["auto", "required", "none"] | None = Field(
        default=None,
        description="auto (default when tools registered), required (force tool call), "
        "none (text-only reply)",
    )


class LLMOutput(BaseModel):
    """Structured result from an LLM call."""

    content: str = Field(default="", description="Text content of the response")
    model: str = Field(default="", description="Model identifier")
    finish_reason: str | None = Field(default=None, description="Completion finish reason")
    tool_calls: list[ToolCallOutput] = Field(
        default_factory=list,
        description="OpenAI-format tool calls: [{id, type, function: {name, arguments}}]",
    )
    usage: UsageOutput | None = Field(default=None, description="Token usage statistics")


# ── Output parser ──────────────────────────────────────────────────


class _LLMOutputParser(BaseOutputParser[LLMOutput]):
    """Parse ChatOpenAI AIMessage → typed LLMOutput.

    Follows the langchain_core BaseOutputParser interface — drop-in compatible
    with the ``prompt | model | parser`` chain pattern.

    Handles the LangChain-internal wrapping:
    ``BaseOutputParser.invoke()`` calls ``parse_result([ChatGeneration(message=msg)])``,
    so we must unwrap: list → ChatGeneration.message → AIMessage before extracting.
    """

    model_name: str = ""

    def parse(self, text: str) -> LLMOutput:
        """Fallback: parse raw text string."""
        return LLMOutput(content=_strip_think_tags(text), model=self.model_name)

    def parse_result(self, result: Any, *, partial: bool = False) -> LLMOutput:
        """Extract LLMOutput — unwrap LangChain internals then delegate."""
        msg = self._extract_ai_message(result)
        if msg is not None:
            return self._from_ai_message(msg)
        if isinstance(result, str):
            return LLMOutput(content=_strip_think_tags(result), model=self.model_name)
        return LLMOutput(content=str(result), model=self.model_name)

    @staticmethod
    def _extract_ai_message(result: Any) -> AIMessage | None:
        """Unwrap LangChain's internal wrappers to reach the raw AIMessage.

        LangChain chains deliver the model output as:
        (a) ``[ChatGeneration(message=AIMessage)]`` — common path
        (b) ``ChatGeneration(message=AIMessage)`` — some versions
        (c) ``AIMessage`` directly — if model.invoke() called standalone
        """
        # (a) list of Generations/ChatGenerations
        if isinstance(result, list) and result:
            first = result[0]
            if hasattr(first, "message"):
                msg = first.message
                if isinstance(msg, AIMessage):
                    return msg
            if isinstance(first, AIMessage):
                return first

        # (b) single ChatGeneration
        if hasattr(result, "message"):
            msg = result.message
            if isinstance(msg, AIMessage):
                return msg

        # (c) raw AIMessage
        if isinstance(result, AIMessage):
            return result

        return None

    def _from_ai_message(self, msg: AIMessage) -> LLMOutput:
        """Convert LangChain AIMessage → LLMOutput.

        Tool calls are extracted from BOTH ``msg.tool_calls`` (LangChain
        normalized ``{name, args, id}``) AND ``msg.additional_kwargs["tool_calls"]``
        (raw OpenAI format ``{id, type, function: {name, arguments}}``).
        Both are converted to the OpenAI nested format so callers in
        ``core.py`` and ``tool_parser.py`` see a consistent shape.
        """
        raw: Any = msg.content or ""
        text = raw if isinstance(raw, str) else str(raw)
        content = _strip_think_tags(text)

        meta: dict[str, Any] = getattr(msg, "response_metadata", {}) or {}
        finish_reason: str | None = meta.get("finish_reason")

        # Collect tool calls from both LangChain-normalized and raw sources.
        raw_tool_calls: list[dict[str, Any]] = list(
            getattr(msg, "tool_calls", []) or []
        )

        # Fallback: some providers/models return tool_calls only in
        # additional_kwargs (raw OpenAI format pass-through).
        extra: dict[str, Any] = getattr(msg, "additional_kwargs", {}) or {}
        if not raw_tool_calls and extra.get("tool_calls"):
            raw_tool_calls = extra["tool_calls"]

        tool_calls: list[ToolCallOutput] = []
        for tc in raw_tool_calls:
            tool_calls.append(_normalize_tool_call(tc))

        # Token usage, preferring response_metadata, falling back to usage_metadata
        usage: UsageOutput | None = None
        token_usage = meta.get("token_usage") or meta.get("usage", {})
        if not token_usage:
            um = getattr(msg, "usage_metadata", None)
            if um:
                token_usage = {
                    "prompt_tokens": getattr(um, "input_tokens", 0) or 0,
                    "completion_tokens": getattr(um, "output_tokens", 0) or 0,
                    "total_tokens": getattr(um, "total_tokens", 0) or 0,
                }
        if token_usage:
            usage = UsageOutput(
                prompt_tokens=token_usage.get("prompt_tokens", 0),
                completion_tokens=token_usage.get("completion_tokens", 0),
                total_tokens=token_usage.get("total_tokens", 0),
            )

        # Log reasoning_content for debugging (DeepSeek/Qwen reasoning models)
        reasoning = extra.get("reasoning_content") or ""
        if reasoning:
            logger.debug("LLM reasoning_content (%d chars): %.200s", len(reasoning), reasoning)

        return LLMOutput(
            content=content,
            model=self.model_name,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            usage=usage,
        )


def _normalize_tool_call(tc: dict[str, Any]) -> ToolCallOutput:
    """Normalize a single tool_call dict → ToolCallOutput.

    Handles both formats:
      - LangChain flat:   ``{name, args (dict), id}``
      - OpenAI nested:    ``{id, type, function: {name, arguments (json str)}}``
    """
    # LangChain flat format
    if "name" in tc and "args" not in tc.get("function", {}):
        args = tc.get("args", {})
        args_str = json.dumps(args) if isinstance(args, dict) else str(args)
        return ToolCallOutput(
            id=str(tc.get("id", "")),
            type="function",
            function=ToolCallFunction(name=tc.get("name", ""), arguments=args_str),
        )

    # OpenAI nested format (or already-normalized)
    fn = tc.get("function", {})
    if isinstance(fn, dict):
        name = fn.get("name", "")
        raw_args = fn.get("arguments", "{}")
        args_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args)
    else:
        name = tc.get("name", "")
        args_str = json.dumps(tc.get("args", {}))
    return ToolCallOutput(
        id=str(tc.get("id", "")),
        type=tc.get("type", "function"),
        function=ToolCallFunction(name=name, arguments=args_str),
    )


# ── Prompter ──────────────────────────────────────────────────────


class Prompter:
    """Unified LLM interface backed by LangChain ChatOpenAI.

    Handles:
    - Async chat completion via ``ChatOpenAI.ainvoke()``
    - Context-length recovery (trim oldest turns)
    - Tool calling (OpenAI format via ``model.bind(tools=...)``)
    - Think‑tag stripping for reasoning models
    - Embeddings via ``OpenAIEmbeddings``

    Usage::

        prompter = Prompter(settings.llm, embedding=settings.embedding)
        response = await prompter.send_request(
            LLMInput(system_message="...", history=[...], tool_choice="auto")
        )
    """

    def __init__(
        self,
        llm: LLMSettings,
        *,
        embedding: EmbeddingSettings | None = None,
    ):
        self._config = llm
        self._tools: list[dict[str, Any]] | None = None

        # Strip LiteLLM provider prefix so old .env files are compatible
        # (e.g. "openai/qwen/qwen3.6-35b-a3b" → "qwen/qwen3.6-35b-a3b")
        model_name = llm.model
        for prefix in ("openai/", "anthropic/", "deepseek/"):
            if model_name.startswith(prefix):
                model_name = model_name[len(prefix) :]
                break

        self._model = ChatOpenAI(  # type: ignore[call-arg]
            model=model_name,
            openai_api_base=llm.base_url or None,
            openai_api_key=SecretStr(llm.api_key) if llm.api_key else SecretStr("not-needed"),
            temperature=llm.temperature,
            max_tokens=llm.max_tokens,
            request_timeout=llm.timeout,
            max_retries=0,  # retry loop is in send_request()
        )

        # Chain components (assembled per-call in _build_chain for tool binding)
        self._prompt = ChatPromptTemplate.from_messages([
            ("system", "{system_message}"),
            MessagesPlaceholder(variable_name="history"),
        ])

        self._embedding_config = embedding
        self._embeddings: OpenAIEmbeddings | None = None
        if embedding and embedding.model:
            emb_key = embedding.api_key or llm.api_key or "not-needed"
            self._embeddings = OpenAIEmbeddings(  # type: ignore[call-arg]
                model=embedding.model,
                openai_api_base=embedding.base_url or llm.base_url or None,
                openai_api_key=SecretStr(emb_key),
            )

    # ── properties ──────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self._config.model

    # ── tools ───────────────────────────────────────────────────────

    def register_tools(self, tools: list[dict[str, Any]]) -> None:
        """Register OpenAI-format tool definitions for tool-calling mode."""
        self._tools = tools

    # ── chat completion ─────────────────────────────────────────────

    async def send_request(self, input: LLMInput) -> LLMOutput:
        """Send a chat completion request via LangChain ChatOpenAI.

        All parameters are in the typed ``LLMInput`` model — no **kwargs.
        """
        lc_history = _dicts_to_lc_messages(input.history)
        chain = self._build_chain(tool_choice=input.tool_choice)

        for attempt in range(3):
            try:
                raw_result: Any = await chain.ainvoke({
                    "system_message": input.system_message,
                    "history": lc_history,
                })
                # LLMOutputParser.parse_result() always returns LLMOutput;
                # LangChain's Runnable.ainvoke() return type is opaque Any.
                return raw_result  # type: ignore[no-any-return]
            except Exception as exc:
                err_msg = str(exc)

                # Context window overflow → trim oldest turn and retry
                if _is_context_overflow(err_msg):
                    if len(input.history) > 1:
                        logger.warning(
                            "Context window exceeded, trimming oldest turn (%d→%d)",
                            len(input.history),
                            len(input.history) - 1,
                        )
                        return await self.send_request(
                            LLMInput(
                                system_message=input.system_message,
                                history=input.history[1:],
                                tool_choice=input.tool_choice,
                            )
                        )
                    logger.exception("Context window exceeded with single turn")
                    return LLMOutput(
                        content="Message too long, please shorten your request.",
                        model=self.model,
                    )

                # Transient error → backoff and retry
                if _is_transient_error(err_msg) and attempt < 2:
                    delay = 1.5 * (attempt + 1)
                    logger.warning(
                        "LLM transient error (attempt %d/3), retrying in %.1fs: %s",
                        attempt + 1,
                        delay,
                        err_msg[:120],
                    )
                    await asyncio.sleep(delay)
                    continue

                logger.exception("ChatOpenAI request failed (attempt %d)", attempt + 1)
                return LLMOutput(
                    content="My brain disconnected, try again.",
                    model=self.model,
                )
        else:
            logger.error("LLM request failed after 3 attempts")
            return LLMOutput(
                content="My brain disconnected, try again.",
                model=self.model,
            )

    # ── embeddings ──────────────────────────────────────────────────

    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector via OpenAIEmbeddings.

        Returns a zero vector if no embedding model is configured or on failure.
        """
        if self._embeddings is None:
            dim = self._embedding_config.dimension if self._embedding_config else 1536
            return [0.0] * dim

        text = text[:8191] if len(text) > 8191 else text
        try:
            result = await self._embeddings.aembed_query(text)
            return list(result)
        except Exception:
            logger.exception("Embedding failed, returning zero vector")
            dim = self._embedding_config.dimension if self._embedding_config else 1536
            return [0.0] * dim

    # ── internal ────────────────────────────────────────────────────

    def _build_chain(self, *, tool_choice: str | None = None) -> Any:
        """Build the ``prompt | model | parser`` chain, conditionally binding tools.

        Per-call chain construction is cheap (Runnables are lightweight objects)
        and lets us vary tool binding per invocation — some calls need tools,
        others are pure-text (question / chitchat intents).
        """
        parser = _LLMOutputParser(model_name=self.model)

        if self._tools and tool_choice != "none":
            bound_model = self._model.bind(
                tools=self._tools,
                tool_choice=tool_choice or "auto",
            )
            return self._prompt | bound_model | parser

        if tool_choice == "none":
            # Explicitly disable tool calling for question/chitchat intents
            bound_model = self._model.bind(tool_choice="none")
            return self._prompt | bound_model | parser

        # No tools registered, no tool_choice override → plain model
        return self._prompt | self._model | parser


# ── Message conversion ─────────────────────────────────────────────


def _dicts_to_lc_messages(turns: list[dict[str, Any]]) -> list[BaseMessage]:
    """Convert dict-format conversation turns to LangChain message objects.

    Preserves tool-call protocol round-tripping: assistant turns with
    ``tool_calls`` are converted to ``AIMessage(tool_calls=[...])`` and
    follow-up tool results become ``ToolMessage`` with matching ``tool_call_id``.

    Accepts tool_calls as either raw OpenAI dicts or Pydantic ``ToolCallOutput``
    models (which are serialised to dicts before passing to LangChain).
    """
    messages: list[BaseMessage] = []
    for turn in turns:
        content = str(turn.get("content", ""))
        role = turn.get("role", "user")

        if role == "assistant":
            lc_tool_calls = _serialise_tool_calls(turn.get("tool_calls"))
            if lc_tool_calls:
                msg = AIMessage(content=content, tool_calls=lc_tool_calls)
            else:
                msg = AIMessage(content=content)
            messages.append(msg)
        elif role == "tool":
            messages.append(
                ToolMessage(
                    content=content,
                    tool_call_id=str(turn.get("tool_call_id", "")),
                )
            )
        else:
            messages.append(HumanMessage(content=content))

    return messages


def _serialise_tool_calls(
    raw: list[Any] | None,
) -> list[dict[str, Any]] | None:
    """Convert a mixed list of dicts / ToolCallOutput → LangChain-compatible dicts.

    LangChain AIMessage expects ``[{name, args (dict), id, type}]`` format.
    """
    if not raw:
        return None

    out: list[dict[str, Any]] = []
    for tc in raw:
        if isinstance(tc, dict):
            # Already a dict — use as-is (OpenAI format → LangChain format)
            fn = tc.get("function", {})
            if isinstance(fn, dict) and "name" in fn:
                args = fn.get("arguments", "{}")
                out.append({
                    "name": fn.get("name", ""),
                    "args": json.loads(args) if isinstance(args, str) else args,
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "tool_call"),
                })
            else:
                out.append(tc)
        elif hasattr(tc, "function"):
            # ToolCallOutput (Pydantic model)
            args_str = tc.function.arguments
            out.append({
                "name": tc.function.name,
                "args": json.loads(args_str) if isinstance(args_str, str) else args_str,
                "id": tc.id,
                "type": tc.type,
            })
    return out or None


# ── Think-tag stripping ────────────────────────────────────────────


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks from reasoning model output."""
    result = re.sub(r"<think>[\s\S]*?</think>", "", text)
    if "<think>" in result:
        result = result.split("<think>")[0]
    return result.strip()


# ── Error classification ──────────────────────────────────────────


_CONTEXT_OVERFLOW_PATTERNS: tuple[str, ...] = (
    "context_length_exceeded",
    "maximum context length",
    "reduce the length of the messages",
    "too many tokens",
    "requested token count exceeds",
)


def _is_context_overflow(error_message: str) -> bool:
    """Return True if the error message indicates context-length exceeded."""
    lower = error_message.lower()
    return any(pat in lower for pat in _CONTEXT_OVERFLOW_PATTERNS)


_TRANSIENT_ERROR_PATTERNS: tuple[str, ...] = (
    "model reloaded",
    "model is still loading",
    "overloaded",
    "rate_limit",
    "rate limit",
    "timed out",
    "timeout",
    "connection",
    "service unavailable",
    "internal server error",
    "server error",
    "bad gateway",
    "too many requests",
    "capacity",
    "try again",
)


def _is_transient_error(error_message: str) -> bool:
    """Return True if the error is likely transient and worth retrying."""
    lower = error_message.lower()
    return any(pat in lower for pat in _TRANSIENT_ERROR_PATTERNS)
