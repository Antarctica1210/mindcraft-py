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
import time
from datetime import datetime, timezone
from pathlib import Path
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
_RAW_RESPONSE_LOGGER_NAME = "llm.raw_response"
_INTERACTION_LOGGER_NAME = "llm.interaction"


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
        text = _coerce_content_to_text(raw)
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
        if not raw_tool_calls:
            raw_tool_calls = _recover_invalid_tool_calls(
                getattr(msg, "invalid_tool_calls", []) or []
            )

        tool_calls: list[ToolCallOutput] = []
        for tc in raw_tool_calls:
            tool_calls.append(_normalize_tool_call(tc))

        # Some reasoning endpoints put the only natural-language payload in
        # message.reasoning_content / additional_kwargs["reasoning_content"].
        # Use it only when there is no normal content and no tool call; for
        # action responses, content="" + finish_reason="tool_calls" is expected.
        reasoning = _extract_reasoning_content(msg, extra)
        if not content and not tool_calls and reasoning:
            content = _strip_think_tags(reasoning)

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
        if reasoning:
            logger.debug("LLM reasoning_content (%d chars): %.200s", len(reasoning), reasoning)
        if not content and not tool_calls:
            invalid_tool_calls = getattr(msg, "invalid_tool_calls", []) or []
            logger.warning(
                "LLM response parsed empty: finish_reason=%r metadata=%s "
                "additional_kwargs=%s invalid_tool_calls=%s",
                finish_reason,
                _safe_preview(meta),
                _safe_preview(extra),
                _safe_preview(invalid_tool_calls),
            )

        return LLMOutput(
            content=content,
            model=self.model_name,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            usage=usage,
        )


def _coerce_content_to_text(raw: Any) -> str:
    """Extract text from LangChain/OpenAI content shapes."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(raw)


def _recover_invalid_tool_calls(raw: list[Any]) -> list[dict[str, Any]]:
    """Recover provider tool calls LangChain marked invalid.

    Some OpenAI-compatible servers return a tool call with arguments that
    LangChain cannot parse into a dict.  The raw name/args are still useful to
    our executor, so keep them instead of treating the whole response as empty.
    """
    recovered: list[dict[str, Any]] = []
    for tc in raw:
        if isinstance(tc, dict):
            name = tc.get("name") or tc.get("function", {}).get("name", "")
            args = (
                tc.get("args")
                or tc.get("arguments")
                or tc.get("function", {}).get("arguments", "{}")
            )
            recovered.append({
                "id": tc.get("id", ""),
                "type": tc.get("type", "function"),
                "function": {"name": name, "arguments": args},
            })
            continue
        name = getattr(tc, "name", "")
        args = getattr(tc, "args", None) or getattr(tc, "arguments", "{}")
        recovered.append({
            "id": getattr(tc, "id", ""),
            "type": getattr(tc, "type", "function"),
            "function": {"name": name, "arguments": args},
        })
    return recovered


def _extract_reasoning_content(msg: AIMessage, extra: dict[str, Any]) -> str:
    """Return provider-specific reasoning text, if LangChain preserved it."""
    value = extra.get("reasoning_content") or getattr(msg, "reasoning_content", "")
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return _coerce_content_to_text(value)


def _safe_preview(value: Any, *, limit: int = 1200) -> str:
    """JSON-ish one-line preview for diagnostics without flooding logs."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    return text[:limit]


