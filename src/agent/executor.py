"""
Phase 2 — Action executor.

Maps parsed CommandCall objects to bot actions via the JSPyBridge bridge.
Mirrors the JS `executeCommand()` flow in `src/agent/commands/index.js`.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class ActionExecutor:
    """Executes parsed !command(args) calls against the bot bridge.

    Commands are registered via `@ActionExecutor.register("name")` decorator
    or the `register()` method.  Execution is async because every action
    ultimately calls into the Mineflayer bot via JSPyBridge.
    """

    def __init__(self, bridge: Any = None) -> None:
        self._bridge = bridge
        self._handlers: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, handler: Callable[..., Any]) -> None:
        """Register a command handler.  ``name`` is the bare command (no ``!`` prefix)."""
        self._handlers[name] = handler

    def register_many(self, handlers: dict[str, Callable[..., Any]]) -> None:
        self._handlers.update(handlers)

    async def execute(self, command_name: str, args: list[Any]) -> str | None:
        """Execute a parsed command and return a result string for the LLM history.

        Returns ``None`` when the command produces no feedback (empty history line).
        """
        # Strip leading ! if present
        name = command_name.lstrip("!")

        handler = self._handlers.get(name)
        if handler is None:
            logger.warning("Unknown command: !%s", name)
            return f"Command !{name} does not exist."

        try:
            result = handler(self._bridge, *args)
            # Support both sync and async handlers
            if hasattr(result, "__await__"):
                result = await result
            return str(result) if result is not None else None
        except Exception:
            logger.exception("Command !%s failed", name)
            return f"Command !{name} failed with an error."

    def has(self, name: str) -> bool:
        """Check if a command handler is registered."""
        return name.lstrip("!") in self._handlers

    # ── Built-in commands ───────────────────────────────────────────

    @staticmethod
    async def _cmd_stop(bridge: Any, *_args: Any) -> Any:
        return await bridge.stop()

    @staticmethod
    async def _cmd_stats(bridge: Any, *_args: Any) -> str:
        state = await bridge.observe()
        p = state["position"]
        return (
            f"Health: {state['health']:.0f}/20, Food: {state['food']:.0f}/20, "
            f"Position: ({p['x']:.0f}, {p['y']:.0f}, {p['z']:.0f}), "
            f"Gamemode: {state['gamemode']}"
        )

    @staticmethod
    async def _cmd_inventory(bridge: Any, *_args: Any) -> str:
        items = await bridge.get_inventory()
        if not items:
            return "Inventory empty."
        lines = ["Inventory:"]
        for item in items:
            lines.append(f"  {item['name']} x{item['count']}")
        return "\n".join(lines)

    @staticmethod
    async def _cmd_goal(bridge: Any, *_args: Any) -> Any:
        return await bridge.get_goal()

    def _register_builtins(self) -> None:
        """Register the minimal set of unblockable commands."""
        self.register_many({
            "stop": self._cmd_stop,
            "stats": self._cmd_stats,
            "inventory": self._cmd_inventory,
            "goal": self._cmd_goal,
        })
