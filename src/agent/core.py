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
            bot = self._bridge.get_bot()
            if bot:
                bot.clearControlStates()
        logger.info("Agent %s stopped", self.name)

    # ── Observation ─────────────────────────────────────────────────

    async def observe(self) -> BotState:
        """Gather the current bot state from the Mineflayer bridge."""
        bridge = self._bridge
        if bridge is None:
            return BotState()

        bot = bridge.get_bot()
        if bot is None:
            return BotState()

        pos = bot.entity.position
        position = Position(x=pos.x, y=pos.y, z=pos.z)

        # Gather nearby entities
        entities_str = ""
        try:
            entities = bot.entities or {}
            parts = []
            for _eid, entity in list(entities.items())[:10]:
                if entity.name and entity.name != "player":
                    dist = 0.0
                    if hasattr(entity, "position") and entity.position:
                        dist = position.distance_to(
                            Position(entity.position.x, entity.position.y, entity.position.z)
                        )
                    parts.append(f"  {entity.name} ({dist:.0f}m)")
            entities_str = "\n".join(parts)
        except Exception:
            logger.debug("Failed to enumerate entities", exc_info=True)

        # Gather nearby blocks (if blockAt is available)
        blocks_str = ""
        try:
            block = bot.blockAt(bot.entity.position)
            if block:
                blocks_str = f"standing on: {block.name}"
        except Exception:
            logger.debug("Failed to read current block", exc_info=True)

        time_of_day = ""
        try:
            tod = bot.time.timeOfDay
            hour = tod / 1000
            if hour < 6:
                time_of_day = "night"
            elif hour < 7:
                time_of_day = "sunrise"
            elif hour < 12:
                time_of_day = "day"
            elif hour < 13:
                time_of_day = "noon"
            elif hour < 18:
                time_of_day = "afternoon"
            elif hour < 19:
                time_of_day = "sunset"
            else:
                time_of_day = "night"
        except Exception:
            pass

        return BotState(
            health=bot.health or 20,
            food=bot.food or 20,
            position=position,
            gamemode=getattr(bot.game, "gameMode", "survival"),
            time_of_day=time_of_day,
            biome="",
            entities_nearby=entities_str,
            blocks_nearby=blocks_str,
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

        inventory = self._format_inventory()

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
                    bot = self._bridge.get_bot()
                    if bot and bot.pathfinder.goal is None:
                        self._modes.release(BehaviorMode.UNSTUCK)

            except Exception:
                logger.exception("Error in agent loop tick %d", self._tick_count)

            await asyncio.sleep(tick_interval)

    # ── Internal helpers ────────────────────────────────────────────

    def _format_inventory(self) -> str:
        """Format the bot's inventory for the $INVENTORY placeholder."""
        if self._bridge is None:
            return "INVENTORY: empty"
        bot = self._bridge.get_bot()
        if bot is None:
            return "INVENTORY: empty"
        try:
            items = bot.inventory.items()
            if not items:
                return "INVENTORY: empty"
            lines = ["INVENTORY:"]
            for item in items[:20]:
                name = getattr(item, "name", "unknown")
                count = getattr(item, "count", 0)
                lines.append(f"  {name} x{count}")
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
    """Return the default set of bot command definitions.

    These populate the $COMMAND_DOCS section of the system prompt.
    Extended by downstream modules.
    """
    return {
        "stop": CommandDef(
            name="stop",
            description="Stop all current actions (movement, mining, combat).",
        ),
        "stats": CommandDef(
            name="stats",
            description="Report current health, food, position, and gamemode.",
        ),
        "inventory": CommandDef(
            name="inventory",
            description="List all items currently in the bot's inventory.",
        ),
        "goal": CommandDef(
            name="goal",
            description="Report the current pathfinding goal, if any.",
        ),
        "moveTo": CommandDef(
            name="moveTo",
            description="Move the bot to the given coordinates.",
            params={
                "x": {"type": "float", "description": "X coordinate"},
                "y": {"type": "float", "description": "Y coordinate"},
                "z": {"type": "float", "description": "Z coordinate"},
            },
        ),
        "collectBlocks": CommandDef(
            name="collectBlocks",
            description="Mine and collect blocks of the specified type.",
            params={
                "blockType": {
    "type": "string",
    "description": "Block type to collect, e.g. oak_log",
},
                "count": {"type": "int", "description": "Number of blocks to collect"},
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
