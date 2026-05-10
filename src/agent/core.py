"""
Phase 2 — Agent core loop.

The central ``Agent`` class wires together:
  - Prompter (LLM provider, Phase 1)
  - MessageBuilder (prompt assembly, Phase 1)
  - action_parser (response parsing, Phase 1)
  - ActionExecutor (command dispatch, Phase 2)
  - ConversationManager (history + turn mgmt, Phase 2)
  - ModeEvaluator (reactive behavior, Phase 2)

Mirrors the ``Agent`` class in ``src/agent/agent.js``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from llm import Prompter
from llm.message_builder import CommandDef, ConversationTurn, MessageBuilder
from llm.tool_parser import parse_response

from .conversation import ConversationManager
from .executor import ActionExecutor
from .models import AgentConfig, BotState, Position
from .modes import BehaviorMode, ModeEvaluator

logger = logging.getLogger(__name__)

# ── Agent ───────────────────────────────────────────────────────────


class Agent:
    """Minecraft bot agent with LLM-driven behavior.

    Usage::

        config = AgentConfig(
            name="andy",
            profile={"model": "lmstudio/qwen/qwen3.6-35b-a3b", "url": "http://...", ...},
        )
        agent = Agent(config)
        await agent.start(bridge)
    """

    def __init__(self, config: AgentConfig):
        self._config = config
        self.name = config.name

        # Phase 1 components (initialised in start())
        self._prompter: Prompter | None = None
        self._msg_builder: MessageBuilder | None = None

        # Phase 2 components
        self._executor = ActionExecutor()
        self._convo_mgr = ConversationManager()
        self._modes = ModeEvaluator()

        # Runtime state
        self._bridge = None
        self._history: list[ConversationTurn] = []
        self._last_sender: str | None = None
        self._shut_up: bool = False
        self._running: bool = False

        # Metrics
        self._tick_count: int = 0
        self._start_time: float = 0.0

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self, bridge: Any) -> None:
        """Initialise all subsystems and begin the agent loop."""
        self._bridge = bridge
        self._running = True
        self._start_time = time.time()

        # Phase 1: LLM provider (LiteLLM-backed)
        from settings import settings as global_settings
        self._prompter = Prompter(
            global_settings.llm,
            embedding=global_settings.embedding,
        )

        # Register tool definitions if the provider supports tool calling
        tools = _build_tool_defs(self._executor._handlers)
        if tools:
            self._prompter.register_tools(tools)

        # Phase 1: message builder
        self._msg_builder = MessageBuilder(
            bot_name=self.name,
            commands=list(_command_defs().values()),
            self_prompt="",
        )

        # Phase 2: register built-in command handlers
        self._executor._register_builtins()

        logger.info("Agent %s started (model=%s)", self.name, self._prompter.model)

    async def stop(self) -> None:
        """Gracefully shut down the agent."""
        self._running = False
        if self._bridge:
            try:
                await self._bridge.stop()
            except Exception:
                pass
        logger.info("Agent %s stopped", self.name)

    # ── Observation ─────────────────────────────────────────────────

    async def observe(self) -> BotState:
        """Gather the current bot state from the bridge."""
        bridge = self._bridge
        if bridge is None:
            return BotState()

        try:
            s = await bridge.observe()
        except Exception:
            logger.debug("observe() failed", exc_info=True)
            return BotState()

        p = s.get("position", {})
        return BotState(
            health=s.get("health", 20),
            food=s.get("food", 20),
            position=Position(x=p.get("x", 0), y=p.get("y", 0), z=p.get("z", 0)),
            gamemode=s.get("gamemode", "survival"),
            time_of_day=s.get("time_of_day", ""),
            biome=s.get("biome", ""),
            entities_nearby=s.get("entities_nearby", ""),
            blocks_nearby=s.get("blocks_nearby", ""),
        )

    # ── Planning ────────────────────────────────────────────────────

    async def plan(self, bot_state: BotState, _source: str = "") -> str:
        """Build the prompt and query the LLM for the next action."""
        if self._prompter is None or self._msg_builder is None:
            return ""

        stats = self._msg_builder.format_stats(
            health=bot_state.health,
            food=bot_state.food,
            position=(bot_state.position.x, bot_state.position.y, bot_state.position.z),
            gamemode=bot_state.gamemode,
            time_of_day=bot_state.time_of_day,
            biome=bot_state.biome,
            entities_nearby=bot_state.entities_nearby,
            blocks_nearby=bot_state.blocks_nearby,
        )

        inventory = await self._format_inventory()

        command_docs = self._msg_builder.build_command_docs(
            blocked_actions=set(self._config.blocked_actions)
        )

        memory = ""  # TODO: wire up MemoryLayer (Phase 3)
        examples = ""  # TODO: wire up example retrieval (Phase 3)

        system_prompt = self._msg_builder.build_system_prompt(
            stats=stats,
            inventory=inventory,
            command_docs=command_docs,
            examples=examples,
            memory=memory,
        )

        messages = self._msg_builder.build_messages(
            history=self._history,
            system_prompt=system_prompt,
        )

        response = await self._prompter.send_request(
            turns=messages[1:],  # exclude system message (already in turns)
            system_message=system_prompt,
        )

        return response.content

    # ── Execution ───────────────────────────────────────────────────

    async def execute_command(self, command_name: str, args: list[Any]) -> str | None:
        """Execute a parsed command and return feedback for the LLM history."""
        return await self._executor.execute(command_name, args)

    # ── Message handling (mirrors agent.js handleMessage) ────────────

    async def handle_message(
        self, source: str, message: str, max_responses: int | None = None
    ) -> bool:
        """Handle an incoming message: plan → parse → execute → repeat.

        Returns True if at least one command was executed.
        """
        if not self._running or not self._prompter:
            return False

        if max_responses is None:
            max_responses = self._config.max_commands

        used_command = False

        # Observe current state
        bot_state = await self.observe()

        # Add the incoming message to history
        self._history.append(ConversationTurn(role="user", content=message))
        self._last_sender = source

        for _ in range(max_responses):
            if self._shut_up or not self._running:
                break

            # Plan
            llm_output = await self.plan(bot_state, source)
            if not llm_output.strip():
                logger.debug("Empty LLM response — ending turn")
                break

            # Parse
            parsed = parse_response(llm_output)
            self._history.append(
                ConversationTurn(role="assistant", content=llm_output)
            )

            if not parsed.has_command:
                # Plain conversation response — no command to execute
                break

            # Execute each command
            for cmd in parsed.commands:
                if self._shut_up:
                    break

                result = await self.execute_command(cmd.name, cmd.args)
                used_command = True

                if result:
                    self._history.append(
                        ConversationTurn(role="system", content=result)
                    )
                else:
                    break  # no feedback = stop chaining

        # Compress history if needed
        self._history = self._convo_mgr.compress_history(self._history)

        return used_command

    # ── Main loop ───────────────────────────────────────────────────

    async def run_loop(self, tick_interval: float = 0.05) -> None:
        """Run the agent main loop: modes → observe → (idle check).

        This is the top-level loop that runs every tick (20 TPS = 0.05 s).
        It evaluates reactive modes and yields control back to the bridge.
        """
        while self._running:
            self._tick_count += 1

            try:
                bot_state = await self.observe()
                if not bot_state.is_alive:
                    await asyncio.sleep(1.0)
                    continue

                # Evaluate reactive behavior modes
                triggered = await self._modes.tick(bot_state, self._bridge)
                if triggered:
                    logger.debug("Tick %d: mode %s triggered", self._tick_count, triggered)

                # Update stuck tracking
                if self._bridge is not None and self._modes.is_active(BehaviorMode.UNSTUCK):
                    try:
                        goal = await self._bridge.get_goal()
                        if goal and "No current" in goal:
                            self._modes.release(BehaviorMode.UNSTUCK)
                    except Exception:
                        pass

            except Exception:
                logger.exception("Error in agent loop tick %d", self._tick_count)

            await asyncio.sleep(tick_interval)

    # ── Internal helpers ────────────────────────────────────────────

    async def _format_inventory(self) -> str:
        """Format the bot's inventory for the $INVENTORY placeholder."""
        if self._bridge is None:
            return "INVENTORY: empty"
        try:
            items = await self._bridge.get_inventory()
            if not items:
                return "INVENTORY: empty"
            lines = ["INVENTORY:"]
            for item in items[:20]:
                lines.append(f"  {item['name']} x{item['count']}")
            return "\n".join(lines)
        except Exception:
            return "INVENTORY: unavailable"

    def shut_up(self) -> None:
        """Mute the agent (stop all non-critical output)."""
        self._shut_up = True
        self._convo_mgr.end_all()

    def unshut(self) -> None:
        self._shut_up = False


