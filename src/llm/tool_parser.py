"""
Phase 1 — LLM response parser.

Extracts structured command calls from LLM text output.
Supports both:
    1. Legacy !command(args) syntax (regex, JS-compatible)
    2. OpenAI tool-calls format (when providers support it)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Matches !command(args) where args are either positional or keyword.
#   Positional: !moveTo(642, 71, 481)
#   Keyword:    !moveTo(x=642, y=71, z=481)
#   Mixed:      !collectBlocks("oak_log", count=5)
# Captures: group 1 = command name, group 2 = optional args string
_SIMPLE_VAL = r'(?:-?\d+(?:\.\d+)?|true|false|"[^"]*"|[^,)]+)'
_KW_VAL = r'(?:\w+=(?:-?\d+(?:\.\d+)?|true|false|"[^"]*"|[^,)]+))'
_ARG = rf'(?:{_KW_VAL}|{_SIMPLE_VAL})'
_COMMAND_PATTERN = re.compile(
    rf'!([\w:]+)'
    rf'(?:\(({_ARG}(?:\s*,\s*{_ARG})*)\))?'
)


@dataclass
class CommandCall:
    """A parsed command extracted from LLM output."""

    name: str          # e.g. "collectBlocks"
    args: list[Any]    # e.g. ["oak_log", 5]
    raw: str            # original matched text
    source: str = "regex"  # "regex" or "tool_call"


@dataclass
class ParseResult:
    """Result of parsing an LLM response."""

    text: str                    # Cleaned text (no command) or full response
    commands: list[CommandCall]  # Parsed commands in order
    has_command: bool


def parse_legacy(response_text: str) -> list[CommandCall]:
    """Extract !command(args) calls from text using JS-compatible regex.

    This mirrors the exact regex and behavior of
    `containsCommand()` / `parseCommandMessage()` in
    `src/agent/commands/index.js`.
    """
    commands: list[CommandCall] = []
    for match in _COMMAND_PATTERN.finditer(response_text):
        name = match.group(1)
        args_str = match.group(2)

        # Strip action_ / action: prefix (tool-calling convention)
        if name.startswith("action_"):
            name = name[7:]
        elif name.startswith("action:"):
            name = name[7:]

        args: list[Any] = []
        if args_str:
            args = _parse_args(args_str)

        commands.append(CommandCall(
            name=name,
            args=args,
            raw=match.group(0),
        ))

    return commands


def parse_tool_calls(tool_calls: list[dict[str, Any]]) -> list[CommandCall]:
    """Convert OpenAI tool_calls into CommandCall objects.

    Handles both the OpenAI-native format
        {"id": "...", "type": "function", "function": {"name": "...", "arguments": "..."}}
    and the legacy flattened format
        {"id": "...", "name": "...", "arguments": "..."}.
    """
    commands: list[CommandCall] = []
    for tc in tool_calls:
        # OpenAI-native format: name/arguments live inside "function"
        fn = tc.get("function", {})
        if fn and isinstance(fn, dict):
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
        else:
            # Legacy flattened format (backwards compatibility)
            name = tc.get("name", "")
            raw_args = tc.get("arguments", "{}")

        # Remove "action:" prefix if present (JS convention)
        if name.startswith("action:"):
            name = name[7:]

        arguments: dict[str, Any] = {}
        if isinstance(raw_args, str):
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                logger.warning("Failed to parse tool call arguments: %s", raw_args)
        elif isinstance(raw_args, dict):
            arguments = raw_args

        commands.append(CommandCall(
            name=name,
            args=list(arguments.values()),
            raw=json.dumps(tc),
            source="tool_call",
        ))

    return commands


# Model-specific tokens that sometimes leak into text output.
# Stripped before parsing so they don't interfere with command extraction.
_MODEL_ARTIFACT_PATTERNS = [
    # Gemma / proprietary model channel markers: "<|channel>string", "<|channel>system"
    re.compile(r"<\|channel>[^\n]*", re.IGNORECASE),
    # ChatML delimiters
    re.compile(r"<\|im_start\|>.*?<\|im_end\|>", re.DOTALL),
    re.compile(r"<\|(?:assistant|user|system)\|?>", re.IGNORECASE),
]


def _clean_model_artifacts(text: str) -> str:
    """Remove known model-specific tokens that leak into responses.

    Lines that start with a model artifact token are dropped entirely
    (since the residual text after stripping, e.g. "string", is meaningless).
    Embedded artifacts in otherwise-valid lines are stripped in-place.
    """
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Drop lines that are nothing but artifact residues
        for pat in _MODEL_ARTIFACT_PATTERNS:
            stripped = pat.sub("", stripped).strip()
        if not stripped:
            continue
        cleaned_lines.append(stripped)
    return "\n".join(cleaned_lines).strip()


def parse_response(
    response_text: str,
    tool_calls: list[dict[str, Any]] | None = None,
) -> ParseResult:
    """Parse an LLM response into text + structured commands.

    Priority:
        1. If tool_calls present, use them (higher accuracy).
        2. Otherwise fall back to regex parsing of !command syntax.

    Returns:
        ParseResult with cleaned text and parsed commands.
    """
    commands: list[CommandCall] = []

    # Try tool calls first
    if tool_calls:
        commands = parse_tool_calls(tool_calls)
        if commands:
            return ParseResult(
                text=response_text,
                commands=commands,
                has_command=True,
            )

    # Clean model-specific artifacts before regex parsing
    response_text = _clean_model_artifacts(response_text)

    # Fall back to regex
    commands = parse_legacy(response_text)

    # Remove command text from response for display
    cleaned = response_text
    for cmd in commands:
        cleaned = cleaned.replace(cmd.raw, "", 1)
    cleaned = cleaned.strip()

    return ParseResult(
        text=cleaned,
        commands=commands,
        has_command=len(commands) > 0,
    )


def has_command(response_text: str) -> bool:
    """Quick check: does the response contain a command?"""
    return bool(_COMMAND_PATTERN.search(response_text))


# ── Internal helpers ──────────────────────────────────────────────


def _parse_args(args_str: str) -> list[Any]:
    """Parse comma-separated arguments from the regex capture group.

    Handles two formats:
        Positional:  '"oak_log", 5'          → ['oak_log', 5]
        Keyword:     'x=642, y=71, z=481'    → [642, 71, 481]
        Single-quoted: "player_name='Wilson'" → ['Wilson', ...]
    """
    args: list[Any] = []
    # Match double-quoted strings, single-quoted strings, or non-comma sequences
    parts = re.findall(r"""(?:"[^"]*"|'[^']*'|[^,]+)""", args_str)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Strip keyword prefix: "x=642" → "642" (but not inside quotes)
        if "=" in part and not (part.startswith('"') or part.startswith("'")):
            part = part.split("=", 1)[1].strip()

        # Double-quoted string
        if part.startswith('"') and part.endswith('"'):
            args.append(part[1:-1])
        # Single-quoted string (some models prefer this)
        elif part.startswith("'") and part.endswith("'"):
            args.append(part[1:-1])
        elif part.lower() == "true":
            args.append(True)
        elif part.lower() == "false":
            args.append(False)
        elif "." in part:
            try:
                args.append(float(part))
            except ValueError:
                args.append(part)
        else:
            try:
                args.append(int(part))
            except ValueError:
                args.append(part)

    return args
