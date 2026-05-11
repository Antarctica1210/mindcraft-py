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

from llm import InputIntent, InputRouter, Prompter
from llm.message_builder import CommandDef, ConversationTurn, MessageBuilder
from llm.tool_parser import parse_response

from .conversation import ConversationManager
from .executor import ActionExecutor
from .models import AgentConfig, BotState, EntityInfo, InventoryItem, Position
from .modes import BehaviorMode, ModeEvaluator
from .safety_monitor import SafetyMonitor

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

    # Default config path for InputRouter keyword tables.
    DEFAULT_INTENT_CONFIG = "config/intent_keywords.yaml"

    # Cap on action_call payloads we feed back to InputRouter / feedback handler.
    _RECENT_ACTIONS_WINDOW = 5

    def __init__(self, config: AgentConfig):
        self._config = config
        self.name = config.name

        # Phase 1 components (initialised in start())
        self._prompter: Prompter | None = None
        self._msg_builder: MessageBuilder | None = None
        self._router: InputRouter | None = None

        # Phase 2 components
        self._executor = ActionExecutor()
        self._convo_mgr = ConversationManager()
        self._modes = ModeEvaluator()
        self._safety: SafetyMonitor | None = None

        # Runtime state
        self._bridge: Any = None
        self._history: list[ConversationTurn] = []
        self._last_sender: str | None = None
        self._shut_up: bool = False
        self._running: bool = False
        # Track recent action_call results for feedback intent (Phase 4 hook)
        self._recent_actions: list[dict[str, Any]] = []

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

        # Phase 1: register handlers FIRST so tool defs can be derived from them
        self._executor._register_builtins()
        self._executor.set_bridge(bridge)

        # Phase 1: tool definitions (action functions only)
        tools = _build_tool_defs(self._executor._handlers)
        if tools:
            self._prompter.register_tools(tools)

        # Phase 1: input intent classifier — pure regex/keyword, no LLM dep
        self._router = InputRouter.from_config(self.DEFAULT_INTENT_CONFIG)

        # Phase 1: message builder
        self._msg_builder = MessageBuilder(
            bot_name=self.name,
            commands=list(_command_defs().values()),
            self_prompt="",
        )

        # Phase 2: safety polling (Plan A — Python-side observe loop)
        self._safety = SafetyMonitor(bridge)
        await self._safety.start()

        logger.info(
            "Agent %s started (model=%s, router=on, safety=on)",
            self.name, self._prompter.model,
        )

    async def stop(self) -> None:
        """Gracefully shut down the agent."""
        self._running = False
        if self._safety:
            await self._safety.stop()
        if self._bridge:
            try:
                await self._bridge.stop()
            except Exception:
                pass
        logger.info("Agent %s stopped", self.name)

    # ── Observation ─────────────────────────────────────────────────

    async def observe(self) -> BotState:
        """Gather full bot state via concurrent SocketBridge queries.

        One ``asyncio.gather`` call covers observe / inventory / entities /
        nearby blocks / current goal. Per-call failures fall back to defaults
        without poisoning the others.
        """
        bridge = self._bridge
        if bridge is None:
            return BotState()

        results = await asyncio.gather(
            bridge.observe(),
            bridge.get_inventory(),
            bridge.get_entities(),
            bridge.get_nearby_blocks(),
            bridge.get_goal(),
            return_exceptions=True,
        )
        base, inv_raw, ents_raw, blocks_raw, goal_raw = results

        if isinstance(base, Exception) or not isinstance(base, dict):
            logger.debug("observe() failed: %r", base)
            return BotState()

        p = base.get("position", {}) or {}

        inventory: list[InventoryItem] = []
        if isinstance(inv_raw, list):
            for item in inv_raw[:30]:
                if isinstance(item, dict):
                    inventory.append(InventoryItem(
                        name=item.get("name", "unknown"),
                        count=int(item.get("count", 0)),
                    ))

        entities: list[EntityInfo] = []
        if isinstance(ents_raw, list):
            self_pos = Position(
                x=p.get("x", 0), y=p.get("y", 0), z=p.get("z", 0),
            )
            for e in ents_raw[:20]:
                if not isinstance(e, dict):
                    continue
                epos_raw = e.get("position", {}) or {}
                epos = Position(
                    x=epos_raw.get("x", 0),
                    y=epos_raw.get("y", 0),
                    z=epos_raw.get("z", 0),
                )
                entities.append(EntityInfo(
                    name=e.get("name", ""),
                    entity_type=e.get("type", ""),
                    position=epos,
                    distance=self_pos.distance_to(epos),
                    health=float(e.get("health", 0) or 0),
                ))

        nearby_block_types: list[str] = []
        if isinstance(blocks_raw, list):
            nearby_block_types = [str(b) for b in blocks_raw]

        current_goal: str | None = None
        if isinstance(goal_raw, str):
            current_goal = goal_raw or None
        elif goal_raw is not None and not isinstance(goal_raw, Exception):
            current_goal = str(goal_raw)

        return BotState(
            health=base.get("health", 20),
            food=base.get("food", 20),
            position=Position(x=p.get("x", 0), y=p.get("y", 0), z=p.get("z", 0)),
            gamemode=base.get("gamemode", "survival"),
            time_of_day=base.get("time_of_day", ""),
            biome=base.get("biome", ""),
            entities_nearby=base.get("entities_nearby", ""),
            blocks_nearby=base.get("blocks_nearby", ""),
            players_nearby=base.get("players_nearby", ""),
            inventory=inventory,
            entities=entities,
            nearby_block_types=nearby_block_types,
            current_goal=current_goal,
        )

    # ── Planning ────────────────────────────────────────────────────

    async def plan(
        self,
        bot_state: BotState,
        *,
        tool_choice: str | None = None,
    ) -> str:
        """Build the prompt and query the LLM for the next action.

        Args:
            bot_state: Current state snapshot from ``observe()``.
            tool_choice: Override tool selection. Pass ``"none"`` to forbid
                tool calls (used for question / chitchat intents).
        """
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

        inventory = self._format_inventory(bot_state)

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

        # Wrap the LLM call so SafetyMonitor can cancel us on danger.
        llm_task: asyncio.Task[Any] = asyncio.ensure_future(
            self._prompter.send_request(
                turns=messages[1:],  # exclude system message (already in turns)
                system_message=system_prompt,
                tool_choice=tool_choice,
            )
        )
        if self._safety:
            self._safety.register_cancellable(llm_task)
        try:
            response = await llm_task
        except asyncio.CancelledError:
            logger.warning("LLM call cancelled (safety interrupt)")
            return ""
        finally:
            if self._safety:
                self._safety.clear_cancellables()

        return response.content

    # ── Execution ───────────────────────────────────────────────────

    async def execute_command(self, command_name: str, args: list[Any]) -> str | None:
        """Execute a parsed command and return feedback for the LLM history."""
        return await self._executor.execute(command_name, args)

    # ── Message handling (mirrors agent.js handleMessage) ────────────

    async def handle_message(
        self, source: str, message: str, max_responses: int | None = None
    ) -> bool:
        """Handle an incoming player message.

        Classifies intent through ``InputRouter`` first, then dispatches:

          * ``meta_command``    → ``_handle_meta_command`` (MemoryService stub)
          * ``feedback``        → ``_handle_feedback``    (Phase 4a stub)
          * ``ambiguous``       → ``_handle_ambiguous``   (ask to clarify)
          * ``action_command``  → main LLM, ``tool_choice="auto"``
          * ``question``/``chitchat`` → main LLM, ``tool_choice="none"``

        Returns ``True`` iff at least one tool/action was executed.
        """
        if not self._running or not self._prompter:
            return False

        if max_responses is None:
            max_responses = self._config.max_commands

        # ── 1. Classify intent ────────────────────────────────────────
        intent = InputIntent.ACTION_COMMAND
        matched: str | None = None
        if self._router is not None:
            decision = self._router.classify(message)
            intent = decision.intent
            matched = decision.matched_pattern
        logger.info(
            "InputRouter: source=%s intent=%s match=%r",
            source, intent.value, matched,
        )

        # History gets the user turn regardless of intent — it's the truth
        # source for both Conversation and (later) Phase 3 episodic L1 NDJSON.
        self._history.append(ConversationTurn(role="user", content=message))
        self._last_sender = source

        # ── 2. Dispatch by intent ─────────────────────────────────────
        if intent == InputIntent.META_COMMAND:
            await self._handle_meta_command(message, source)
            return False
        if intent == InputIntent.FEEDBACK:
            await self._handle_feedback(message, source)
            return False
        if intent == InputIntent.AMBIGUOUS:
            await self._handle_ambiguous(message)
            return False

        # action_command / question / chitchat → main LLM
        use_tools = intent == InputIntent.ACTION_COMMAND
        return await self._run_plan_loop(
            max_responses=max_responses, use_tools=use_tools,
        )

    # ── Plan loop (action / question / chitchat) ────────────────────

    async def _run_plan_loop(
        self, *, max_responses: int, use_tools: bool,
    ) -> bool:
        """Drive plan → parse → execute repeats for tool-capable intents.

        For ``use_tools=False`` (question/chitchat) the LLM is called with
        ``tool_choice="none"`` and the reply is spoken once, no commands.
        """
        used_command = False
        bot_state = await self.observe()
        tool_choice = None if use_tools else "none"

        for _ in range(max_responses):
            if self._shut_up or not self._running:
                break
            if self._safety and self._safety.state.interrupted:
                logger.warning("Plan loop halted: safety interrupt active")
                break

            llm_output = await self.plan(bot_state, tool_choice=tool_choice)
            if not llm_output.strip():
                logger.debug("Empty LLM response — ending turn")
                break

            self._history.append(
                ConversationTurn(role="assistant", content=llm_output),
            )

            # Question / chitchat path: just speak the reply, no commands.
            if not use_tools:
                await self._send_chat(llm_output.strip())
                break

            parsed = parse_response(llm_output)
            if not parsed.has_command:
                # Action path but model chose plain text — speak it and stop.
                await self._send_chat(llm_output.strip())
                break

            for cmd in parsed.commands:
                if self._shut_up:
                    break
                if self._safety and self._safety.state.interrupted:
                    logger.warning("Command chain halted: safety interrupt")
                    break

                result = await self.execute_command(cmd.name, cmd.args)
                used_command = True
                self._record_action(cmd.name, cmd.args, result)

                if result:
                    self._history.append(
                        ConversationTurn(role="system", content=result),
                    )
                else:
                    break  # no feedback = stop chaining

        self._history = self._convo_mgr.compress_history(self._history)
        return used_command

    # ── Intent-specific handlers ────────────────────────────────────

    async def _handle_meta_command(self, message: str, source: str) -> None:
        """Phase 3/4a hook — route to MemoryService write candidate.

        For now: log + acknowledge. Wired into MemoryPolicy once Phase 3
        lands.
        """
        logger.info("META_COMMAND from %s: %s", source, message)
        await self._send_chat(
            f"Memory command noted: '{message}'. "
            "(MemoryService lands in Phase 3/4a.)"
        )

    async def _handle_feedback(self, message: str, source: str) -> None:
        """Phase 4a hook — feedback candidate + recent action trace.

        Logs the last few action_calls so a future feedback generator can
        correlate the complaint with what the bot just did.
        """
        recent = [a["name"] for a in self._recent_actions]
        logger.info(
            "FEEDBACK from %s: %s (last %d actions: %s)",
            source, message, len(self._recent_actions), recent,
        )
        await self._send_chat(
            f"Got it — I'll learn from '{message}'. "
            "(Feedback memory lands in Phase 4a.)"
        )

    async def _handle_ambiguous(self, message: str) -> None:
        """Ask the player to clarify rather than guess."""
        logger.info("AMBIGUOUS: %s", message)
        await self._send_chat(
            f"Did you want me to do something with '{message}', "
            "or are we chatting? Try a verb (e.g. 'go ...', 'remember ...')."
        )

    async def _send_chat(self, text: str) -> None:
        """Speak ``text`` through the bridge; quiet on failure."""
        if self._bridge is None or not text:
            return
        try:
            await self._bridge.chat(text)
        except Exception:
            logger.debug("Failed to send chat", exc_info=True)

    def _record_action(
        self, name: str, args: list[Any], result: str | None,
    ) -> None:
        """Keep a small ring of recent action_calls for FEEDBACK handler."""
        self._recent_actions.append({
            "name": name, "args": args, "result": result, "ts": time.time(),
        })
        if len(self._recent_actions) > self._RECENT_ACTIONS_WINDOW:
            self._recent_actions = self._recent_actions[
                -self._RECENT_ACTIONS_WINDOW:
            ]

    # ── Main loop ───────────────────────────────────────────────────

    async def run_loop(self, tick_interval: float = 0.05) -> None:
        """Run the agent main loop: safety check → quick observe → modes.

        Top-level loop, default 20 TPS (50 ms). Only does a *quick* observe
        (single ``bridge.observe()`` call) — the full concurrent gather is
        reserved for ``handle_message``. SafetyMonitor runs its own poll
        independently at 250 ms.
        """
        while self._running:
            self._tick_count += 1

            try:
                # 1. Safety interrupt — stop motion, clear modes, recover.
                if self._safety and self._safety.state.interrupted:
                    logger.warning(
                        "SafetyMonitor interrupt: %s — stopping motion "
                        "(emergency_escape skill lands in Phase 5)",
                        self._safety.state.reason,
                    )
                    try:
                        if self._bridge:
                            await self._bridge.stop()
                    except Exception:
                        pass
                    self._safety.clear_interrupt()
                    await asyncio.sleep(0.5)
                    continue

                # 2. Cheap tick observe — modes only need health/pos/blocks.
                bot_state = await self._observe_quick()
                if not bot_state.is_alive:
                    await asyncio.sleep(1.0)
                    continue

                # 3. Evaluate reactive behavior modes.
                triggered = await self._modes.tick(bot_state, self._bridge)
                if triggered:
                    logger.debug(
                        "Tick %d: mode %s triggered", self._tick_count, triggered,
                    )

                # 4. Stuck recovery follow-up.
                if (
                    self._bridge is not None
                    and self._modes.is_active(BehaviorMode.UNSTUCK)
                ):
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

    async def _observe_quick(self) -> BotState:
        """Cheap single-call observe for the high-frequency tick loop.

        Skips the inventory / entities / nearby-blocks / goal queries so we
        only pay one network round-trip per tick. Falls back to defaults on
        failure so the tick never crashes.
        """
        bridge = self._bridge
        if bridge is None:
            return BotState()
        try:
            s = await bridge.observe()
        except Exception:
            logger.debug("_observe_quick failed", exc_info=True)
            return BotState()
        p = s.get("position", {}) or {}
        return BotState(
            health=s.get("health", 20),
            food=s.get("food", 20),
            position=Position(x=p.get("x", 0), y=p.get("y", 0), z=p.get("z", 0)),
            gamemode=s.get("gamemode", "survival"),
            time_of_day=s.get("time_of_day", ""),
            biome=s.get("biome", ""),
            entities_nearby=s.get("entities_nearby", ""),
            blocks_nearby=s.get("blocks_nearby", ""),
            players_nearby=s.get("players_nearby", ""),
        )

    def _format_inventory(self, state: BotState) -> str:
        """Format inventory from a populated ``BotState`` for the prompt.

        Pulls directly from ``state.inventory`` (already fetched in the
        concurrent ``observe()`` gather), no extra network call.
        """
        if not state.inventory:
            return "INVENTORY: empty"
        lines = ["INVENTORY:"]
        for item in state.inventory[:20]:
            lines.append(f"  {item.name} x{item.count}")
        return "\n".join(lines)

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