# ── Internal helpers ─────────────────────────────────────────────────


def _command_defs() -> dict[str, CommandDef]:
    """Return the full set of bot command definitions for the LLM prompt.

    Mirrors the JS command list in ``src/agent/commands/``.
    """
    return {
        # ── Control ─────────────────────────────────────────────────
        "stop": CommandDef(
            name="stop",
            description="Stop all current actions (movement, mining, combat, following).",
        ),
        "stfu": CommandDef(
            name="stfu",
            description="Stop chatting and self-narration, but continue current actions.",
        ),
        "restart": CommandDef(
            name="restart",
            description="Restart the agent process (clean shutdown).",
        ),
        "clearChat": CommandDef(
            name="clearChat",
            description="Clear the conversation history.",
        ),
        # ── Observation ─────────────────────────────────────────────
        "stats": CommandDef(
            name="stats",
            description="Report current health, food, position, gamemode, biome, time, "
            "weather, and nearby players.",
        ),
        "inventory": CommandDef(
            name="inventory",
            description="List all items in the bot's inventory with counts.",
        ),
        "goal": CommandDef(
            name="goal",
            description="Report the current pathfinding goal, if any.",
        ),
        "entities": CommandDef(
            name="entities",
            description="List nearby players and entities with positions and types.",
        ),
        "nearbyBlocks": CommandDef(
            name="nearbyBlocks",
            description="List block types near the bot.",
        ),
        "craftable": CommandDef(
            name="craftable",
            description="List all items craftable with current inventory.",
        ),
        "help": CommandDef(
            name="help",
            description="List all available commands.",
        ),
        "modes": CommandDef(
            name="modes",
            description="Show all available behavior modes and their status.",
        ),
        # ── Movement ────────────────────────────────────────────────
        "moveTo": CommandDef(
            name="moveTo",
            description="Move the bot to the given coordinates.",
            params={
                "x": {"type": "float", "description": "X coordinate"},
                "y": {"type": "float", "description": "Y coordinate"},
                "z": {"type": "float", "description": "Z coordinate"},
            },
        ),
        "goToPlayer": CommandDef(
            name="goToPlayer",
            description="Navigate to the given player.",
            params={
                "player_name": {"type": "string", "description": "Player name to go to"},
                "closeness": {"type": "float", "description": "How close to get (default 3)"},
            },
        ),
        "followPlayer": CommandDef(
            name="followPlayer",
            description="Continuously follow a player.",
            params={
                "player_name": {"type": "string", "description": "Player name to follow"},
                "follow_dist": {"type": "float", "description": "Following distance (default 4)"},
            },
        ),
        "goToCoordinates": CommandDef(
            name="goToCoordinates",
            description="Move to specific x, y, z coordinates.",
            params={
                "x": {"type": "float", "description": "X coordinate"},
                "y": {"type": "float", "description": "Y coordinate"},
                "z": {"type": "float", "description": "Z coordinate"},
                "closeness": {"type": "float", "description": "How close to get (default 2)"},
            },
        ),
        "searchForBlock": CommandDef(
            name="searchForBlock",
            description="Find and navigate to the nearest block of the given type.",
            params={
                "block_type": {"type": "string", "description": "Block type, e.g. oak_log"},
                "search_range": {"type": "float", "description": "Search radius (default 64)"},
            },
        ),
        "searchForEntity": CommandDef(
            name="searchForEntity",
            description="Find and navigate to the nearest entity of the given type.",
            params={
                "entity_type": {"type": "string", "description": "Entity type, e.g. cow"},
                "search_range": {"type": "float", "description": "Search radius (default 64)"},
            },
        ),
        "moveAway": CommandDef(
            name="moveAway",
            description="Move in a random direction by the given distance.",
            params={
                "distance": {"type": "float", "description": "Distance to move (default 16)"},
            },
        ),
        "goToBed": CommandDef(
            name="goToBed",
            description="Find the nearest bed and sleep in it.",
        ),
        "goToSurface": CommandDef(
            name="goToSurface",
            description="Navigate upward to the surface.",
        ),
        "digDown": CommandDef(
            name="digDown",
            description="Dig straight down, stopping at lava/water/bedrock.",
            params={
                "distance": {"type": "int", "description": "Max distance to dig (default 10)"},
            },
        ),
        "stay": CommandDef(
            name="stay",
            description="Stay in place. Pauses all modes.",
            params={
                "seconds": {"type": "int", "description": "Seconds to stay (-1 = indefinite)"},
            },
        ),
        # ── Combat ──────────────────────────────────────────────────
        "attack": CommandDef(
            name="attack",
            description="Attack and kill the nearest hostile entity of the given type.",
            params={
                "entity_type": {"type": "string", "description": "Entity type, e.g. zombie"},
            },
        ),
        "attackPlayer": CommandDef(
            name="attackPlayer",
            description="Attack a specific player until they die or flee.",
            params={
                "player_name": {"type": "string", "description": "Player name to attack"},
            },
        ),
        # ── Inventory ───────────────────────────────────────────────
        "equip": CommandDef(
            name="equip",
            description="Equip an item to the given body slot.",
            params={
                "item_name": {"type": "string", "description": "Item to equip, e.g. diamond_sword"},
            },
        ),
        "consume": CommandDef(
            name="consume",
            description="Eat or drink the given item.",
            params={
                "item_name": {"type": "string", "description": "Item to consume, e.g. cooked_beef"},
            },
        ),
        "discard": CommandDef(
            name="discard",
            description="Discard items from inventory.",
            params={
                "item_name": {"type": "string", "description": "Item to discard"},
                "num": {"type": "int", "description": "Number to discard (-1 = all)"},
            },
        ),
        "givePlayer": CommandDef(
            name="givePlayer",
            description="Navigate to a player and give them items.",
            params={
                "player_name": {"type": "string", "description": "Player to give to"},
                "item_name": {"type": "string", "description": "Item to give"},
                "num": {"type": "int", "description": "Number to give (default 1)"},
            },
        ),
        # ── Block Interaction ───────────────────────────────────────
        "placeHere": CommandDef(
            name="placeHere",
            description="Place a block from inventory at the current position.",
            params={
                "block_type": {"type": "string", "description": "Block type to place"},
            },
        ),
        "activateBlock": CommandDef(
            name="activateBlock",
            description="Right-click / activate the nearest block of the given type.",
            params={
                "block_type": {"type": "string", "description": "Block type, e.g. lever"},
            },
        ),
        "collectBlocks": CommandDef(
            name="collectBlocks",
            description="Mine and collect blocks of the specified type.",
            params={
                "blockType": {"type": "string", "description": "Block type, e.g. oak_log"},
                "count": {"type": "int", "description": "Number to collect"},
            },
        ),
        "useOn": CommandDef(
            name="useOn",
            description="Use a tool on a target entity or block.",
            params={
                "tool_name": {"type": "string", "description": "Tool to use, e.g. bucket"},
                "target": {
                    "type": "string",
                    "description": "Target entity/block or 'nothing'/'hand'",
                },
            },
        ),
        # ── Chest ───────────────────────────────────────────────────
        "viewChest": CommandDef(
            name="viewChest",
            description="Show contents of the nearest chest.",
        ),
        "putInChest": CommandDef(
            name="putInChest",
            description="Store items in the nearest chest.",
            params={
                "item_name": {"type": "string", "description": "Item to store"},
                "num": {"type": "int", "description": "Number to store (-1 = all)"},
            },
        ),
        "takeFromChest": CommandDef(
            name="takeFromChest",
            description="Take items from the nearest chest.",
            params={
                "item_name": {"type": "string", "description": "Item to take"},
                "num": {"type": "int", "description": "Number to take (-1 = all)"},
            },
        ),
        # ── Crafting / Smelting ─────────────────────────────────────
        "craftRecipe": CommandDef(
            name="craftRecipe",
            description="Craft a recipe using available materials.",
            params={
                "recipe_name": {"type": "string", "description": "Item to craft, e.g. stick"},
                "num": {"type": "int", "description": "Number to craft (default 1)"},
            },
        ),
        "smeltItem": CommandDef(
            name="smeltItem",
            description="Smelt items in a furnace.",
            params={
                "item_name": {"type": "string", "description": "Item to smelt, e.g. raw_iron"},
                "num": {"type": "int", "description": "Number to smelt (default 1)"},
            },
        ),
        "clearFurnace": CommandDef(
            name="clearFurnace",
            description="Clear all items from the nearest furnace.",
        ),
        # ── Villager ────────────────────────────────────────────────
        "showVillagerTrades": CommandDef(
            name="showVillagerTrades",
            description="Show trades for a villager by entity ID.",
            params={
                "villager_id": {"type": "int", "description": "Villager entity ID"},
            },
        ),
        "tradeWithVillager": CommandDef(
            name="tradeWithVillager",
            description="Execute a trade with a villager.",
            params={
                "villager_id": {"type": "int", "description": "Villager entity ID"},
                "trade_index": {"type": "int", "description": "Trade number (1-based)"},
                "count": {"type": "int", "description": "Number of trades to execute"},
            },
        ),
        # ── Look ────────────────────────────────────────────────────
        "lookAtPlayer": CommandDef(
            name="lookAtPlayer",
            description="Look at a player or in the same direction as a player.",
            params={
                "player_name": {"type": "string", "description": "Player to look at"},
            },
        ),
        "lookAtPosition": CommandDef(
            name="lookAtPosition",
            description="Look at specific coordinates.",
            params={
                "x": {"type": "float", "description": "X coordinate"},
                "y": {"type": "float", "description": "Y coordinate"},
                "z": {"type": "float", "description": "Z coordinate"},
            },
        ),
        # ── Mode / Goal ─────────────────────────────────────────────
        "setMode": CommandDef(
            name="setMode",
            description="Enable or disable a behavior mode.",
            params={
                "mode_name": {"type": "string", "description": "Mode name"},
                "on": {"type": "bool", "description": "True to enable, False to disable"},
            },
        ),
        "setGoal": CommandDef(
            name="setGoal",
            description="Set a goal message for persistent self-prompting.",
            params={
                "self_prompt": {"type": "string", "description": "Goal message for self-prompting"},
            },
        ),
        "endGoal": CommandDef(
            name="endGoal",
            description="End the current goal and stop self-prompting.",
        ),
        # ── Skills ──────────────────────────────────────────────────
        "pickupNearbyItems": CommandDef(
            name="pickupNearbyItems",
            description="Pick up all nearby dropped items on the ground.",
            params={
                "distance": {"type": "float", "description": "Pickup radius (default 8)"},
            },
        ),
        "tillAndSow": CommandDef(
            name="tillAndSow",
            description="Till the ground at coordinates and optionally plant seeds.",
            params={
                "x": {"type": "float", "description": "X coordinate to till"},
                "y": {"type": "float", "description": "Y coordinate to till"},
                "z": {"type": "float", "description": "Z coordinate to till"},
                "seed_type": {
                    "type": "string",
                    "description": "Seed type to plant, e.g. wheat_seeds (optional)",
                },
            },
        ),
        "useDoor": CommandDef(
            name="useDoor",
            description="Open a door, walk through it, and close it.",
            params={
                "x": {
                    "type": "float",
                    "description": "X coordinate of door (optional, finds nearest)",
                },
                "y": {"type": "float", "description": "Y coordinate of door (optional)"},
                "z": {"type": "float", "description": "Z coordinate of door (optional)"},
            },
        ),
        "getCraftingPlan": CommandDef(
            name="getCraftingPlan",
            description="Get a detailed crafting plan showing required ingredients and steps.",
            params={
                "target_item": {
                    "type": "string",
                    "description": "The item to craft, e.g. diamond_sword",
                },
                "quantity": {
                    "type": "int",
                    "description": "Quantity to craft (default 1)",
                },
            },
        ),
        "searchWiki": CommandDef(
            name="searchWiki",
            description="Search the Minecraft Wiki for info about an item, block, or mechanic.",
            params={
                "query": {
                    "type": "string",
                    "description": "The search query, e.g. 'enchanting table'",
                },
            },
        ),
        # ── Conversation ────────────────────────────────────────────
        "startConversation": CommandDef(
            name="startConversation",
            description="Start a conversation with another player.",
            params={
                "player_name": {"type": "string", "description": "Player to talk to"},
                "message": {"type": "string", "description": "Message to send"},
            },
        ),
        "endConversation": CommandDef(
            name="endConversation",
            description="End the current conversation.",
            params={
                "player_name": {"type": "string", "description": "Player to stop talking to"},
            },
        ),
    }


def _build_tool_defs(handlers: dict[str, Callable[..., Any]]) -> list[dict[str, Any]] | None:
    """Convert registered command handlers into OpenAI tool-calling format.

    Returns None if no handlers are registered (tool calling not used).
    """
    if not handlers:
        return None

    cmd_defs = _command_defs()
    tools: list[dict[str, Any]] = []
    for name in handlers:
        if name not in cmd_defs:
            continue
        cmd = cmd_defs[name]
        properties: dict[str, dict[str, str]] = {}
        required: list[str] = []
        for pname, pinfo in cmd.params.items():
            properties[pname] = {
                "type": pinfo.get("type", "string"),
                "description": pinfo.get("description", ""),
            }
            required.append(pname)

        tools.append({
            "type": "function",
            "function": {
                "name": f"action:{name}",
                "description": cmd.description,
                **({"parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }} if properties else {}),
            },
        })

    return tools or None