def _to_jsonable(value: Any) -> Any:
    """Convert LangChain/Pydantic objects into JSON-serializable data."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _raw_response_logger() -> logging.Logger:
    """Logger dedicated to complete LLM response payloads."""
    raw_logger = logging.getLogger(_RAW_RESPONSE_LOGGER_NAME)
    raw_logger.setLevel(logging.INFO)
    raw_logger.propagate = False

    if not raw_logger.handlers:
        log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        handler = logging.FileHandler(
            log_dir / f"{today}.llm_responses.jsonl",
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        raw_logger.addHandler(handler)

    return raw_logger


def _jsonl_logger(name: str, filename_suffix: str) -> logging.Logger:
    """Return a dedicated JSONL logger under logs/."""
    json_logger = logging.getLogger(name)
    json_logger.setLevel(logging.INFO)
    json_logger.propagate = False

    if not json_logger.handlers:
        log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        handler = logging.FileHandler(
            log_dir / f"{today}.{filename_suffix}.jsonl",
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        json_logger.addHandler(handler)

    return json_logger


def _log_raw_response(
    *,
    model: str,
    tool_choice: str | None,
    attempt: int,
    raw_result: Any,
    parsed: LLMOutput | None = None,
) -> None:
    """Append one full raw LLM response record to the debug JSONL log."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "tool_choice": tool_choice,
        "attempt": attempt,
        "raw_type": type(raw_result).__name__,
        "raw_response": _to_jsonable(raw_result),
        "parsed": _to_jsonable(parsed) if parsed is not None else None,
    }
    _raw_response_logger().info(json.dumps(record, ensure_ascii=False, default=str))


