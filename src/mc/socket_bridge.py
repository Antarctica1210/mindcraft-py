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
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

EventHandler = Callable[..., Any]
_POLLING_METHODS: frozenset[str] = frozenset({
    "observe",
    "should_place_torch",
    "get_goal",
})


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

    # ── Lifecycle ───────────────────────────────────────────────────
    async def create_bot(
        self, host: str = "127.0.0.1", port: int = 25565,
        username: str = "SocketBot", **kwargs: Any,
    ) -> Any:
        """Create a Mineflayer bot and wait for spawn."""
        return await self._request("create_bot", {
            "host": host, "port": port, "username": username, **kwargs,
        })

    async def close_server(self) -> Any:
        """Tell the socket server to shut down."""
        return await self._request("close")

    # ── Observation ─────────────────────────────────────────────────
    async def observe(self) -> Any:
        """Gather current bot state (health, food, position, etc.)."""
        return await self._request("observe")

    async def get_entities(self) -> Any:
        """Return all nearby entities with positions and types."""
        return await self._request("get_entities")

    async def get_nearby_blocks(self, distance: int = 16) -> Any:
        """Return list of nearby block type names."""
        return await self._request("get_nearby_blocks", {"distance": distance})

    async def get_craftable(self) -> Any:
        """Return list of craftable item names given current inventory."""
        return await self._request("get_craftable")

    # ── Communication ───────────────────────────────────────────────
    async def chat(self, message: str) -> None:
        """Send a chat message from the bot."""
        await self._request("chat", {"message": message})

    # ── Movement ────────────────────────────────────────────────────
    async def move_to(self, x: float, y: float, z: float) -> Any:
        """Move the bot to the given coordinates."""
        return await self._request("move_to", {"x": x, "y": y, "z": z})

    async def go_to_player(self, player_name: str, closeness: float = 3) -> Any:
        """Navigate to a nearby player."""
        return await self._request("go_to_player", {
            "player_name": player_name, "closeness": closeness,
        })

    async def follow_player(self, player_name: str, follow_dist: float = 4) -> Any:
        """Continuously follow a player."""
        return await self._request("follow_player", {
            "player_name": player_name, "follow_dist": follow_dist,
        })

    async def move_away(self, distance: float = 16) -> Any:
        """Move in a random direction by the given distance."""
        return await self._request("move_away", {"distance": distance})

    async def search_for_block(self, block_type: str, search_range: float = 64) -> Any:
        """Find and navigate to the nearest block of the given type."""
        return await self._request("search_for_block", {
            "block_type": block_type, "search_range": search_range,
        })

    async def search_for_entity(self, entity_type: str, search_range: float = 64) -> Any:
        """Find and navigate to the nearest entity of the given type."""
        return await self._request("search_for_entity", {
            "entity_type": entity_type, "search_range": search_range,
        })

    async def go_to_bed(self) -> Any:
        """Find the nearest bed and sleep in it."""
        return await self._request("go_to_bed")

    async def go_to_surface(self) -> Any:
        """Navigate to the surface from underground."""
        return await self._request("go_to_surface")

    async def dig_down(self, distance: int = 10) -> Any:
        """Dig straight down, stopping at lava/water/bedrock."""
        return await self._request("dig_down", {"distance": distance})

    async def stay(self, seconds: int = -1) -> Any:
        """Stay in place. -1 means indefinitely."""
        return await self._request("stay", {"seconds": seconds})

    async def set_control_state(self, control: str, state: bool = True) -> None:
        """Set a control state: 'jump', 'sprint', 'sneak', etc."""
        await self._request("set_control_state", {"control": control, "state": state})

    # ── Combat ──────────────────────────────────────────────────────
    async def attack(self, entity_type: str, range_val: float = 64) -> Any:
        """Attack the nearest hostile entity of the given type."""
        return await self._request("attack", {
            "entity_type": entity_type, "range": range_val,
        })

    async def attack_player(self, player_name: str) -> Any:
        """Attack a specific player."""
        return await self._request("attack_player", {"player_name": player_name})

    # ── Inventory ───────────────────────────────────────────────────
    async def get_inventory(self) -> Any:
        """Return inventory as a list of {name, count} dicts."""
        return await self._request("get_inventory")

    async def get_goal(self) -> Any:
        """Return the current pathfinding goal."""
        return await self._request("get_goal")

    async def equip(self, item_name: str, destination: str = "hand") -> Any:
        """Equip an item to the given body slot."""
        return await self._request("equip", {
            "item_name": item_name, "destination": destination,
        })

    async def consume(self, item_name: str) -> Any:
        """Eat/drink the named item."""
        return await self._request("consume", {"item_name": item_name})

    async def discard(self, item_name: str, num: int = -1) -> Any:
        """Discard items from inventory. -1 = all."""
        return await self._request("discard", {"item_name": item_name, "num": num})

    # ── Block interaction ───────────────────────────────────────────
    async def place_block(self, block_type: str) -> Any:
        """Place a block from inventory at the current position."""
        return await self._request("place_block", {"block_type": block_type})

    async def activate_block(self, block_type: str) -> Any:
        """Right-click the nearest block of the given type."""
        return await self._request("activate_block", {"block_type": block_type})

    async def use_on(self, tool_name: str, target: str) -> Any:
        """Use a tool on a target entity or block."""
        return await self._request("use_on", {
            "tool_name": tool_name, "target": target,
        })

    async def collect_blocks(self, block_type: str, count: int = 1) -> Any:
        """Mine and collect blocks."""
        return await self._request("collect_blocks", {
            "block_type": block_type, "count": count,
        })

    # ── Chest operations ────────────────────────────────────────────
    async def view_chest(self) -> Any:
        """Show contents of the nearest chest."""
        return await self._request("view_chest")

    async def put_in_chest(self, item_name: str, num: int = -1) -> Any:
        """Store items in the nearest chest."""
        return await self._request("put_in_chest", {"item_name": item_name, "num": num})

    async def take_from_chest(self, item_name: str, num: int = -1) -> Any:
        """Take items from the nearest chest."""
        return await self._request("take_from_chest", {"item_name": item_name, "num": num})

    # ── Crafting / Smelting ─────────────────────────────────────────
    async def craft_recipe(self, recipe_name: str, num: int = 1) -> Any:
        """Craft a recipe the given number of times."""
        return await self._request("craft_recipe", {"recipe_name": recipe_name, "num": num})

    async def smelt_item(self, item_name: str, num: int = 1) -> Any:
        """Smelt items in a furnace."""
        return await self._request("smelt_item", {"item_name": item_name, "num": num})

    async def clear_furnace(self) -> Any:
        """Clear all items from the nearest furnace."""
        return await self._request("clear_furnace")

    # ── Villager trading ────────────────────────────────────────────
    async def show_villager_trades(self, villager_id: int) -> Any:
        """List trades for a villager by entity ID."""
        return await self._request("show_villager_trades", {"villager_id": villager_id})

    async def trade_with_villager(
        self, villager_id: int, trade_index: int = 1, count: int = 1,
    ) -> Any:
        """Execute a trade with a villager."""
        return await self._request("trade_with_villager", {
            "villager_id": villager_id, "trade_index": trade_index, "count": count,
        })

    # ── Look ────────────────────────────────────────────────────────
    async def look_at_player(self, player_name: str) -> Any:
        """Look at a specific player."""
        return await self._request("look_at_player", {"player_name": player_name})

    async def look_at_position(self, x: float, y: float, z: float) -> Any:
        """Look at specific coordinates."""
        return await self._request("look_at_position", {"x": x, "y": y, "z": z})

    # ── Give ────────────────────────────────────────────────────────
    async def give_to_player(self, player_name: str, item_name: str, num: int = 1) -> Any:
        """Navigate to a player and give them items."""
        return await self._request("give_to_player", {
            "player_name": player_name, "item_name": item_name, "num": num,
        })

    # ── Skills ──────────────────────────────────────────────────────
    async def pickup_nearby_items(self, distance: float = 8) -> Any:
        """Pick up all nearby dropped items on the ground."""
        return await self._request("pickup_nearby_items", {"distance": distance})

    async def should_place_torch(self) -> Any:
        """Check whether a torch should be placed (dark area, torch in inventory)."""
        return await self._request("should_place_torch")

    async def place_torch(self) -> Any:
        """Place a torch at the bot's current position."""
        return await self._request("place_torch")

    async def till_and_sow(self, x: float, y: float, z: float, seed_type: str | None = None) -> Any:
        """Till the ground at (x,y,z) and optionally plant seeds."""
        return await self._request("till_and_sow", {
            "x": x, "y": y, "z": z,
            "seed_type": seed_type,
        })

    async def use_door(
        self, x: float | None = None, y: float | None = None, z: float | None = None,
    ) -> Any:
        """Open a door and walk through it. If no coordinates given, uses nearest door."""
        params: dict[str, Any] = {}
        if x is not None and y is not None and z is not None:
            params = {"x": x, "y": y, "z": z}
        return await self._request("use_door", params)

    async def get_crafting_plan(self, target_item: str, quantity: int = 1) -> Any:
        """Get a detailed crafting plan for the target item given current inventory."""
        return await self._request("get_crafting_plan", {
            "target_item": target_item, "quantity": quantity,
        })

    # ── Dynamic code / action output ────────────────────────────────
    async def execute_code(self, code: str, timeout_ms: int = 60000) -> Any:
        """Execute a generated JavaScript action in the Mineflayer process."""
        return await self._request("execute_code", {
            "code": code,
            "timeout_ms": timeout_ms,
        })

    async def wait_for_action(self, timeout_ms: int = 60000) -> Any:
        """Wait for the current pathfinder/collector action to finish."""
        return await self._request("wait_for_action", {"timeout_ms": timeout_ms})

    async def get_action_output(self) -> Any:
        """Return and clear detailed action log output from the socket server."""
        return await self._request("get_action_output")

    # ── Stop ────────────────────────────────────────────────────────
    async def stop(self) -> Any:
        """Stop all current actions (movement, mining, combat, following)."""
        return await self._request("stop")

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

        started = time.perf_counter()
        log = logger.debug if method in _POLLING_METHODS else logger.info
        log(
            "SocketBridge request start: id=%s method=%s params=%s",
            req_id,
            method,
            _preview(params or {}),
        )
        self._writer.write(payload.encode())
        await self._writer.drain()

        try:
            result = await asyncio.wait_for(fut, timeout=120)
            elapsed_ms = (time.perf_counter() - started) * 1000
            log(
                "SocketBridge request done: id=%s method=%s elapsed_ms=%.1f result=%s",
                req_id,
                method,
                elapsed_ms,
                _preview(result),
            )
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "SocketBridge request timeout: id=%s method=%s elapsed_ms=%.1f",
                req_id,
                method,
                elapsed_ms,
            )
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
                # Server-pushed event — dispatch via create_task so the
                # read loop is never blocked.  This matters when an event
                # handler itself calls _request() (e.g. chat → observe).
                event_name: str = msg["event"]
                data: dict[str, Any] = msg.get("data", {})
                for handler in self._handlers.get(event_name, []):
                    result = handler(**data)
                    if asyncio.iscoroutine(result):

                        def _done_callback(
                            t: asyncio.Task[Any], en: str = event_name,
                        ) -> None:
                            exc = t.exception()
                            if exc is not None:
                                logger.exception(
                                    "Error in event handler for '%s'", en,
                                )

                        task = asyncio.create_task(result)
                        task.add_done_callback(_done_callback)


def _preview(value: Any, *, limit: int = 500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."
