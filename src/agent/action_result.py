"""Structured command results for the agent loop."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .chat_presenter import compact_command_result_for_chat, first_result_line
from .effects import ActionEffect, action_effects_from_result, action_succeeded
from .failures import FailureCause, classify_failure


@dataclass(frozen=True)
class ActionResult:
    """Normalized result of a command execution.

    Executors may still return legacy strings. The agent loop should convert
    them at the boundary so downstream code can use stable fields for success,
    chat presentation, logging, effects, and recovery.
    """

    ok: bool
    command: str
    summary: str
    detail: str = ""
    effects: tuple[ActionEffect, ...] = ()
    failure: FailureCause | None = None

    @classmethod
    def from_legacy(cls, command: str, result: Any) -> "ActionResult":
        detail = "" if result is None else str(result)
        ok = action_succeeded(result)
        default_prefix = "Done" if ok else "Failed"
        summary = (
            compact_command_result_for_chat(detail, default_prefix=default_prefix)
            if detail.strip() else f"{default_prefix}: no response"
        )
        return cls(
            ok=ok,
            command=command.lstrip("!"),
            summary=summary,
            detail=detail,
            effects=tuple(action_effects_from_result(command, detail)),
            failure=None if ok else classify_failure(detail),
        )

    @property
    def history_content(self) -> str:
        return self.detail

    @property
    def log_summary(self) -> str:
        return first_result_line(self.detail) if self.detail else self.summary