def _log_llm_interaction(
    *,
    model: str,
    tool_choice: str | None,
    attempt: int,
    elapsed_ms: float,
    system_message: str,
    history: list[dict[str, Any]],
    parsed: LLMOutput,
) -> None:
    """Append one full LLM interaction record for debugging."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "tool_choice": tool_choice,
        "attempt": attempt,
        "elapsed_ms": round(elapsed_ms, 2),
        "request": {
            "system_chars": len(system_message),
            "history_count": len(history),
            "system_message": system_message,
            "history": _to_jsonable(history),
        },
        "response": _to_jsonable(parsed),
    }
    _jsonl_logger(_INTERACTION_LOGGER_NAME, "llm_interactions").info(
        json.dumps(record, ensure_ascii=False, default=str)
    )


def _allowed_tool_names(tools: list[dict[str, Any]]) -> set[str]:
    """Return registered tool/function names from OpenAI-style tool defs."""
    names: set[str] = set()
    for tool in tools:
        fn = tool.get("function", {})
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


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

        model_kwargs: dict[str, Any] = {}
        if llm.disable_thinking:
            model_kwargs["reasoning_effort"] = llm.reasoning_effort or "none"
            model_kwargs["extra_body"] = {
                "enable_thinking": False,
                "enableThinking": False,
                "chat_template_kwargs": {
                    "enable_thinking": False,
                    "enableThinking": False,
                },
            }
            logger.info(
                "LLM thinking disabled (reasoning_effort=%s)",
                model_kwargs["reasoning_effort"],
            )

        self._model = ChatOpenAI(  # type: ignore[call-arg]
            model=model_name,
            openai_api_base=llm.base_url or None,
            openai_api_key=SecretStr(llm.api_key) if llm.api_key else SecretStr("not-needed"),
            temperature=llm.temperature,
            max_tokens=llm.max_tokens,
            request_timeout=llm.timeout,
            max_retries=0,  # retry loop is in send_request()
            **model_kwargs,
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
        """Register OpenAI-format tool definitions for tool-calling mode.

        When ``LLMSettings.use_tool_calling`` is False (default — JS
        mindcraft mode), this no-ops: the tools are documented in the
        system prompt's ``$COMMAND_DOCS`` placeholder instead, and the
        LLM emits ``!cmd("arg", 5)`` plain-text syntax that
        :py:func:`tool_parser.parse_legacy` extracts via regex.
        """
        if not self._config.use_tool_calling:
            logger.info(
                "Prompter: plain-text mode active (use_tool_calling=False) — "
                "ignoring %d tool definitions; commands come via $COMMAND_DOCS",
                len(tools),
            )
            self._tools = None
            return
        self._tools = tools

    # ── chat completion ─────────────────────────────────────────────

    async def send_request(self, input: LLMInput) -> LLMOutput:
        """Send a chat completion request via LangChain ChatOpenAI.

        All parameters are in the typed ``LLMInput`` model — no **kwargs.
        """
        lc_history = _dicts_to_lc_messages(input.history)
        chain = self._build_model_chain(tool_choice=input.tool_choice)
        parser = _LLMOutputParser(model_name=self.model)

        for attempt in range(3):
            try:
                started = time.perf_counter()
                logger.info(
                    "LLM request start: model=%s tool_choice=%s history=%d "
                    "system_chars=%d tools=%s attempt=%d/3",
                    self.model,
                    input.tool_choice,
                    len(input.history),
                    len(input.system_message),
                    bool(self._tools),
                    attempt + 1,
                )
                raw_result: Any = await chain.ainvoke({
                    "system_message": input.system_message,
                    "history": lc_history,
                })
                elapsed_ms = (time.perf_counter() - started) * 1000
                parsed = parser.parse_result(raw_result)
                parsed = self._filter_unknown_tool_calls(parsed)
                usage = parsed.usage
                logger.info(
                    "LLM response parsed: model=%s elapsed_ms=%.1f "
                    "content_chars=%d tool_calls=%d finish=%s usage=%s",
                    self.model,
                    elapsed_ms,
                    len(parsed.content),
                    len(parsed.tool_calls),
                    parsed.finish_reason,
                    usage.model_dump() if usage else None,
                )
                _log_raw_response(
                    model=self.model,
                    tool_choice=input.tool_choice,
                    attempt=attempt + 1,
                    raw_result=raw_result,
                    parsed=parsed,
                )
                _log_llm_interaction(
                    model=self.model,
                    tool_choice=input.tool_choice,
                    attempt=attempt + 1,
                    elapsed_ms=elapsed_ms,
                    system_message=input.system_message,
                    history=input.history,
                    parsed=parsed,
                )
                return parsed
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

    def _filter_unknown_tool_calls(self, output: LLMOutput) -> LLMOutput:
        """Drop model-invented tool calls before they reach the executor."""
        if not output.tool_calls or not self._tools:
            return output

        allowed = _allowed_tool_names(self._tools)
        kept: list[ToolCallOutput] = []
        dropped: list[str] = []
        for tc in output.tool_calls:
            name = tc.function.name
            if name in allowed:
                kept.append(tc)
            else:
                dropped.append(name)

        if dropped:
            logger.warning(
                "Dropped unknown LLM tool calls: %s",
                ", ".join(name[:120] for name in dropped),
            )
            return output.model_copy(update={"tool_calls": kept})

        return output

    def _build_model_chain(self, *, tool_choice: str | None = None) -> Any:
        """Build the ``prompt | model`` chain, conditionally binding tools.

        Per-call chain construction is cheap (Runnables are lightweight objects)
        and lets us vary tool binding per invocation — some calls need tools,
        others are pure-text (question / chitchat intents).

        When ``LLMSettings.use_tool_calling`` is False (JS mindcraft mode),
        tools are NEVER bound regardless of ``tool_choice``; commands are
        documented in the system prompt and parsed from response text by
        :py:func:`tool_parser.parse_legacy` regex.
        """
        if not self._config.use_tool_calling:
            # Plain-text mode — no tools bound, no tool_choice sent.
            # The model behaves like the JS original: emits free text
            # with `!cmd("arg", 5)` syntax for actions.
            return self._prompt | self._model

        if self._tools and tool_choice != "none":
            bound_model = self._model.bind(
                tools=self._tools,
                tool_choice=tool_choice or "auto",
            )
            return self._prompt | bound_model

        if tool_choice == "none":
            # Explicitly disable tool calling for question/chitchat intents
            bound_model = self._model.bind(tool_choice="none")
            return self._prompt | bound_model

        # No tools registered, no tool_choice override → plain model
        return self._prompt | self._model


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
