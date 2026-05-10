"""
Socket Bridge — Python client for the Mineflayer socket server.

Replaces JSPyBridge with a clean TCP JSON-line protocol.
The Mineflayer bot lives in a Node.js process (socket_server.js);
this module provides an async Python API to control it.

Usage::

    bridge = SocketBridge()
    await bridge.connect()
    await bridge.create_bot(host="127.0.0.1", port=55916, username="Bot")
    state = await bridge.observe()
    await bridge.chat("Hello from Python!")
    await bridge.move_to(100, 64, 200)
    await bridge.close()
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

EventHandler = Callable[..., Any]


class SocketBridgeError(Exception):
    """Raised when the socket server returns an error."""


class SocketBridge:
    """Async client for the Mineflayer socket server.

    Provides a request-response API for bot control and an event system
    for push notifications (chat, spawn, etc.).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9090):
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._read_task: asyncio.Task[None] | None = None
        self._running = False

    # ── Connection ──────────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to the Mineflayer socket server."""
        logger.info("Connecting to socket server at %s:%d", self._host, self._port)
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        self._running = True
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info("Connected to socket server")

    async def close(self) -> None:
        """Disconnect from the server and clean up."""
        if not self._running:
            return
        self._running = False

        # Cancel pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        # Cancel read task
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass

        # Close connection
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

        logger.info("Disconnected from socket server")

    async def __aenter__(self) -> SocketBridge:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ── Bot API ─────────────────────────────────────────────────────

    async def create_bot(
        self,
        host: str = "127.0.0.1",
        port: int = 25565,
        username: str = "SocketBot",
        **kwargs: Any,
    ) -> Any:
        """Create a Mineflayer bot and wait for spawn.

        Returns a dict with ``username``, ``host``, ``port`` on success.
        """
        return await self._request("create_bot", {
            "host": host, "port": port, "username": username, **kwargs,
        })

    async def observe(self) -> Any:
        """Gather current bot state.

        Returns a dict with keys: health, food, position (x/y/z),
        gamemode, time_of_day, biome, entities_nearby, blocks_nearby,
        players_nearby.
        """
        return await self._request("observe")

    async def chat(self, message: str) -> None:
        """Send a chat message from the bot."""
        await self._request("chat", {"message": message})

    async def move_to(self, x: float, y: float, z: float) -> Any:
        """Move the bot to the given coordinates."""
        return await self._request("move_to", {"x": x, "y": y, "z": z})

    async def collect_blocks(self, block_type: str, count: int = 1) -> Any:
        """Mine and collect blocks of the specified type."""
        return await self._request("collect_blocks", {
            "block_type": block_type, "count": count,
        })

    async def stop(self) -> Any:
        """Stop all current bot actions."""
        return await self._request("stop")

    async def get_inventory(self) -> Any:
        """Return the bot's inventory as a list of {name, count} dicts."""
        return await self._request("get_inventory")

    async def get_goal(self) -> Any:
        """Return the current pathfinding goal description."""
        return await self._request("get_goal")

    async def set_control_state(self, control: str, state: bool = True) -> None:
        """Set a control state on the bot, e.g. 'jump', 'sprint', 'sneak'."""
        await self._request("set_control_state", {"control": control, "state": state})

    # ── Events ──────────────────────────────────────────────────────

    def on(self, event_name: str) -> Callable[[EventHandler], EventHandler]:
        """Decorator: register a handler for a server-pushed event.

        Supported events: ``spawn``, ``chat``, ``end``, ``error``, ``kicked``.

        Example::

            @bridge.on("chat")
            async def on_chat(sender: str, message: str):
                print(f"{sender}: {message}")
        """

        def decorator(func: EventHandler) -> EventHandler:
            self._handlers[event_name].append(func)
            return func

        return decorator

    def off(self, event_name: str, func: EventHandler | None = None) -> None:
        """Remove an event handler (or all handlers for an event)."""
        if func is None:
            self._handlers.pop(event_name, None)
        elif event_name in self._handlers:
            self._handlers[event_name] = [
                h for h in self._handlers[event_name] if h is not func
            ]

    # ── Internal ────────────────────────────────────────────────────

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request and wait for the matching response."""
        if not self._running or not self._writer:
            raise SocketBridgeError("Not connected to socket server")

        req_id = uuid.uuid4().hex[:8]
        payload = json.dumps({
            "id": req_id,
            "method": method,
            "params": params or {},
        }) + "\n"

        fut: asyncio.Future[Any] = asyncio.Future()
        self._pending[req_id] = fut

        self._writer.write(payload.encode())
        await self._writer.drain()

        try:
            return await asyncio.wait_for(fut, timeout=120)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise SocketBridgeError(f"Request '{method}' timed out after 120s")

    async def _read_loop(self) -> None:
        """Read JSON-line messages from the server."""
        assert self._reader is not None

        while self._running:
            try:
                line = await self._reader.readline()
            except Exception:
                if self._running:
                    logger.exception("Read error in socket bridge")
                break

            if not line:
                logger.info("Socket server closed connection")
                break

            try:
                msg: dict[str, Any] = json.loads(line.decode())
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from server: %s", line[:100])
                continue

            if "id" in msg:
                # Response to a pending request
                req_id: str = msg["id"]
                fut = self._pending.pop(req_id, None)
                if fut is not None and not fut.done():
                    if msg.get("ok"):
                        fut.set_result(msg.get("result"))
                    else:
                        fut.set_exception(
                            SocketBridgeError(msg.get("error", "Unknown error"))
                        )
            elif "event" in msg:
                # Server-pushed event
                event_name: str = msg["event"]
                data: dict[str, Any] = msg.get("data", {})
                for handler in self._handlers.get(event_name, []):
                    try:
                        result = handler(**data)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.exception(
                            "Error in event handler for '%s'", event_name
                        )
