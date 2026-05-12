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
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from llm import InputIntent, InputRouter, Prompter
from llm.message_builder import CommandDef, ConversationTurn, MessageBuilder
from llm.prompter import LLMInput, LLMOutput
from llm.tool_parser import parse_response
from memory.file_store import FileMemoryStore
from memory.models import MemoryQuery
from memory.service import MemoryService
from skills.registry import SkillRegistry

from .conversation import ConversationManager
from .executor import ActionExecutor
from .models import AgentConfig, BotState, EntityInfo, InventoryItem, Position
from .modes import BehaviorMode, ModeEvaluator
from .safety_monitor import SafetyMonitor

logger = logging.getLogger(__name__)

_AFFIRMATIVE: frozenset[str] = frozenset({
    "yes", "yeah", "y", "yep", "sure", "ok", "okay", "alright",
    "of course", "go ahead", "yea", "yup", "mhm", "please",
})
_NEGATIVE: frozenset[str] = frozenset({
    "no", "nope", "nah", "n", "no thanks", "not now",
})
_QUERY_COMMANDS: frozenset[str] = frozenset({
    "stats",
    "inventory",
    "goal",
    "entities",
    "nearbyBlocks",
    "craftable",
    "help",
    "modes",
    "getCraftingPlan",
    "searchWiki",
    "showVillagerTrades",
})


@dataclass
class PendingTask:
    """Tracks a failed command so the bot can resume with player guidance."""

    command_name: str
    command_args: list[Any]
    command_kwargs: dict[str, Any] | None
    goal: str          # what the player originally asked for
    player_name: str   # who to follow for guidance
    failed_reason: str  # why the command failed


# ── Helpers ──────────────────────────────────────────────────────────


def _is_hard_failure(result: str) -> bool:
    """Return True for failures the LLM cannot recover from by retrying.

    Soft failures ("no oak_log nearby", "player not found") are fed back
    to the LLM so it can replan with a different approach.  Hard failures
    (command does not exist, bridge error) require player intervention.
    """
    hard_patterns = [
        "does not exist",
        "BridgeError",
        "not supported",
        "not available",
    ]
    result_lower = result.lower()
    return any(p.lower() in result_lower for p in hard_patterns)


def _describe_action(name: str, args: list[Any]) -> str:
    """Build a short human-readable description of what a command will do.

    Uses the command's registered description and argument values to
    produce a one-line summary suitable for in-game chat announcements.
    """
    cmd_defs = _command_defs()
    cmd = cmd_defs.get(name)
    param_names = list(cmd.params.keys()) if cmd else []

    if not args:
        if cmd:
            desc = cmd.description.rstrip(".")
            return desc[0].lower() + desc[1:]
        return name

    parts: list[str] = []
    for i, arg in enumerate(args):
        label = param_names[i] if i < len(param_names) else str(i)
        parts.append(f"{label}={arg}")

    return f"{name}({', '.join(parts)})"


def _tool_call_required_reminder() -> str:
    """Prompt nudge for models that ignore required tool calling."""
    return (
        "You must respond with a tool call (function call) for this request. "
        "Do NOT output plain text or reasoning-only content — pick the most "
        "appropriate command and call it. If you are unsure which command to "
        "use, call !help or !stats first."
    )


