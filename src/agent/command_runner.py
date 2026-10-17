"""Command execution boundary for the agent loop."""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .action_result import ActionResult

CommandExecutor = Callable[
    [str, list[Any], dict[str, Any] | None],
    Awaitable[Any],
]


@dataclass(frozen=True)
class CommandRun:
    """A command execution plus normalized result metadata."""

    raw: Any
    result: ActionResult
    elapsed_ms: float


class CommandRunner:
    """Run commands and normalize legacy executor output at the boundary."""

    def __init__(self, executor: CommandExecutor):
        self._executor = executor

    async def run(
        self,
        command_name: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> CommandRun:
        normalized_args = list(args or [])
        normalized_kwargs = dict(kwargs or {})
        started = time.perf_counter()
        raw = await self._executor(command_name, normalized_args, normalized_kwargs)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return CommandRun(
            raw=raw,
            result=ActionResult.from_legacy(command_name, raw),
            elapsed_ms=elapsed_ms,
        )
