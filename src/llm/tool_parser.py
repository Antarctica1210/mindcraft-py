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

# Mirrors JS regex: /!(\w+)(?:\((...params...)\))?/
# Captures: group 1 = command name, group 2 = optional args string
_COMMAND_PATTERN = re.compile(
    r'!(\w+)'
    r'(?:\(('
    r'(?:-?\d+(?:\.\d+)?|true|false|"[^"]*")'
    r'(?:\s*,\s*(?:-?\d+(?:\.\d+)?|true|false|"[^"]*"))*'
    r')\))?'
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
    """Convert OpenAI tool_calls into CommandCall objects."""
    commands: list[CommandCall] = []
    for tc in tool_calls:
        name = tc.get("name", "")
        # Remove "action:" prefix if present (JS convention)
        if name.startswith("action:"):
            name = name[7:]

        arguments: dict[str, Any] = {}
        raw_args = tc.get("arguments", "{}")
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

    # Fall back to regex
    commands = parse_legacy(response_text)

    # Remove command text from response for display
    cleaned = response_text
    for cmd in commands:
        cleaned = cleaned.replace(cmd.raw, "", 1)
    cleaned = cleaned.strip()

    return ParseResult(
        text=cleaned or response_text,
        commands=commands,
        has_command=len(commands) > 0,
    )


def has_command(response_text: str) -> bool:
    """Quick check: does the response contain a command?"""
    return bool(_COMMAND_PATTERN.search(response_text))


# ── Internal helpers ──────────────────────────────────────────────


def _parse_args(args_str: str) -> list[Any]:
    """Parse comma-separated arguments from the regex capture group.

    Handles: integers, floats, booleans, quoted strings.

    Examples:
        '"oak_log", 5' → ['oak_log', 5]
        '"steve", 3, true' → ['steve', 3, True]
        '1.5, -10' → [1.5, -10]
    """
    args: list[Any] = []
    # Split by comma, respecting quoted strings
    parts = re.findall(r'(?:"[^"]*"|[^,]+)', args_str)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Quoted string
        if part.startswith('"') and part.endswith('"'):
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
