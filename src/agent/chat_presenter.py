"""Player-facing chat presentation helpers."""
from __future__ import annotations

import re

MAX_IN_GAME_CHAT_CHARS = 180

_MISSING_TOOL_RE = re.compile(
    r"Need a (?P<tool>[A-Za-z0-9_]+) to mine (?P<target>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
_MISSING_MATERIALS_RE = re.compile(
    r"Missing materials for (?P<target>[A-Za-z0-9_]+): (?P<missing>.+?)(?:\.|\n|$)",
    re.IGNORECASE,
)


def truncate_chat_line(text: str, *, limit: int = MAX_IN_GAME_CHAT_CHARS) -> str:
    clean = " ".join(text.strip().split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."


def first_result_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return text.strip()


def compact_command_result_for_chat(
    result: str,
    *,
    default_prefix: str,
) -> str:
    """Convert verbose command output into a short player-facing status line."""
    text = result.strip()
    if text.startswith("FAILED:"):
        text = text[len("FAILED:"):].strip()
        default_prefix = "Failed"

    missing_tool = _MISSING_TOOL_RE.search(text)
    if missing_tool is not None:
        return (
            f"Failed: need {missing_tool.group('tool')} "
            f"for {missing_tool.group('target')}."
        )

    missing_materials = _MISSING_MATERIALS_RE.search(text)
    if missing_materials is not None:
        return truncate_chat_line(
            f"Failed: missing {missing_materials.group('missing').strip()} "
            f"for {missing_materials.group('target')}."
        )

    timeout = re.search(r"Request '([^']+)' timed out after (\d+)s", text)
    if timeout is not None:
        return f"Failed: {timeout.group(1)} timed out after {timeout.group(2)}s."

    nearby_miss = re.search(r"No ([A-Za-z0-9_]+) nearby", text)
    if nearby_miss is not None:
        return f"Failed: no {nearby_miss.group(1)} nearby."

    first_line = first_result_line(text)
    lower = first_line.lower()
    if default_prefix == "Done" and lower.startswith("crafting plan for "):
        return "Done: checked crafting plan."
    if default_prefix == "Done" and lower.startswith("inventory"):
        return "Done: checked inventory."
    if default_prefix == "Done" and lower.startswith("gave "):
        return truncate_chat_line(f"Done: {first_line[0].lower()}{first_line[1:]}")

    return truncate_chat_line(f"{default_prefix}: {first_line}")


def compact_chat_text(text: str) -> str:
    """Keep Minecraft chat concise; full details stay in logs/events."""
    stripped = text.strip()
    if not stripped:
        return ""

    if stripped.startswith("Done:"):
        return compact_command_result_for_chat(
            stripped[len("Done:"):],
            default_prefix="Done",
        )
    if stripped.startswith("Failed:"):
        return compact_command_result_for_chat(
            stripped[len("Failed:"):],
            default_prefix="Failed",
        )
    if "\n" in stripped or len(stripped) > MAX_IN_GAME_CHAT_CHARS:
        return truncate_chat_line(first_result_line(stripped))
    return stripped
