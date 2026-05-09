"""
Phase 0 — Mineflayer event registration.

Wraps Mineflayer's event system with a Pythonic decorator API
that supports both synchronous and asynchronous handlers.

Usage:
    bot = create_bot(...)
    events = EventRegistry(bot)

    @events.on("spawn")
    def on_spawn():
        bot.chat("I'm alive!")

    @events.on("chat")
    def on_chat(sender, message, *args):
        if message == "ping":
            bot.chat("pong")

    # Later, to clean up:
    events.remove_all()
"""
from __future__ import annotations

import asyncio
import functools
import logging
from collections import defaultdict
from typing import Any, Callable

from javascript import On
from javascript import off as js_off

logger = logging.getLogger(__name__)

HandlerFunc = Callable[..., Any]


class EventRegistry:
    """Manage Mineflayer event listeners with Pythonic syntax.

    Tracks all registered handlers so they can be removed or inspected.
    """

    def __init__(self, bot: Any):
        """Args:
            bot: A Mineflayer bot instance from mc.bridge.create_bot().
        """
        self._bot = bot
        # event_name -> list of (handler, wrapped_js_handler)
        self._handlers: dict[str, list[tuple[HandlerFunc, Callable[..., Any]]]] = (
            defaultdict(list)
        )

    def on(self, event_name: str) -> Callable[[HandlerFunc], HandlerFunc]:
        """Decorator: register a handler for a Mineflayer event.

        Args:
            event_name: Mineflayer event name, e.g. 'spawn', 'chat', 'tick'.

        Returns:
            A decorator that registers the wrapped function.

        Example:
            @events.on("entityHurt")
            def on_entity_hurt(entity):
                print(f"{entity.name} took damage")
        """

        def decorator(func: HandlerFunc) -> HandlerFunc:
            # Wrap to bridge async/sync handlers
            if asyncio.iscoroutinefunction(func):

                @functools.wraps(func)
                def sync_wrapper(*args: Any, **kwargs: Any) -> None:
                    # Fire-and-forget for coroutine handlers — use asyncio.run
                    # for simplicity; heavy handlers should use sync mode for
                    # events fired at high frequency.
                    try:
                        asyncio.run(func(*args, **kwargs))
                    except Exception:
                        logger.exception(
                            "Error in async handler for event '%s'", event_name
                        )

                wrapped = sync_wrapper
            else:
                wrapped = func

            # Register with JSPyBridge's @On decorator
            try:
                js_decorated = On(self._bot, event_name)(wrapped)
                self._handlers[event_name].append((func, js_decorated))
                logger.debug("Registered handler for event '%s': %s", event_name, func.__name__)
            except Exception:
                logger.exception("Failed to register handler for event '%s'", event_name)

            return func

        return decorator

    def register(self, event_name: str, func: HandlerFunc) -> HandlerFunc:
        """Register a handler function for an event (non-decorator form).

        Args:
            event_name: Mineflayer event name.
            func: The handler function.

        Returns:
            The original function (unwrapped).
        """
        return self.on(event_name)(func)

    def remove(self, event_name: str, func: HandlerFunc | None = None) -> None:
        """Remove one or all handlers for an event.

        Args:
            event_name: Event name to clean up.
            func: If provided, remove only this handler. Otherwise remove all for this event.
        """
        if event_name not in self._handlers:
            return

        if func is None:
            # Remove all handlers for this event
            for _, js_handler in self._handlers[event_name]:
                try:
                    js_off(self._bot, event_name, js_handler)
                except Exception:
                    pass
            self._handlers.pop(event_name, None)
            logger.debug("Removed all handlers for event '%s'", event_name)
        else:
            # Remove specific handler
            remaining = []
            for handler_fn, js_handler in self._handlers[event_name]:
                if handler_fn is func:
                    try:
                        js_off(self._bot, event_name, js_handler)
                    except Exception:
                        pass
                else:
                    remaining.append((handler_fn, js_handler))
            self._handlers[event_name] = remaining

    def remove_all(self) -> None:
        """Remove all registered event handlers."""
        event_names = list(self._handlers.keys())
        for name in event_names:
            self.remove(name)

    @property
    def registered_events(self) -> list[str]:
        """Return list of event names with active handlers."""
        return list(self._handlers.keys())
