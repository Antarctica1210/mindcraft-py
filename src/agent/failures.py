"""Failure classification helpers for command recovery."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MISSING_TOOL_RE = re.compile(
    r"Need a (?P<tool>[A-Za-z0-9_]+) to mine (?P<target>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
MISSING_MATERIALS_RE = re.compile(
    r"Missing materials for (?P<target>[A-Za-z0-9_]+): (?P<missing>.+?)(?:\.|\n|$)",
    re.IGNORECASE,
)
NO_FUEL_RE = re.compile(
    r"\bNo fuel to smelt (?P<target>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
NOT_ENOUGH_RE = re.compile(
    r"\bNot enough (?P<item>[A-Za-z0-9_]+)\s+"
    r"\(have\s+(?P<have>\d+),\s+need\s+(?P<need>\d+)\)",
    re.IGNORECASE,
)
NEARBY_MISS_RE = re.compile(
    r"\bNo\s+(?P<target>[A-Za-z0-9_]+)\s+nearby\b|"
    r"\bNo\s+breakable\s+(?P<breakable>[A-Za-z0-9_]+)\s+within\b",
    re.IGNORECASE,
)
REQUEST_TIMEOUT_RE = re.compile(
    r"Request\s+'(?P<target>[A-Za-z0-9_]+)'\s+timed out(?:\s+after\s+(?P<seconds>\d+)s)?",
    re.IGNORECASE,
)
GENERIC_TIMEOUT_RE = re.compile(
    r"\b(?:timed out|timeout(?:error)?)\b",
    re.IGNORECASE,
)
BRIDGE_FAILURE_PATTERNS: tuple[str, ...] = (
    "not connected to socket server",
    "socket server read failed",
    "socket server closed",
    "connection reset",
    "connection refused",
    "broken pipe",
    "read failed",
    "write failed",
)
HARD_FAILURE_PATTERNS: tuple[str, ...] = (
    "does not exist",
    "bridgeerror",
    "cannot read properties of undefined",
    "offhand",
    "not supported",
    "not available",
)


@dataclass(frozen=True)
class FailureCause:
    kind: str
    target: str | None = None
    required: str | None = None
    missing: dict[str, int] = field(default_factory=dict)
    message: str = ""


def parse_missing_counts(text: str) -> dict[str, int]:
    """Parse snippets like 'stick x2, cobblestone x3' into a count map."""
    counts: dict[str, int] = {}
    for item, count in re.findall(r"([A-Za-z0-9_]+)\s+x\s*(\d+)", text):
        counts[item.lower()] = counts.get(item.lower(), 0) + int(count)
    return counts


def is_hard_failure(result: Any) -> bool:
    """Return True for failures that usually need player/developer intervention."""
    text = str(result or "").lower()
    if any(pattern in text for pattern in BRIDGE_FAILURE_PATTERNS):
        return True
    if REQUEST_TIMEOUT_RE.search(text) or GENERIC_TIMEOUT_RE.search(text):
        return False
    return any(pattern in text for pattern in HARD_FAILURE_PATTERNS)


def classify_failure(result: Any) -> FailureCause | None:
    text = str(result or "").strip()
    if not text:
        return None

    tool_match = MISSING_TOOL_RE.search(text)
    if tool_match is not None:
        return FailureCause(
            kind="missing_tool",
            target=tool_match.group("target").lower(),
            required=tool_match.group("tool").lower(),
            message=text,
        )

    material_match = MISSING_MATERIALS_RE.search(text)
    if material_match is not None:
        missing = parse_missing_counts(material_match.group("missing"))
        return FailureCause(
            kind="missing_materials",
            target=material_match.group("target").lower(),
            missing=missing,
            message=text,
        )

    fuel_match = NO_FUEL_RE.search(text)
    if fuel_match is not None:
        return FailureCause(
            kind="missing_materials",
            target="smeltItem",
            missing={"coal": 1},
            message=text,
        )

    not_enough_match = NOT_ENOUGH_RE.search(text)
    if not_enough_match is not None:
        item = not_enough_match.group("item").lower()
        have = int(not_enough_match.group("have"))
        need = int(not_enough_match.group("need"))
        return FailureCause(
            kind="missing_materials",
            target=item,
            missing={item: max(1, need - have)},
            message=text,
        )

    nearby_match = NEARBY_MISS_RE.search(text)
    if nearby_match is not None:
        target = nearby_match.group("target") or nearby_match.group("breakable")
        return FailureCause(
            kind="nearby_miss",
            target=target.lower() if target else None,
            message=text,
        )

    lowered = text.lower()
    if any(pattern in lowered for pattern in BRIDGE_FAILURE_PATTERNS):
        return FailureCause(kind="bridge_failure", message=text)

    timeout_match = REQUEST_TIMEOUT_RE.search(text)
    if timeout_match is not None:
        return FailureCause(
            kind="path_timeout",
            target=timeout_match.group("target").lower(),
            message=text,
        )
    if GENERIC_TIMEOUT_RE.search(text) is not None:
        return FailureCause(kind="path_timeout", message=text)

    if is_hard_failure(text):
        return FailureCause(kind="hard_failure", message=text)

    if text.startswith("FAILED:"):
        return FailureCause(kind="unknown_failure", message=text)
    return None