def _command_call_key(
    name: str,
    args: list[Any],
    kwargs: dict[str, Any] | None = None,
) -> str:
    """Stable key for detecting repeated calls with the same arguments."""
    payload = {
        "name": name.lstrip("!"),
        "args": args,
        "kwargs": kwargs or {},
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _query_repeat_reminder(name: str, result: str) -> str:
    """Prompt nudge when a model repeats the same read-only query."""
    return (
        f"You already called {name} with the same arguments and got this result:\n"
        f"{result[:1200]}\n\n"
        "Do NOT call the same query again. Use this information to take the next "
        "concrete action toward the player's request. For crafting tasks, choose "
        "an action such as craftRecipe for craftable items, collectBlocks/search "
        "for missing materials, or ask the player if the materials cannot be obtained."
    )


def _extract_js_code(text: str) -> str:
    """Extract the first JavaScript code block, or return stripped text."""
    match = re.search(r"```(?:js|javascript)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    if stripped.startswith("async ") or "bot." in stripped or "await " in stripped:
        return stripped
    return ""


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"false", "0", "off", "no", "disable", "disabled"}:
        return False
    if text in {"true", "1", "on", "yes", "enable", "enabled"}:
        return True
    return default


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
        self._last_message: str = ""
        self._shut_up: bool = False
        self._running: bool = False
        # Track recent action_call results for feedback intent (Phase 4 hook)
        self._recent_actions: list[dict[str, Any]] = []

        # Error recovery — guided task resumption
        self._pending_task: PendingTask | None = None
        self._guided_loop_task: asyncio.Task[None] | None = None
        self._self_prompt_goal: str | None = None
        self._self_prompt_task: asyncio.Task[None] | None = None
        self._self_prompt_running: bool = False

        # Memory layer (Phase 3/4a)
        self._memory: MemoryService | None = None
        self._knowledge: Any = None  # KnowledgeGraph (loaded lazily)

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

        # Phase 5a: skill registry
        skill_reg = SkillRegistry("src/skills/skills")
        await skill_reg.load()

        # Phase 1: message builder
        self._msg_builder = MessageBuilder(
            bot_name=self.name,
            commands=list(_command_defs().values()),
            self_prompt="",
            skill_registry=skill_reg,
        )

        # Phase 3/4a: memory layer
        from pathlib import Path
        memory_dir = Path("bots") / self.name / "memory"
        store = FileMemoryStore(memory_dir)
        self._memory = MemoryService(
            store,
            bot_name=self.name,
            base_dir=Path("bots") / self.name,
        )
        await self._memory.start()

        # Phase 7a: static knowledge (load lazily if data present)
        k_data = Path("data/minecraft/1.21.6")
        if k_data.exists():
            from knowledge.knowledge_graph import KnowledgeGraph
            self._knowledge = KnowledgeGraph(k_data)
            await self._knowledge.load()
            logger.info("KnowledgeGraph loaded for %s", self.name)
        else:
            logger.info("No minecraft-data found — KnowledgeGraph disabled")

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
        self._cancel_self_prompt()
        if self._memory:
            await self._memory.stop()
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
    ) -> LLMOutput:
        """Build the prompt and query the LLM for the next action.

        Args:
            bot_state: Current state snapshot from ``observe()``.
            tool_choice: Override tool selection. Pass ``"none"`` to forbid
                tool calls (used for question / chitchat intents).

        Returns:
            LLMOutput with content and any tool_calls.
        """
        if self._prompter is None or self._msg_builder is None:
            return LLMOutput(content="", model="")

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
        if self._self_prompt_goal:
            self._msg_builder.self_prompt = (
                f"CURRENT SELF-GOAL: {self._self_prompt_goal}\n"
                "Keep making progress toward this goal until you call endGoal. "
                "Prefer concrete tool calls over commentary.\n"
            )
        else:
            self._msg_builder.self_prompt = ""

        # Phase 3/4a: recall relevant long-term memory
        memory = await self._recall_memory(message=self._last_message)

        examples = ""  # TODO: wire up example retrieval (Phase 5)

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
        llm_task: asyncio.Task[LLMOutput] = asyncio.ensure_future(
            self._prompter.send_request(
                LLMInput(
                    history=messages[1:],  # exclude system message (already in turns)
                    system_message=system_prompt,
                    tool_choice=tool_choice,
                )
            )
        )
        if self._safety:
            self._safety.register_cancellable(llm_task)
        try:
            response = await llm_task
        except asyncio.CancelledError:
            logger.warning("LLM call cancelled (safety interrupt)")
            return LLMOutput(content="", model=self._prompter.model)
        finally:
            if self._safety:
                self._safety.clear_cancellables()

        return response

    # ── Execution ───────────────────────────────────────────────────

    async def execute_command(
        self,
        command_name: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> str | None:
        """Execute a parsed command and return feedback for the LLM history."""
        return await self._executor.execute(command_name, args, kwargs)

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

        # -1 mirrors the JS default "unlimited" — convert to effectively
        # infinite so the loop can run until the LLM produces an empty
        # result (mirrors JS Infinity behaviour).
        if max_responses is not None and max_responses < 0:
            max_responses = sys.maxsize

        logger.info(">>> %s: %s", source, message)
        self._last_message = message

        # ── 0. Check for yes/no when we have a pending guided task ───
        if self._pending_task is not None:
            msg_lower = message.strip().lower()
            if msg_lower in _AFFIRMATIVE:
                await self._start_guided_task(source)
                return False
            if msg_lower in _NEGATIVE:
                await self._cancel_pending_task()
                return False

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

        # Record L1 player message event
        await self._record_event("player_msg", {
            "source": source, "message": message,
            "intent": intent.value,
        })

        # Include the sender's name so the LLM can reference them in
        # commands like followPlayer / goToPlayer.
        self._history.append(
            ConversationTurn(role="user", content=f"{source}: {message}"),
        )
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

        For ``use_tools=True`` (action_command) we use ``tool_choice="required"``
        to force the LLM to always return structured tool_calls.  This eliminates
        the legacy ``!command(args)`` text-parsing path entirely — every response
        is in OpenAI tool-call format, so there are no quote-escaping bugs,
        model-artifact leaks, or regex mismatches.
        """
        used_command = False
        bot_state = await self.observe()
        tool_choice = "required" if use_tools else "none"
        consecutive_failures = 0
        last_failed: tuple[str, str] | None = None  # (command, args_str)
        no_tool_retries = 0  # F2: track re-prompts when LLM omits tool calls
        seen_query_results: dict[str, str] = {}
        warned_repeated_queries: set[str] = set()

        for _ in range(max_responses):
            if self._shut_up or not self._running:
                break
            if self._safety and self._safety.state.interrupted:
                logger.warning("Plan loop halted: safety interrupt active")
                break

            # Flush behavior log so the LLM sees what modes have been doing
            # (mirrors JS behavior_log flush before each promptConvo call).
            b_log = self._modes.flush_behavior_log()
            if b_log:
                self._history.append(
                    ConversationTurn(role="system", content=f"Recent behaviors:\n{b_log}"),
                )

            llm_response = await self.plan(bot_state, tool_choice=tool_choice)

            # Concise action summary: tool calls first, then truncated reply
            if llm_response.tool_calls:
                tc_names = [tc.function.name for tc in llm_response.tool_calls]
                logger.info("<<< tools=%s", ", ".join(tc_names))
            elif llm_response.content.strip():
                preview = llm_response.content.strip()[:80]
                logger.info("<<< %s", preview)
            else:
                logger.info("<<< (empty)")

            # Action path + tool_choice="required": some reasoning models can
            # spend tokens internally, return finish_reason="stop", and expose
            # neither content nor tool calls. Give them the same one-shot
            # correction we use for plain text without tool calls.
            if use_tools and not llm_response.tool_calls and not llm_response.content.strip():
                if no_tool_retries < 1:
                    no_tool_retries += 1
                    logger.warning(
                        "LLM produced empty response on action_command intent — "
                        "injecting tool-call reminder (retry %d/1)", no_tool_retries,
                    )
                    self._history.append(ConversationTurn(
                        role="system",
                        content=_tool_call_required_reminder(),
                    ))
                    continue
                logger.debug("Empty LLM response with no tool calls after retry — ending turn")
                break

            parsed = parse_response(
                llm_response.content,
                tool_calls=llm_response.tool_calls,
            )

            # Question / chitchat path: just remember and speak the reply,
            # no commands.
            if not use_tools:
                self._history.append(
                    ConversationTurn(
                        role="assistant",
                        content=llm_response.content,
                        tool_calls=llm_response.tool_calls,
                    ),
                )
                await self._send_chat(llm_response.content.strip())
                break

            # Non-action paths can still produce neither tool calls nor text.
            if not parsed.has_command and not llm_response.content.strip():
                logger.debug("Empty LLM response with no tool calls — ending turn")
                break

            if not parsed.has_command:
                # F2: Action path but model produced plain text with no tool
                # calls.  If we haven't retried yet, inject a reminder and
                # re-prompt once — the LLM may have forgotten it must produce
                # a tool call.  If it still refuses, speak the text so the
                # player at least sees the model's reasoning.
                if use_tools and no_tool_retries < 1:
                    no_tool_retries += 1
                    logger.warning(
                        "LLM produced no tool call on action_command intent — "
                        "injecting reminder (retry %d/1)", no_tool_retries,
                    )
                    self._history.append(ConversationTurn(
                        role="system",
                        content=_tool_call_required_reminder(),
                    ))
                    continue  # re-call plan() with tool_choice="required"

                # Speak plain text (question/chitchat path, or retry exhausted)
                await self._send_chat(llm_response.content.strip())
                break

            repeated_query: tuple[str, str] | None = None
            if use_tools:
                for cmd in parsed.commands:
                    cmd_key = _command_call_key(
                        cmd.name,
                        cmd.args,
                        getattr(cmd, "kwargs", None) or None,
                    )
                    normalized_name = cmd.name.lstrip("!")
                    if normalized_name in _QUERY_COMMANDS and cmd_key in seen_query_results:
                        repeated_query = (normalized_name, cmd_key)
                        break

            if repeated_query is not None:
                normalized_name, cmd_key = repeated_query
                if cmd_key not in warned_repeated_queries:
                    warned_repeated_queries.add(cmd_key)
                    logger.warning(
                        "LLM repeated query command %s with same arguments — "
                        "injecting next-step reminder",
                        normalized_name,
                    )
                    self._history.append(ConversationTurn(
                        role="system",
                        content=_query_repeat_reminder(
                            normalized_name,
                            seen_query_results[cmd_key],
                        ),
                    ))
                    continue
                logger.warning(
                    "LLM repeated query command %s after reminder — ending turn",
                    normalized_name,
                )
                await self._send_chat(
                    "I'm stuck repeating the same check instead of taking the next step."
                )
                return used_command

            # Append assistant turn — include tool_calls if present so
            # models that expect the OpenAI tool-call protocol see them.
            self._history.append(
                ConversationTurn(
                    role="assistant",
                    content=llm_response.content,
                    tool_calls=llm_response.tool_calls,
                ),
            )

            # Speak the pre-command text so the player knows what the bot
            # is doing (mirrors JS routeResponse before executeCommand).
            if parsed.text.strip():
                await self._send_chat(parsed.text.strip())

            for cmd in parsed.commands:
                if self._shut_up:
                    break
                if self._safety and self._safety.state.interrupted:
                    logger.warning("Command chain halted: safety interrupt")
                    break

                # Announce each step before executing
                cmd_args = cmd.args
                cmd_kwargs = getattr(cmd, "kwargs", None) or None
                cmd_key = _command_call_key(cmd.name, cmd_args, cmd_kwargs)
                normalized_name = cmd.name.lstrip("!")
                action_desc = _describe_action(
                    cmd.name,
                    cmd_args if cmd_args else list((cmd_kwargs or {}).values()),
                )
                await self._send_chat(action_desc)

                result = await self._execute_agent_command(cmd.name, cmd_args, cmd_kwargs)
                used_command = True
                self._record_action(cmd.name, cmd_args, result)
                await self._record_event("action_result", {
                    "command": cmd.name,
                    "args": cmd_args,
                    "kwargs": cmd_kwargs,
                    "result": result,
                })

                if result is None:
                    await self._send_chat(f"{action_desc} — no response")
                    break  # no feedback = stop chaining

                # Use role="tool" with tool_call_id when responding to an
                # OpenAI-format tool call; the provider rejects role="system"
                # after an assistant message that contains tool_calls.
                result_role: str = "system"
                result_tc_id: str | None = None
                for tc in llm_response.tool_calls:
                    tc_name = tc.function.name
                    if tc_name.startswith("action:"):
                        tc_name = tc_name[7:]
                    if tc_name == cmd.name:
                        result_role = "tool"
                        result_tc_id = tc.id
                        break

                self._history.append(
                    ConversationTurn(
                        role=result_role,
                        content=result,
                        tool_call_id=result_tc_id,
                    ),
                )

                # ── Failure classification ───────────────────────────
                is_fail = result and "FAILED:" in result
                args_str = ",".join(str(a) for a in cmd_args)
                if not args_str and cmd_kwargs:
                    args_str = ",".join(f"{k}={v}" for k, v in cmd_kwargs.items())
                this_fail_key = (cmd.name, args_str) if is_fail else None

                if _is_hard_failure(result):
                    logger.warning("Hard command failure — asking player: %s", result)
                    await self._send_chat(f"Failed: {result}")
                    self._pending_task = PendingTask(
                        command_name=cmd.name,
                        command_args=list(cmd_args),
                        command_kwargs=dict(cmd_kwargs or {}),
                        goal=self._last_message,
                        player_name=self._last_sender or "player",
                        failed_reason=result,
                    )
                    tool_choice = "none"
                    break  # don't execute remaining commands in this batch

                if is_fail:
                    consecutive_failures += 1
                    # Same command + args failed twice → inject hint
                    if (
                        last_failed is not None
                        and this_fail_key is not None
                        and this_fail_key == last_failed
                    ):
                        self._history.append(ConversationTurn(
                            role="system",
                            content=(
                                f"You just tried {cmd.name}({args_str}) and it failed "
                                f"again with the same error. Do NOT retry the same command "
                                f"with the same arguments — try a completely different "
                                f"approach instead. Check what went wrong and replan."
                            ),
                        ))
                    last_failed = this_fail_key

                    # 3 consecutive failures (any command) → stop and ask
                    if consecutive_failures >= 3:
                        logger.warning("3 consecutive failures — asking player to guide")
                        await self._send_chat(
                            "I'm stuck — I've tried several things but they're not working. "
                            "What should I try next?"
                        )
                        break
                else:
                    consecutive_failures = 0
                    last_failed = None
                    if normalized_name in _QUERY_COMMANDS:
                        seen_query_results[cmd_key] = result

                # Report success to the player
                await self._send_chat(f"Done: {result}")

                # Refresh bot state so the next LLM call sees updated
                # inventory, position, etc. (mirrors JS in-line !stats/!inventory).
                bot_state = await self.observe()

        self._history = self._convo_mgr.compress_history(self._history)
        return used_command

    async def _execute_agent_command(
        self,
        command_name: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> str | None:
        """Handle commands that need Agent runtime state, else use executor."""
        name = command_name.lstrip("!")
        values = args if args else list((kwargs or {}).values())
        if name == "setGoal":
            goal = str((kwargs or {}).get("self_prompt", values[0] if values else ""))
            return await self._set_self_goal(goal)
        if name == "endGoal":
            return await self._end_self_goal()
        if name == "setMode":
            mode_name = str((kwargs or {}).get("mode_name", values[0] if values else ""))
            on_raw = (kwargs or {}).get("on", values[1] if len(values) > 1 else True)
            on = _as_bool(on_raw, default=True)
            return self._modes.set_enabled(mode_name, on)
        if name == "modes":
            return self._modes.status_text()
        if name == "newAction":
            prompt = str(
                (kwargs or {}).get("code_prompt", values[0] if values else self._last_message)
            )
            return await self._execute_dynamic_action(prompt)
        if name == "getCraftingPlan":
            target = str((kwargs or {}).get("target_item", values[0] if values else ""))
            quantity = int((kwargs or {}).get("quantity", values[1] if len(values) > 1 else 1) or 1)
            kg_plan = await self._get_knowledge_crafting_plan(target, quantity)
            if kg_plan is not None:
                return kg_plan
        if name == "craftRecipe":
            args, kwargs = await self._normalise_craft_recipe_args(args, kwargs)
        if name == "stfu":
            self.shut_up()
            return "Bot muted."
        if name == "unshut":
            self.unshut()
            return "Bot unmuted."
        if name == "clearChat":
            self._history.clear()
            return "Chat history cleared."
        return await self.execute_command(command_name, args, kwargs)

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

    # ── Guided task recovery ─────────────────────────────────────────

    async def _start_guided_task(self, player_name: str) -> None:
        """Player said yes — follow them and periodically retry the pending task."""
        pt = self._pending_task
        if pt is None or self._bridge is None:
            return

        self._pending_task = None  # consumed
        logger.info(
            "Guided task: following %s, retrying !%s every few seconds",
            player_name, pt.command_name,
        )

        await self._send_chat(
            "Okay, following you! Lead me to what I need."
        )

        # Cancel any previous guided loop
        self._cancel_guided_loop()

        self._guided_loop_task = asyncio.create_task(
            self._run_guided_loop(pt, player_name)
        )

    async def _cancel_pending_task(self) -> None:
        """Player said no — ask what they want to do next."""
        self._pending_task = None
        self._cancel_guided_loop()

        if self._prompter and self._msg_builder and self._bridge:
            await self._send_chat(
                "No problem! What would you like me to do instead?"
            )

    def _cancel_guided_loop(self) -> None:
        if self._guided_loop_task and not self._guided_loop_task.done():
            self._guided_loop_task.cancel()
        self._guided_loop_task = None

    async def _set_self_goal(self, goal: str) -> str:
        goal = goal.strip()
        if not goal:
            return "FAILED: setGoal requires a non-empty goal."
        self._self_prompt_goal = goal
        self._cancel_self_prompt()
        self._self_prompt_task = asyncio.create_task(self._run_self_prompt_loop(goal))
        self._history.append(ConversationTurn(
            role="system",
            content=(
                f"Persistent self-goal set: {goal}. Continue planning steps "
                "toward it until endGoal is called."
            ),
        ))
        return f"Goal set: {goal}. Self-prompting is active."

    async def _end_self_goal(self) -> str:
        old_goal = self._self_prompt_goal
        self._self_prompt_goal = None
        self._cancel_self_prompt()
        self._history.append(ConversationTurn(
            role="system",
            content=f"Persistent self-goal ended: {old_goal or 'none'}.",
        ))
        return "Goal ended. Self-prompting stopped."

    def _cancel_self_prompt(self) -> None:
        if self._self_prompt_task and not self._self_prompt_task.done():
            self._self_prompt_task.cancel()
        self._self_prompt_task = None

    async def _run_self_prompt_loop(self, goal: str) -> None:
        """Periodic autonomous plan loop for setGoal, mirroring JS self_prompter."""
        try:
            while self._running and self._self_prompt_goal == goal:
                await asyncio.sleep(5)
                if (
                    self._shut_up
                    or self._pending_task is not None
                    or self._self_prompt_running
                    or self._prompter is None
                ):
                    continue
                self._self_prompt_running = True
                try:
                    self._history.append(ConversationTurn(
                        role="user",
                        content=(
                            f"system: Continue working toward your current "
                            f"self-goal: {goal}. Call exactly one useful tool. "
                            "If the goal is complete, call endGoal."
                        ),
                    ))
                    await self._run_plan_loop(max_responses=1, use_tools=True)
                finally:
                    self._self_prompt_running = False
        except asyncio.CancelledError:
            pass

    async def _execute_dynamic_action(self, prompt: str) -> str:
        """Ask the LLM for a JavaScript action and execute it through the bridge."""
        if self._prompter is None or self._msg_builder is None:
            return "FAILED: LLM is not initialized."
        if self._bridge is None or not hasattr(self._bridge, "execute_code"):
            return "FAILED: Socket bridge does not support dynamic code execution."

        bot_state = await self.observe()
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
        code_docs = self._msg_builder.build_code_docs(query=prompt, limit=8)
        memory = await self._recall_memory(message=prompt)
        system_prompt = self._msg_builder.build_system_prompt(
            stats=stats,
            inventory=self._format_inventory(bot_state),
            code_docs=code_docs,
            memory=memory,
            for_coding=True,
        )
        messages = self._msg_builder.build_messages(
            history=[
                *self._history[-12:],
                ConversationTurn(
                    role="user",
                    content=(
                        "Write one JavaScript code block for this Minecraft action: "
                        f"{prompt}"
                    ),
                ),
            ],
            system_prompt=system_prompt,
        )
        response = await self._prompter.send_request(LLMInput(
            history=messages[1:],
            system_message=system_prompt,
            tool_choice="none",
        ))
        code = _extract_js_code(response.content)
        if not code:
            return "FAILED: LLM did not return a JavaScript code block."
        exec_result = await self._bridge.execute_code(code, timeout_ms=60000)
        if isinstance(exec_result, dict):
            status = "ok" if exec_result.get("ok", True) else "failed"
            output = exec_result.get("output") or exec_result.get("result") or ""
            return f"Dynamic action {status}:\n{output}".strip()
        return f"Dynamic action result:\n{exec_result}"

    async def _run_guided_loop(
        self, pt: PendingTask, player_name: str,
    ) -> None:
        """Follow player and retry the failed command every few seconds."""
        if self._bridge is None:
            return

        # Start following the player
        try:
            await self._executor.execute(
                "followPlayer", [player_name, 4],
            )
        except Exception:
            logger.exception("followPlayer failed in guided loop")

        deadline = time.time() + 120  # 2 min timeout
        check_interval = 4  # seconds between retries
        last_status = time.time()

        try:
            while self._running and time.time() < deadline:
                await asyncio.sleep(check_interval)

                # Quick observe to refresh bot state
                bot_state = await self._observe_quick()
                if not bot_state.is_alive:
                    await self._send_chat("I died! Can't complete the task.")
                    return

                # Retry the failed command
                result = await self.execute_command(
                    pt.command_name, pt.command_args, pt.command_kwargs,
                )

                if result is None or "failed" not in result:
                    # Stop following, then re-trigger the plan loop with
                    # the original goal to continue the multi-step task.
                    try:
                        if self._bridge:
                            await self._bridge.stop()
                    except Exception:
                        pass
                    await self._send_chat(
                        f"Found it! Now continuing with: {pt.goal}"
                    )
                    self._history.append(ConversationTurn(
                        role="user",
                        content=f"{player_name}: {pt.goal}",
                    ))
                    await self._run_plan_loop(
                        max_responses=self._config.max_commands,
                        use_tools=True,
                    )
                    return

                # Still failing — send periodic status updates
                now = time.time()
                if now - last_status > 15:
                    await self._send_chat(
                        "Still following you... I haven't found what I need yet."
                    )
                    last_status = now

            # Timeout
            await self._send_chat(
                "I've been following for a while but couldn't find what I "
                "needed. What should I do next?"
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Guided loop error")
        finally:
            # Stop following
            try:
                if self._bridge:
                    await self._bridge.stop()
            except Exception:
                pass

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

    # ── Memory helpers (Phase 3/4a) ──────────────────────────────────

    async def _recall_memory(self, *, message: str = "") -> str:
        """Build a memory string from recalled long-term records."""
        if self._memory is None:
            return ""

        keywords = [w for w in message.lower().split() if len(w) > 2] if message else []
        results = await self._memory.recall(MemoryQuery(
            keywords=keywords, limit=5, min_confidence=0.5,
        ))
        if not results:
            return ""

        parts: list[str] = []
        for r in results:
            parts.append(f"- {r.record.description} ({r.record.type.value}/{r.record.name})")
        return "\n".join(parts)

    async def _record_event(
        self, event_type: str, payload: dict[str, Any],
    ) -> None:
        """Fire-and-forget L1 event recording."""
        if self._memory is not None:
            await self._memory.record_event(event_type, payload)

    def _query_knowledge(
        self, target: str, inventory: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Query the static KnowledgeGraph for recipe / tool info."""
        if self._knowledge is None:
            return None

        missing = self._knowledge.get_missing_materials(target, inventory=inventory)
        recipe = self._knowledge.get_recipe(target)
        collection = self._knowledge.get_collection_path(target)

        return {
            "target": target,
            "missing": missing,
            "recipe": recipe[:3] if recipe else [],
            "collection": collection,
        }

    async def _get_knowledge_crafting_plan(
        self, target_item: str, quantity: int = 1,
    ) -> str | None:
        """Use Python KnowledgeGraph for crafting plans when available."""
        if self._knowledge is None:
            return None
        bot_state = await self.observe()
        inventory = [
            {"name": item.name, "count": item.count}
            for item in bot_state.inventory
        ]
        try:
            return self._knowledge.get_crafting_plan(
                target_item, quantity=quantity, inventory=inventory,
            )
        except Exception:
            logger.exception("KnowledgeGraph crafting plan failed")
            return None

    async def _normalise_craft_recipe_args(
        self,
        args: list[Any],
        kwargs: dict[str, Any] | None,
    ) -> tuple[list[Any], dict[str, Any] | None]:
        """Resolve fuzzy craft targets before sending them to Mineflayer."""
        if self._knowledge is None:
            return args, kwargs

        kw = dict(kwargs or {})
        values = args if args else list(kw.values())
        raw_name = kw.get("recipe_name", values[0] if values else None)
        if not raw_name:
            return args, kwargs

        bot_state = await self.observe()
        inventory = [
            {"name": item.name, "count": item.count}
            for item in bot_state.inventory
        ]
        resolved = self._knowledge.resolve_craft_target(str(raw_name), inventory)
        if args:
            new_args = list(args)
            new_args[0] = resolved
            return new_args, kwargs
        kw["recipe_name"] = resolved
        return args, kw

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
        "unshut": CommandDef(
            name="unshut",
            description="Resume chatting and self-narration after stfu.",
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
                "block_type": {"type": "string", "description": "Block type, e.g. oak_log"},
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
            description="Craft a recipe using available materials. Automatically "
            "detects nearby crafting tables and uses them. If no table is nearby "
            "but one is in inventory, places it, crafts, then picks it back up. "
            "Some recipes (beds, tools, armor) require a placed crafting table. "
            "Always use getCraftingPlan before crafting complex items.",
            params={
                "recipe_name": {"type": "string", "description": "Item to craft, e.g. stick"},
                "num": {"type": "int", "description": "Number to craft (default 1)"},
            },
        ),
        "smeltItem": CommandDef(
            name="smeltItem",
            description="Smelt items in a furnace. Requires a furnace nearby and fuel + "
            "input items in inventory.",
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
        "newAction": CommandDef(
            name="newAction",
            description=(
                "Generate and run a one-off JavaScript Mineflayer action when "
                "built-in commands are not expressive enough."
            ),
            params={
                "code_prompt": {
                    "type": "string",
                    "description": "Natural language description of the custom action to code.",
                },
            },
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
            description="Get a comprehensive crafting plan for a target item. Shows "
            "required ingredients with exact quantities, what you already have, "
            "and what you still need to gather. Always use this before crafting "
            "complex items to know which intermediate steps are needed.",
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
