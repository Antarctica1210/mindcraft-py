"""
Phase 2 — Agent core loop.

The central ``Agent`` class wires together:
  - Prompter (LLM provider, Phase 1)
  - MessageBuilder (prompt assembly, Phase 1)
  - action_parser (response parsing, Phase 1)
  - ActionExecutor (command dispatch, Phase 2)
  - ModeEvaluator (reactive behavior, Phase 2)
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

from llm import ExampleStore, InputIntent, InputRouter, PlanningStrategy, Prompter
from llm.dispatch_gate import DispatchDecision, DispatchGate
from llm.message_builder import CommandDef, ConversationTurn, MessageBuilder
from llm.prompter import LLMInput, LLMOutput
from memory.file_store import ScopedFileMemoryStore
from memory.models import MemoryQuery, MemoryRecord, MemoryType, Scope
from memory.service import MemoryService
from skills.loader import SkillStore
from skills.matcher import SkillMatch, SkillMatcher
from skills.registry import SkillRegistry

from .action_result import ActionResult
from .chat_presenter import compact_chat_text as _compact_chat_text
from .command_registry import (
    PRE_ACTION_OBSERVE_COMMANDS as _PRE_ACTION_OBSERVE_COMMANDS,
)
from .command_registry import (
    TERMINAL_COMPLETION_KINDS as _TERMINAL_COMPLETION_KINDS,
)
from .command_registry import (
    command_completion_kind as _command_completion_kind,
)
from .command_registry import (
    command_defs,
)
from .command_registry import (
    command_preflight_hooks as _command_preflight_hooks,
)
from .command_registry import (
    shortcut_route_known as _shortcut_route_known,
)
from .command_registry import (
    world_change_command_can_complete as _world_change_command_can_complete,
)
from .command_registry import (
    world_change_command_has_intrinsic_effect as _world_change_command_has_intrinsic_effect,
)
from .command_runner import CommandRun, CommandRunner
from .completion import (
    CompletionEngine,
    CompletionSpec,
)
from .completion import (
    inventory_count_any_from_state as _inventory_count_any_from_state,
)
from .completion import (
    inventory_count_from_state as _inventory_count_from_state,
)
from .completion import (
    inventory_counts_from_state as _inventory_counts_from_state,
)
from .completion import (
    inventory_delta_from_baseline as _inventory_delta_from_baseline,
)
from .completion import (
    position_tuple_from_state as _position_tuple_from_state,
)
from .conversation import ConversationManager
from .effects import action_effects_from_result as _action_effects_from_result
from .effects import action_succeeded as _action_succeeded
from .executor import ActionExecutor
from .failures import classify_failure as _classify_failure
from .models import AgentConfig, BotState, EntityInfo, InventoryItem, Position
from .modes import BehaviorMode, ModeEvaluator
from .planner_loop import PlannerLoop
from .recovery import RecoveryManager
from .safety_monitor import SafetyMonitor
from .task_goal_parser import acceptable_items_for_target as _acceptable_items_for_target
from .task_goal_parser import cook_request_is_compound as _cook_request_is_compound
from .task_goal_parser import is_navigation_only_request as _is_navigation_only_request
from .task_goal_parser import parse_cook_request as _parse_cook_request
from .task_goal_parser import parse_craft_request as _parse_craft_request
from .task_goal_parser import parse_item_request as _parse_item_request
from .task_manager import ActiveTask, ActiveTaskEvent, ActiveTaskSnapshot, PendingTask, TaskManager

logger = logging.getLogger(__name__)

_AFFIRMATIVE: frozenset[str] = frozenset({
    "yes", "yeah", "y", "yep", "sure", "ok", "okay", "alright",
    "of course", "go ahead", "yea", "yup", "mhm", "please",
})
_NEGATIVE: frozenset[str] = frozenset({
    "no", "nope", "nah", "n", "no thanks", "not now",
})
_REUSABLE_WORKSTATIONS: frozenset[str] = frozenset({
    "crafting_table",
    "furnace",
    "campfire",
    "blast_furnace",
    "smoker",
})
_DELIVERY_RESULT_RE = re.compile(r"\bGave\s+(?P<count>\d+)x\s+", re.IGNORECASE)

_MAX_RECOVERY_ATTEMPTS_PER_TASK = 3
_DELIVERY_PRIMARY_WEIGHT = 0.70
_DELIVERY_SMALL_MODEL_WEIGHT = 0.30
_DELIVERY_DECISION_THRESHOLD = 0.50
_SEED_GRASS_BLOCKS: frozenset[str] = frozenset({"grass", "short_grass", "tall_grass"})
_WOOD_TYPES: tuple[str, ...] = (
    "oak",
    "spruce",
    "birch",
    "jungle",
    "acacia",
    "dark_oak",
    "mangrove",
    "cherry",
    "pale_oak",
    "crimson",
    "warped",
)
_WOOD_LOG_BLOCKS: frozenset[str] = frozenset({
    *(f"{wood}_log" for wood in _WOOD_TYPES if wood not in {"crimson", "warped"}),
    *(f"stripped_{wood}_log" for wood in _WOOD_TYPES if wood not in {"crimson", "warped"}),
    "crimson_stem",
    "warped_stem",
    "stripped_crimson_stem",
    "stripped_warped_stem",
})
_WOOD_PLANK_ITEMS: frozenset[str] = frozenset({
    *(f"{wood}_planks" for wood in _WOOD_TYPES),
    "bamboo_planks",
})
_GENERIC_WOOD_BLOCK_ALIASES: frozenset[str] = frozenset({
    "wood",
    "log",
    "logs",
    "any_log",
    "tree",
})
_COAL_ORE_BLOCKS: frozenset[str] = frozenset({"coal_ore", "deepslate_coal_ore"})
_SMELT_INPUT_ALIASES: dict[str, str] = {
    "raw_porkchop": "porkchop",
    "raw_beef": "beef",
    "raw_chicken": "chicken",
    "raw_mutton": "mutton",
    "raw_rabbit": "rabbit",
    "raw_cod": "cod",
    "raw_salmon": "salmon",
}
_COMMON_SMELT_OUTPUTS: dict[str, str] = {
    "porkchop": "cooked_porkchop",
    "beef": "cooked_beef",
    "chicken": "cooked_chicken",
    "mutton": "cooked_mutton",
    "rabbit": "cooked_rabbit",
    "cod": "cooked_cod",
    "salmon": "cooked_salmon",
    "potato": "baked_potato",
    "raw_iron": "iron_ingot",
    "raw_gold": "gold_ingot",
    "raw_copper": "copper_ingot",
    "iron_ore": "iron_ingot",
    "deepslate_iron_ore": "iron_ingot",
    "gold_ore": "gold_ingot",
    "deepslate_gold_ore": "gold_ingot",
    "copper_ore": "copper_ingot",
    "deepslate_copper_ore": "copper_ingot",
    "sand": "glass",
    "red_sand": "glass",
    "cobblestone": "stone",
    "stone": "smooth_stone",
    "clay_ball": "brick",
    "netherrack": "nether_brick",
}
_COMMON_FUEL_SMELTS: dict[str, float] = {
    "lava_bucket": 100.0,
    "coal": 8.0,
    "charcoal": 8.0,
    "blaze_rod": 12.0,
    "stick": 0.5,
}
# ── Helpers ──────────────────────────────────────────────────────────


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


def _strip_memory_verbs(message: str) -> str:
    text = message.strip()
    patterns = [
        r"^\s*remember\s+(that\s+)?",
        r"^\s*mark\s+(this|that|here)\s+(as\s+)?",
        r"^\s*prefer\s+",
        r"^\s*avoid\s+",
        r"^\s*记住[:：]?\s*",
        r"^\s*记得[:：]?\s*",
        r"^\s*标记[:：]?\s*",
        r"^\s*偏好[:：]?\s*",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
    return text or message.strip()


def _memory_name_from_message(
    message: str,
    *,
    verbs: tuple[str, ...] = (),
    default: str = "memory",
) -> str:
    text = message.strip()
    for verb in verbs:
        text = re.sub(rf"^\s*{re.escape(verb)}\s+", "", text, flags=re.IGNORECASE)
    text = _strip_memory_verbs(text)

    # Prefer explicit "as NAME" / "called NAME" markers for place names.
    explicit = re.search(r"\b(?:as|called|named)\s+([A-Za-z0-9_\- ]{2,40})", text, re.I)
    if explicit:
        text = explicit.group(1)

    words = re.findall(r"[A-Za-z0-9_]+", text.lower())
    if not words:
        return default
    stop = {
        "that", "this", "the", "a", "an", "is", "are", "at", "to", "here",
        "place", "location", "please", "me", "my", "as", "called", "named",
    }
    filtered = [w for w in words if w not in stop]
    return "_".join(filtered[:6]) or default


def _craft_target_from_message(message: str) -> str:
    lower = message.lower()
    match = re.search(r"\bcraft\s+(?:a|an|the|some)?\s*([a-zA-Z0-9_ -]+)", lower)
    if not match:
        return ""
    target = match.group(1).strip()
    target = re.split(r"\b(?:for|please|now|and|then)\b", target)[0].strip()
    words = [w for w in re.findall(r"[a-zA-Z0-9_]+", target) if w not in {"a", "an", "the"}]
    return "_".join(words[:3])


def _log_preview(value: Any, *, limit: int = 500) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _delivery_succeeded(result: Any) -> bool:
    if not _action_succeeded(result):
        return False
    match = _DELIVERY_RESULT_RE.search(str(result))
    if match is None:
        return True
    return int(match.group("count")) > 0


def _wood_family_from_log(block_name: str) -> str | None:
    name = block_name.strip().lower()
    if name.startswith("stripped_"):
        name = name.removeprefix("stripped_")
    if name.endswith("_log"):
        return name.removesuffix("_log")
    if name.endswith("_stem"):
        return name.removesuffix("_stem")
    return None


def _log_for_wood_family(family: str) -> str:
    if family in {"crimson", "warped"}:
        return f"{family}_stem"
    return f"{family}_log"


def _planks_for_wood_family(family: str) -> str:
    return f"{family}_planks"


def _is_explicit_oak_request(message: str) -> bool:
    text = (message or "").lower()
    return bool(re.search(r"\boak\b|橡木", text))


def _is_generic_wood_request(message: str) -> bool:
    text = (message or "").lower()
    if _is_explicit_oak_request(text):
        return False
    return bool(re.search(r"\b(wood|log|logs|plank|planks)\b|木头|木材|原木|木板", text))


def _is_explicit_charcoal_request(message: str) -> bool:
    text = (message or "").lower()
    return bool(re.search(r"\bcharcoal\b|木炭", text))


def _is_torch_or_light_request(message: str) -> bool:
    text = (message or "").lower()
    return bool(re.search(r"\b(torch|torches|light|lighting)\b|火把|照明|点亮", text))


def _is_seed_source_request(message: str) -> bool:
    text = (message or "").lower()
    return bool(re.search(
        r"\b(seed|seeds|wheat|plant|farm|sow|crop|breed\s+chicken|chicken)\b|"
        r"种子|小麦|种田|种地|播种|农场|耕|繁殖.*鸡|鸡.*繁殖",
        text,
    ))


def _memory_record_matches_task(record: MemoryRecord, message: str) -> bool:
    if not _is_navigation_only_request(message):
        return True
    haystack = " ".join((
        record.name,
        record.description or "",
        " ".join(record.tags or []),
        record.body[:500] if record.body else "",
    )).lower()
    if "active_task" not in haystack and not record.name.startswith("task_"):
        return True
    return bool(re.search(
        r"\b(come|follow|go\s+to|move|spawn|home|base|coordinate|location|place)\b|"
        r"跟|过来|重生点|坐标|位置|地点|基地|家",
        haystack,
    ))


def _inventory_log_count_from_state(state: BotState) -> int:
    return sum(item.count for item in state.inventory if _wood_family_from_log(item.name))


def _best_inventory_log_item(state: BotState) -> tuple[str, int] | None:
    logs = [
        (item.name, item.count)
        for item in state.inventory
        if item.count > 0 and _wood_family_from_log(item.name)
    ]
    if not logs:
        return None
    return max(logs, key=lambda item: item[1])


def _normalise_smelt_input(item_name: str) -> str:
    name = item_name.strip().lower()
    return _SMELT_INPUT_ALIASES.get(name, name)


def _fuel_smelt_capacity(item_name: str) -> float:
    name = item_name.strip().lower()
    if name in _COMMON_FUEL_SMELTS:
        return _COMMON_FUEL_SMELTS[name]
    if (
        name.endswith("_log")
        or name.endswith("_stem")
        or name.endswith("_hyphae")
        or name.endswith("_planks")
    ):
        return 1.5
    if name.endswith("_slab"):
        return 0.75
    if name.endswith("_sapling"):
        return 0.5
    if name.startswith("wooden_"):
        return 1.0
    return 0.0


def _inventory_has_smelting_fuel(state: BotState, needed_items: int) -> bool:
    for item in state.inventory:
        capacity = _fuel_smelt_capacity(item.name)
        if capacity <= 0:
            continue
        if item.count >= math.ceil(max(needed_items, 1) / capacity):
            return True
    return False


def _summary_contradicts_task_status(summary: str, status: str) -> bool:
    text = (summary or "").lower()
    if status == "succeeded":
        return bool(re.search(
            r"\b(failed|failure|unable|could not|couldn't|did not|didn't|missing|"
            r"cancelled|canceled|stuck)\b|失败|未能|没有完成|缺少|取消|卡住",
            text,
        ))
    if status == "failed":
        return bool(re.search(r"\b(succeeded|successfully completed)\b|成功完成", text))
    return False


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


def _player_requested_mute(message: str) -> bool:
    text = message.strip().lower()
    return bool(
        re.search(r"\b(stfu|shut up|be quiet|stop talking|mute)\b|闭嘴|安静|别说话", text)
    )


def _player_requested_stop(message: str) -> bool:
    text = message.strip().lower()
    if _player_requested_mute(text):
        return False
    return bool(re.search(r"\b(stop|cancel|halt|pause)\b|停下|停止|别动", text))


def _player_requested_come_here(message: str) -> bool:
    text = message.strip().lower()
    return bool(re.fullmatch(
        r"(come\s+to\s+me|come\s+here|get\s+over\s+here|come|"
        r"过来|来我这里|来我这|到我这里|到我这)",
        text,
    ))


def _player_requested_follow_me(message: str) -> bool:
    text = message.strip().lower()
    return bool(re.fullmatch(
        r"(follow\s+me|follow|escort\s+me|跟我来|跟着我|跟随我)",
        text,
    ))


def _player_requested_task_interrupt(message: str) -> bool:
    return (
        _player_requested_stop(message)
        or _player_requested_come_here(message)
        or _player_requested_follow_me(message)
    )


def _memory_record_is_failed_task(record: Any) -> bool:
    tags = {str(tag).lower() for tag in getattr(record, "tags", []) or []}
    if "task_failed" in tags:
        return True
    body = str(getattr(record, "body", "") or "").lower()
    description = str(getattr(record, "description", "") or "").lower()
    return "**status:** failed" in body or "status: failed" in body or "failed" in description


def _slugify_task_name(text: str, *, limit: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]+", "_", text.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return (slug or "task")[:limit]


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

    # Default config path for few-shot conversation examples (mirrors JS
    # profile.conversation_examples — fills the $EXAMPLES placeholder).
    DEFAULT_EXAMPLES_CONFIG = "config/conversation_examples.json"

    # How many examples to include per turn (matches JS settings.max_messages
    # → settings.num_examples default of 2).
    _EXAMPLES_PER_TURN = 2

    # Cap on action_call payloads we feed back to InputRouter / feedback handler.
    _RECENT_ACTIONS_WINDOW = 5

    def __init__(self, config: AgentConfig):
        self._config = config
        self.name = config.name

        # Phase 1 components (initialised in start())
        self._prompter: Prompter | None = None
        self._msg_builder: MessageBuilder | None = None
        self._router: InputRouter | None = None
        self._dispatch_gate: DispatchGate | None = None
        self._examples: ExampleStore | None = None

        # Phase 2 components
        self._executor = ActionExecutor()
        self._convo_mgr = ConversationManager()
        self._completion_engine = CompletionEngine()
        self._recovery_manager = RecoveryManager()
        self._task_manager = TaskManager()
        self._planner_loop = PlannerLoop(self)
        self._command_runner = CommandRunner(self._execute_agent_command)
        self._modes = ModeEvaluator()
        self._safety: SafetyMonitor | None = None

        # Runtime state
        self._bridge: Any = None
        self._history: list[ConversationTurn] = []
        self._last_sender: str | None = None
        self._last_message: str = ""
        self._shut_up: bool = False
        self._running: bool = False
        self._turn_id: int = 0
        # Track recent action_call results for feedback intent (Phase 4 hook)
        self._recent_actions: list[dict[str, Any]] = []

        # Error recovery — guided task resumption
        self._pending_task: PendingTask | None = None
        self._active_task: ActiveTask | None = None
        self._active_task_stack: list[ActiveTask] = self._task_manager.stack
        self._direct_shortcut_retry_reason: str | None = None
        self._guided_loop_task: asyncio.Task[None] | None = None
        self._self_prompt_goal: str | None = None
        self._self_prompt_task: asyncio.Task[None] | None = None
        self._self_prompt_running: bool = False

        # Memory layer (Phase 3/4a)
        self._memory: MemoryService | None = None
        self._knowledge: Any = None  # KnowledgeGraph (loaded lazily)
        self._skill_store: SkillStore | None = None
        self._skill_matcher: SkillMatcher | None = None

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

        # Phase 1: input intent classifier — regex/keyword with optional LLM fallback
        self._router = InputRouter.from_config(
            self.DEFAULT_INTENT_CONFIG, prompter=self._prompter,
        )
        self._dispatch_gate = DispatchGate(global_settings.dispatch_gate)
        if self._dispatch_gate.configured:
            logger.info(
                "DispatchGate enabled: model=%s base_url=%s",
                global_settings.dispatch_gate.model,
                global_settings.dispatch_gate.base_url,
            )
        else:
            logger.info("DispatchGate disabled; using keyword/direct routing fallback")

        # Phase 1: few-shot conversation examples for the $EXAMPLES slot.
        # Keyword-overlap retrieval until Phase 4b FAISS lands — same
        # mechanism JS uses (utils/examples.js falls back to word overlap
        # when no embedding model is configured).
        self._examples = ExampleStore.from_config(self.DEFAULT_EXAMPLES_CONFIG)
        self._examples.load()

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
        bot_dir = Path("bots") / self.name
        store = ScopedFileMemoryStore(bot_dir)
        self._memory = MemoryService(
            store,
            bot_name=self.name,
            base_dir=bot_dir,
        )
        await self._memory.start()

        # Phase 5a: executable workflow skills (separate from code-doc skills)
        self._skill_store = SkillStore.for_bot(self.name, memory=self._memory)
        await self._skill_store.load()
        self._skill_matcher = SkillMatcher(self._skill_store)

        # Phase 7a: static knowledge (load lazily if data present)
        k_data = Path("data/minecraft/1.21.6")
        if k_data.exists():
            from knowledge.knowledge_graph import KnowledgeGraph
            self._knowledge = KnowledgeGraph(k_data)
            await self._knowledge.load()
            logger.info("KnowledgeGraph loaded for %s", self.name)
        else:
            logger.info("No minecraft-data found — KnowledgeGraph disabled")

        if global_settings.wiki_rag.enabled:
            from knowledge import WikiVectorStore
            wiki_store = WikiVectorStore(
                index_dir=global_settings.wiki_rag.index_dir,
                embed_model=global_settings.wiki_rag.embed_model,
                rerank_model=global_settings.wiki_rag.rerank_model,
                use_fp16=global_settings.wiki_rag.use_fp16,
                device=global_settings.wiki_rag.device,
                dense_candidates=global_settings.wiki_rag.dense_candidates,
                rerank_candidates=global_settings.wiki_rag.rerank_candidates,
            )
            if wiki_store.load():
                self._executor.set_wiki_store(wiki_store)
                logger.info("Wiki RAG loaded from %s", global_settings.wiki_rag.index_dir)
            else:
                logger.info("Wiki RAG unavailable; searchWiki will use HTTP fallback")
        else:
            logger.info("Wiki RAG disabled by configuration")

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
        """Gather full bot state via sequential SocketBridge queries.

        Keep the same five data sources as the full observation path, but send
        them one by one to avoid overlapping socket-server handlers.
        """
        bridge = self._bridge
        if bridge is None:
            return BotState()

        try:
            base = await bridge.observe()
        except Exception as exc:
            logger.warning("observe() failed; stopping current agent loop: %r", exc)
            self._running = False
            return BotState()

        if isinstance(base, Exception) or not isinstance(base, dict):
            logger.warning("observe() failed; stopping current agent loop: %r", base)
            self._running = False
            return BotState()

        async def optional_component(name: str, call: Callable[[], Awaitable[Any]]) -> Any:
            try:
                return await call()
            except Exception as exc:
                logger.warning("observe() component failed: %s error=%r", name, exc)
                return exc

        inv_raw = await optional_component("get_inventory", bridge.get_inventory)
        ents_raw = await optional_component("get_entities", bridge.get_entities)
        blocks_raw = await optional_component("get_nearby_blocks", bridge.get_nearby_blocks)
        goal_raw = await optional_component("get_goal", bridge.get_goal)

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
        entity_counts: dict[str, int] = {}
        base_counts = base.get("entity_counts", {})
        if isinstance(base_counts, dict):
            for name, count in base_counts.items():
                try:
                    entity_counts[str(name)] = int(count)
                except (TypeError, ValueError):
                    continue
        should_derive_entity_counts = not entity_counts
        if isinstance(ents_raw, list):
            self_pos = Position(
                x=p.get("x", 0), y=p.get("y", 0), z=p.get("z", 0),
            )
            parsed_entities: list[EntityInfo] = []
            for e in ents_raw:
                if not isinstance(e, dict):
                    continue
                entity_name = str(e.get("name", "") or "")
                if should_derive_entity_counts and entity_name and entity_name != "player":
                    entity_counts[entity_name] = entity_counts.get(entity_name, 0) + 1
                epos_raw = e.get("position", {}) or {}
                epos = Position(
                    x=epos_raw.get("x", 0),
                    y=epos_raw.get("y", 0),
                    z=epos_raw.get("z", 0),
                )
                parsed_entities.append(EntityInfo(
                    name=entity_name,
                    entity_type=str(e.get("type", "") or ""),
                    position=epos,
                    distance=self_pos.distance_to(epos),
                    health=float(e.get("health", 0) or 0),
                ))
            parsed_entities.sort(key=lambda entity: entity.distance)
            entities = parsed_entities[:20]

        nearby_block_types: list[str] = []
        if isinstance(blocks_raw, list):
            nearby_block_types = [str(b) for b in blocks_raw]

        current_goal: str | None = None
        if isinstance(goal_raw, str):
            current_goal = goal_raw or None
        elif goal_raw is not None and not isinstance(goal_raw, Exception):
            current_goal = str(goal_raw)

        entities_nearby = base.get("entities_nearby", "")
        if entity_counts and "counts:" not in str(entities_nearby):
            count_text = ", ".join(
                f"{name} x{count}"
                for name, count in sorted(
                    entity_counts.items(),
                    key=lambda item: (-item[1], item[0]),
                )[:12]
            )
            entities_nearby = (
                f"counts: {count_text}\n{entities_nearby}"
                if entities_nearby else f"counts: {count_text}"
            )

        return BotState(
            health=base.get("health", 20),
            food=base.get("food", 20),
            position=Position(x=p.get("x", 0), y=p.get("y", 0), z=p.get("z", 0)),
            gamemode=base.get("gamemode", "survival"),
            time_of_day=base.get("time_of_day", ""),
            biome=base.get("biome", ""),
            entities_nearby=entities_nearby,
            blocks_nearby=base.get("blocks_nearby", ""),
            players_nearby=base.get("players_nearby", ""),
            inventory=inventory,
            entities=entities,
            entity_counts=entity_counts,
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

        logger.info(
            "Plan start: tool_choice=%s goal=%s pos=(%.1f, %.1f, %.1f) "
            "health=%.1f food=%.1f inventory_items=%d nearby_blocks=%d "
            "last_message=%r",
            tool_choice,
            bot_state.current_goal,
            bot_state.position.x,
            bot_state.position.y,
            bot_state.position.z,
            bot_state.health,
            bot_state.food,
            len(bot_state.inventory),
            len(bot_state.nearby_block_types),
            self._last_message,
        )
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
            hints = []
            if self._active_task:
                hints.append(self._planning_strategy_prompt(self._active_task))
            if self._active_task and self._active_task.requires_ctable:
                hints.append(
                    "This task likely requires a crafting table. "
                    "If none is placed nearby, use placeHere(\"crafting_table\") first."
                )
            if self._active_task and self._active_task.timeout_s is not None:
                elapsed = time.time() - self._active_task.started_at
                remaining = max(0, self._active_task.timeout_s - elapsed)
                hints.append(
                    f"Time remaining: {remaining:.0f}s. "
                    "Be efficient and avoid unnecessary steps."
                )
            self._msg_builder.self_prompt = " ".join(hints) if hints else ""

        # Phase 3/4a: recall relevant long-term memory
        memory = await self._recall_memory(message=self._last_message)
        active_context = self._active_task_prompt_context()
        if active_context:
            memory = f"{memory}\n\n{active_context}" if memory else active_context
        logger.info(
            "Plan memory context: chars=%d has_memory=%s",
            len(memory),
            bool(memory),
        )

        # Few-shot examples: keyword-overlap retrieval against the last
        # player message. Mirrors JS prompter.js which selects top-N
        # examples per turn from profile.conversation_examples.
        if self._examples is not None and self._examples.count > 0:
            selected = self._examples.select(
                self._last_message, limit=self._EXAMPLES_PER_TURN,
            )
            examples = MessageBuilder.format_examples(selected)
        else:
            examples = ""

        system_prompt = self._msg_builder.build_system_prompt(
            stats=stats,
            inventory=inventory,
            command_docs=command_docs,
            examples=examples,
            memory=memory,
        )

        prompt_history = self._history_for_active_task_prompt()
        messages = self._msg_builder.build_messages(
            history=prompt_history,
            system_prompt=system_prompt,
        )
        logger.info(
            "Plan prompt built: system_chars=%d messages=%d history_turns=%d "
            "command_docs_chars=%d examples_chars=%d",
            len(system_prompt),
            len(messages),
            len(prompt_history),
            len(command_docs),
            len(examples),
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

        logger.info(
            "Plan response: model=%s content_chars=%d tool_calls=%d finish=%s",
            response.model,
            len(response.content),
            len(response.tool_calls),
            response.finish_reason,
        )
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

    async def _run_agent_command(
        self,
        command_name: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> CommandRun:
        """Execute an agent command and return the structured runner result."""
        return await self._command_runner.run(command_name, args or [], kwargs)

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
        self._turn_id += 1
        turn_id = self._turn_id

        if self._active_task_stack_busy() and not _player_requested_task_interrupt(message):
            await self._reject_message_while_busy(source, message)
            return False

        self._last_message = message

        # Auto-unmute: !stfu suppresses chatty narration mid-task, not
        # permanently. Any new player message means they want interaction.
        if self._shut_up:
            self._shut_up = False
            logger.info("Auto-unmuted: new message from %s", source)

        # F2: reset recent-action ring at the start of each user turn so
        # task-specific context (e.g. five failed searchWiki calls from a
        # previous "craft a bed" attempt) does not bleed into the next
        # turn's feedback handler or plan loop. The L1 NDJSON event log
        # remains intact as the durable record.
        self._recent_actions.clear()
        self._direct_shortcut_retry_reason = None

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
        planning_strategy = PlanningStrategy.REACT
        if self._router is not None:
            decision = await self._router.classify_async(message)
            intent = decision.intent
            matched = decision.matched_pattern
            planning_strategy = decision.planning_strategy
        logger.info(
            "InputRouter: source=%s intent=%s planning_strategy=%s match=%r",
            source, intent.value, planning_strategy.value, matched,
        )

        # Record L1 player message event
        await self._record_event("player_msg", {
            "source": source, "message": message,
            "intent": intent.value,
            "planning_strategy": planning_strategy.value,
        })

        # Include the sender's name so the LLM can reference them in
        # commands like followPlayer / goToPlayer.
        self._history.append(
            ConversationTurn(role="user", content=f"{source}: {message}"),
        )
        self._last_sender = source
        if _player_requested_stop(message):
            await self._finish_active_task(
                "cancelled",
                reason=f"{source} asked me to stop.",
            )
        elif intent == InputIntent.ACTION_COMMAND:
            await self._replace_active_task(
                source,
                message,
                planning_strategy=planning_strategy,
            )

        if _player_requested_stop(message):
            direct_result = await self._try_direct_action_command()
            return bool(direct_result)

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
            max_responses=max_responses, use_tools=use_tools, turn_id=turn_id,
        )

    def _dispatch_allows_shortcut(
        self,
        decision: DispatchDecision | None,
        route: str,
    ) -> bool:
        """Return whether a direct shortcut may run for this route.

        No DispatchGate decision means the feature is disabled/unavailable, so
        the existing keyword/shortcut flow is preserved.  When the gate is
        configured, shortcuts are allowed only for confident atomic decisions;
        everything else naturally falls through to the full planner.
        """
        if not _shortcut_route_known(route):
            logger.info("DispatchGate skipped unknown shortcut route=%s", route)
            return False
        if decision is None:
            return True
        threshold = (
            self._dispatch_gate.direct_confidence
            if self._dispatch_gate is not None else 0.85
        )
        allowed = (
            decision.route == route
            and decision.task_shape == "atomic"
            and decision.confidence >= threshold
        )
        if not allowed:
            logger.info(
                "DispatchGate skipped shortcut route=%s decision=%s/%s confidence=%.2f",
                route,
                decision.route,
                decision.task_shape,
                decision.confidence,
            )
        return allowed

    def _direct_shortcut_block_reason(
        self,
        route: str,
        bot_state: BotState,
    ) -> str | None:
        """Return why a local shortcut should yield to the planner.

        Shortcuts stay narrow: stop/follow/come-to-me remain direct actions.
        Direct item is only for inventory-satisfied delivery. If the bot would
        need to search, mine, gather, craft, smelt, fight, or repair missing
        dependencies, the planner/recovery loop owns the task.
        """
        if route != "direct_item":
            return None

        request = _parse_item_request(self._last_message)
        if request is None:
            return None
        if _cook_request_is_compound(self._last_message):
            return "compound cook request"

        if _is_generic_wood_request(self._last_message) and (
            request.item_name == "oak_log"
            or request.source_block == "oak_log"
            or _wood_family_from_log(request.item_name) is not None
        ):
            have = _inventory_log_count_from_state(bot_state)
        else:
            have = max(
                _inventory_count_from_state(bot_state, item_name)
                for item_name in _acceptable_items_for_target(request.item_name)
            )

        if have >= request.quantity:
            return None
        return (
            f"needs multi-step acquisition for {request.item_name} "
            f"(have {have}, need {request.quantity})"
        )

    # ── Plan loop (action / question / chitchat) ────────────────────

    async def _run_plan_loop(
        self, *, max_responses: int, use_tools: bool, turn_id: int | None = None,
    ) -> bool:
        """Run the planner loop through the PlannerLoop facade."""
        return await self._planner_loop.run(
            max_responses=max_responses,
            use_tools=use_tools,
            turn_id=turn_id,
        )

    async def _run_plan_loop_impl(
        self, *, max_responses: int, use_tools: bool, turn_id: int | None = None,
    ) -> bool:
        """Compatibility wrapper for callers that still target the old loop hook."""
        return await self._planner_loop.run(
            max_responses=max_responses,
            use_tools=use_tools,
            turn_id=turn_id,
        )

    async def _maybe_finish_after_command_completion(
        self,
        command_name: str,
        result: Any,
        bot_state: BotState,
        *,
        effects_already_recorded: bool = False,
    ) -> bool:
        """Finish non-delivery tasks when a terminal command has evidence."""
        task = self._active_task
        if task is None or task.status != "running":
            return False
        if not _action_succeeded(result):
            return False
        action_result = ActionResult.from_legacy(command_name, result)
        if not effects_already_recorded:
            self._active_task_add_effects(command_name, action_result)
        self._active_task_add_snapshot(bot_state, note=f"after {command_name}")

        kind = _command_completion_kind(command_name)
        evidence = self._completion_evidence_for_command(
            command_name,
            kind,
            result,
            bot_state,
        )
        if evidence is None:
            return False

        expects_delivery = self._active_task_expects_delivery(task)
        if expects_delivery and await self._maybe_deliver_satisfied_task(task, bot_state):
            return True
        if expects_delivery:
            return False

        decision = self._completion_engine.should_finish_after_action(
            task,
            action_result,
            bot_state,
        )
        if task.completion_spec is not None and task.completion_spec.kind != "unknown":
            if not decision.should_finish:
                logger.info(
                    "Command completion treated as partial progress: "
                    "task=%s command=%s kind=%s progress=%d/%d evidence=%s",
                    task.id,
                    command_name,
                    kind,
                    decision.progress.current,
                    decision.progress.required,
                    decision.progress.evidence,
                )
                return False
        elif kind not in _TERMINAL_COMPLETION_KINDS and kind != "inventory":
            return False

        if (
            task.completion_spec is None
            or task.completion_spec.kind == "unknown"
        ) and kind == "world_change" and not _world_change_command_can_complete(
            command_name,
            task.original_message,
        ):
            logger.info(
                "World-change command treated as intermediate progress: "
                "task=%s command=%s request=%r",
                task.id,
                command_name,
                task.original_message,
            )
            return False

        self._active_task_add_event(
            "completion_evidence",
            f"{command_name} completion_kind={kind}: {evidence}",
            ok=True,
        )
        logger.info(
            "Command completion evidence: task=%s command=%s kind=%s evidence=%s",
            task.id,
            command_name,
            kind,
            evidence,
        )

        if kind == "delivery":
            await self._finish_active_task(
                "succeeded",
                reason=f"Delivery completed via {command_name}.",
            )
            return True

        if kind in _TERMINAL_COMPLETION_KINDS:
            await self._finish_active_task(
                "succeeded",
                reason=f"Verified non-delivery task completion via {command_name}: {evidence}",
            )
            return True

        if kind == "inventory":
            await self._finish_active_task(
                "succeeded",
                reason=f"Inventory task completed via {command_name}: {evidence}",
            )
            return True

        return False

    async def _maybe_deliver_satisfied_task(self, task: ActiveTask, bot_state: BotState) -> bool:
        spec = task.completion_spec
        if spec is None or not spec.requires_delivery or not spec.acceptable_items:
            return False
        requested = spec.quantity or 1
        delta = _inventory_delta_from_baseline(task, bot_state, spec.acceptable_items)
        have_cooked = (
            spec.kind == "cook_item"
            and _inventory_count_any_from_state(bot_state, spec.acceptable_items) >= requested
        )
        if delta < requested and not have_cooked:
            return False
        item_name = next(
            (
                item.name
                for item in bot_state.inventory
                if item.name in spec.acceptable_items and item.count >= requested
            ),
            spec.target_items[0] if spec.target_items else spec.acceptable_items[0],
        )
        logger.info(
            "Completion inventory satisfied; delivering before finishing: task=%s item=%s qty=%d",
            task.id,
            item_name,
            requested,
        )
        delivered = await self._deliver_item_to_player(
            item_name,
            requested,
            reason="completion_delivery",
        )
        await self._finish_active_task(
            "succeeded" if delivered else "failed",
            reason=(
                "Delivered requested items after completion evidence."
                if delivered else "Could not deliver requested items after completion evidence."
            ),
        )
        return True

    def _completion_evidence_for_command(
        self,
        command_name: str,
        completion_kind: str,
        result: Any,
        bot_state: BotState,
    ) -> str | None:
        """Return a short evidence string when a command can close a task."""
        text = str(result or "").strip()
        if not text:
            return None
        first_line = text.splitlines()[0][:180]

        if completion_kind == "delivery":
            return first_line if _delivery_succeeded(result) else None

        if completion_kind == "custom_action":
            lower = text.lower()
            if "dynamic action ok" in lower and "failed:" not in lower:
                return first_line
            return None

        if completion_kind == "inventory":
            lower = text.lower()
            if "inventory: no inventory change" in lower:
                return None
            if "inventory:" in lower or re.search(
                r"\b(collected|crafted|smelted|picked up)\b",
                lower,
            ):
                return first_line
            return None

        if completion_kind == "navigation":
            lower = text.lower()
            if re.search(
                r"\b(arrived|reached|following|followed|moved|went to|found)\b|"
                r"goal reached",
                lower,
            ):
                return first_line
            return None

        if completion_kind != "world_change":
            return None

        lower = text.lower()
        positive_markers = (
            "bred ",
            "broke ",
            "placed ",
            "activated ",
            "used ",
            "tilled",
            "sowed",
            "planted",
            "cleared",
            "traded",
            "attacked",
            "killed",
            "slept",
            "dug",
            "opened",
            "closed",
            "dynamic action ok",
        )
        if any(marker in lower for marker in positive_markers):
            return first_line
        # A few bridge actions report success mostly through a position or
        # inventory summary. Treat that as evidence only for commands whose
        # purpose is intrinsically a world mutation.
        if _world_change_command_has_intrinsic_effect(command_name):
            return first_line
        return None

    async def _maybe_complete_satisfied_craft(self, bot_state: BotState) -> bool:
        """Deliver craft requests once current inventory satisfies them."""
        request = _parse_craft_request(self._last_message)
        if request is None or request.additive:
            return False

        have = _inventory_count_from_state(bot_state, request.item_name)
        if have < request.quantity:
            return False

        message = (
            f"I already have {have} {request.item_name}; "
            f"that covers the requested {request.quantity}."
        )
        logger.info(
            "Craft request already satisfied: item=%s have=%d requested=%d",
            request.item_name,
            have,
            request.quantity,
        )
        self._history.append(ConversationTurn(
            role="system",
            content=(
                f"Task already satisfied: inventory has {have} {request.item_name}, "
                f"player requested {request.quantity}. Deliver the requested items "
                "to the player now; do not call craftRecipe or stop."
            ),
        ))
        self._active_task_add_event(
            "inventory_check",
            f"Already have {have} {request.item_name}; requested {request.quantity}.",
            ok=True,
        )
        await self._send_chat(message)
        delivered = await self._deliver_item_to_player(
            request.item_name,
            request.quantity,
            reason="craft_request_complete",
        )
        await self._finish_active_task(
            "succeeded" if delivered else "failed",
            reason=(
                "Delivered crafted/requested items to player."
                if delivered else "Could not deliver crafted/requested items to player."
            ),
        )
        return True

    async def _deliver_item_to_player(self, item_name: str, quantity: int, *, reason: str) -> bool:
        """Go to the requester and hand over the requested item quantity."""
        if not self._last_sender:
            return False
        await self._send_chat(f"Bringing {quantity} {item_name} to {self._last_sender}.")
        final_ok = True
        for command, kwargs in [
            ("goToPlayer", {"player_name": self._last_sender, "closeness": 3}),
            (
                "givePlayer",
                {
                    "player_name": self._last_sender,
                    "item_name": item_name,
                    "num": quantity,
                    "_delivery_prepositioned": True,
                },
            ),
        ]:
            command_run = await self._run_agent_command(command, [], kwargs)
            result = command_run.raw
            action_result = command_run.result
            self._record_action(command, [], result)
            ok = (
                _delivery_succeeded(result)
                if command == "givePlayer" else action_result.ok
            )
            self._active_task_add_event(
                "action_result",
                f"{command} kwargs={kwargs} -> {result}",
                ok=ok,
            )
            self._active_task_add_effects(command, action_result)
            await self._record_event("action_result", {
                "command": command,
                "args": [],
                "kwargs": kwargs,
                "result": result,
                "summary": action_result.summary,
                "direct": reason,
            })
            await self._send_chat(action_result.summary)
            if not ok:
                final_ok = False
                break
        return final_ok

    async def _try_direct_action_command(self) -> bool | None:
        """Handle simple player commands without spending an LLM turn."""
        if not self._last_sender:
            return None

        text = self._last_message.strip().lower()
        command: str | None = None
        kwargs: dict[str, Any] = {}
        preface = ""

        if _player_requested_come_here(text):
            command = "goToPlayer"
            kwargs = {"player_name": self._last_sender, "closeness": 3}
            preface = "On my way."
        elif _player_requested_follow_me(text):
            command = "followPlayer"
            kwargs = {"player_name": self._last_sender, "follow_dist": 4}
            preface = "I'll follow you."
        elif _player_requested_stop(text):
            command = "stop"
            preface = "Stopping."

        if command is None:
            return None

        logger.info(
            "Direct action command matched: message=%r command=%s kwargs=%s",
            self._last_message,
            command,
            kwargs,
        )
        # Send a SINGLE chat line before executing. Most Minecraft servers
        # rate-limit chat to ~1.5 s/message, so sending 3 chats in 10 ms
        # (preface + action_desc + result) gets the middle messages dropped
        # by the server. The preface is the only player-facing one we need
        # for direct actions — the executor logs already record the call.
        if preface:
            await self._send_chat(preface)
        command_run = await self._run_agent_command(command, [], kwargs)
        result = command_run.raw
        self._record_action(command, [], result)
        ok = command_run.result.ok
        self._active_task_add_event(
            "action_result",
            f"{command} kwargs={kwargs} -> {result}",
            ok=ok,
        )
        await self._record_event("action_result", {
            "command": command,
            "args": [],
            "kwargs": kwargs,
            "result": result,
            "direct": True,
        })
        # Only chat back the result for one-shot commands. Long-running
        # commands like followPlayer / goToPlayer keep going on the bridge
        # side after returning, and a "Done: ..." line would be wrong.
        if command not in {"followPlayer", "goToPlayer"}:
            await self._send_chat(f"Done: {result}")
        await self._finish_active_task(
            "succeeded" if ok else "failed",
            reason=str(result or ""),
        )
        return True

    def _active_task_stack_busy(self) -> bool:
        return any(task.status == "running" for task in self._active_task_stack)

    async def _reject_message_while_busy(self, source: str, message: str) -> None:
        task = self._active_task or self._task_manager.active
        task_label = task.original_message if task is not None else "current task"
        logger.info(
            "Rejected new command while task stack is busy: source=%s message=%r active=%s",
            source,
            message,
            task.id if task is not None else None,
        )
        await self._record_event("task_rejected_while_busy", {
            "source": source,
            "message": message,
            "active_task_id": task.id if task is not None else None,
            "active_task": task_label,
            "stack_depth": len(self._active_task_stack),
        })
        await self._send_chat(
            "I'm still working on the current task: "
            f"{task_label}. Say stop, come here, or follow me to interrupt. "
            "I'll tell you when I can accept a new command."
        )

    def _queue_direct_shortcut_llm_retry(self, reason: str) -> None:
        """Let the normal planner recover once after a direct shortcut fails."""
        if self._direct_shortcut_retry_reason:
            return
        self._direct_shortcut_retry_reason = reason
        logger.info("Direct shortcut fallback queued: %s", reason)
        self._active_task_add_event("direct_fallback", reason, ok=False)
        self._history.append(ConversationTurn(
            role="system",
            content=(
                "A direct shortcut could not complete the current request: "
                f"{reason} You may make exactly one command attempt to recover. "
                "Stay inside the current player request only; do not resume old "
                "memory tasks or unrelated goals. If recovery is impossible, say "
                "what is missing instead of trying unrelated work."
            ),
        ))

    async def _try_direct_cook_request(self, bot_state: BotState) -> bool | None:
        """Cook food with a furnace, then deliver the cooked result to requester."""
        if not self._last_sender:
            return None
        request = _parse_cook_request(self._last_message)
        if request is None:
            return None
        if _cook_request_is_compound(self._last_message):
            logger.info(
                "Direct cook skipped for compound request: message=%r",
                self._last_message,
            )
            self._active_task_add_event(
                "direct_skip",
                "Skipped direct cook because the request includes gathering/combat before cooking.",
                ok=None,
            )
            return None

        raw_have = _inventory_count_from_state(bot_state, request.raw_item)
        cooked_have = _inventory_count_from_state(bot_state, request.cooked_item)
        requested_qty = request.quantity or max(raw_have + cooked_have, cooked_have, 1)
        remaining_to_cook = max(0, requested_qty - cooked_have)

        logger.info(
            "Direct cook request matched: message=%r raw=%s cooked=%s raw_have=%d "
            "cooked_have=%d qty=%d",
            self._last_message,
            request.raw_item,
            request.cooked_item,
            raw_have,
            cooked_have,
            requested_qty,
        )
        self._active_task_add_event(
            "inventory_check",
            (
                f"Have {raw_have} {request.raw_item} and {cooked_have} "
                f"{request.cooked_item}; requested {requested_qty} cooked."
            ),
            ok=(raw_have + cooked_have) >= requested_qty,
        )

        if remaining_to_cook > raw_have:
            await self._send_chat(
                f"I only have enough raw {request.raw_item} for "
                f"{raw_have + cooked_have} {request.cooked_item}. I'll try one fallback plan."
            )
            self._queue_direct_shortcut_llm_retry(
                f"Direct cook had only {raw_have} {request.raw_item} and "
                f"{cooked_have} {request.cooked_item}, but needs {requested_qty} cooked."
            )
            return None

        await self._send_chat(
            f"I'll cook {remaining_to_cook} {request.raw_item} and bring you "
            f"{requested_qty} {request.cooked_item}."
        )

        if remaining_to_cook > 0:
            furnace_ready = await self._ensure_furnace_for_cooking(bot_state)
            if not furnace_ready:
                self._queue_direct_shortcut_llm_retry(
                    "Direct cook could not find, place, or craft a furnace."
                )
                return None

            smelt_run = await self._run_agent_command(
                "smeltItem",
                [],
                {"item_name": request.raw_item, "num": remaining_to_cook},
            )
            smelt_result = smelt_run.raw
            self._record_action("smeltItem", [], smelt_result)
            smelt_ok = smelt_run.result.ok
            self._active_task_add_event(
                "action_result",
                (
                    f"smeltItem kwargs={{'item_name': {request.raw_item!r}, "
                    f"'num': {remaining_to_cook}}} -> {smelt_result}"
                ),
                ok=smelt_ok,
            )
            await self._record_event("action_result", {
                "command": "smeltItem",
                "args": [],
                "kwargs": {"item_name": request.raw_item, "num": remaining_to_cook},
                "result": smelt_result,
                "direct": "cook_request",
            })
            await self._send_chat(f"Done: {smelt_result}")
            if not smelt_ok:
                self._queue_direct_shortcut_llm_retry(
                    f"Direct cook smeltItem failed: {smelt_result or 'no response'}"
                )
                return None

        delivered = await self._deliver_item_to_player(
            request.cooked_item,
            requested_qty,
            reason="cook_request_complete",
        )
        await self._finish_active_task(
            "succeeded" if delivered else "failed",
            reason=(
                "Delivered cooked food to player."
                if delivered else "Could not deliver cooked food to player."
            ),
        )
        return True

    async def _maybe_retry_cook_after_furnace_clear(self) -> bool:
        """After a successful clearFurnace in a cook task, retry the original smelt once."""
        task = self._active_task
        if task is None or task.status != "running":
            return False
        if _parse_cook_request(task.original_message) is None:
            return False

        events = task.events or []
        last_clear_index: int | None = None
        for index, event in enumerate(events):
            if (
                event.kind == "action_result"
                and event.ok is not False
                and ("clearFurnace" in event.message or "clear_furnace" in event.message)
            ):
                last_clear_index = index
        if last_clear_index is None:
            return False

        later_events = events[last_clear_index + 1:]
        if any(
            event.kind == "action_result"
            and "smeltItem" in event.message
            and event.ok is not False
            for event in later_events
        ):
            return False

        logger.info(
            "Retrying cook task after successful furnace clear: task=%s",
            task.id,
        )
        self._active_task_add_event(
            "recovery",
            "Furnace was cleared; retrying the original cook request before finishing.",
            ok=None,
        )
        state = await self.observe()
        if not self._running:
            return False
        result = await self._try_direct_cook_request(state)
        return bool(result)

    async def _ensure_furnace_for_cooking(self, bot_state: BotState) -> bool:
        """Prefer an existing nearby furnace; only create one if none is found."""
        search_run = await self._run_agent_command(
            "searchForBlock",
            [],
            {"block_type": "furnace", "search_range": 64},
        )
        search_result = search_run.raw
        self._record_action("searchForBlock", [], search_result)
        search_ok = search_run.result.ok
        self._active_task_add_event(
            "action_result",
            "searchForBlock kwargs={'block_type': 'furnace', 'search_range': 64} "
            f"-> {search_result}",
            ok=search_ok,
        )
        await self._record_event("action_result", {
            "command": "searchForBlock",
            "args": [],
            "kwargs": {"block_type": "furnace", "search_range": 64},
            "result": search_result,
            "direct": "cook_request",
        })
        if search_ok:
            return True

        if _inventory_count_from_state(bot_state, "furnace") <= 0:
            if _inventory_count_from_state(bot_state, "cobblestone") < 8:
                return False
            craft_run = await self._run_agent_command(
                "craftRecipe",
                [],
                {"recipe_name": "furnace", "num": 1},
            )
            craft_result = craft_run.raw
            self._record_action("craftRecipe", [], craft_result)
            craft_ok = craft_run.result.ok
            self._active_task_add_event(
                "action_result",
                "craftRecipe kwargs={'recipe_name': 'furnace', 'num': 1} "
                f"-> {craft_result}",
                ok=craft_ok,
            )
            await self._record_event("action_result", {
                "command": "craftRecipe",
                "args": [],
                "kwargs": {"recipe_name": "furnace", "num": 1},
                "result": craft_result,
                "direct": "cook_request",
            })
            await self._send_chat(f"Done: {craft_result}")
            if not craft_ok:
                return False

        place_run = await self._run_agent_command(
            "placeHere",
            [],
            {"block_type": "furnace"},
        )
        place_result = place_run.raw
        self._record_action("placeHere", [], place_result)
        place_ok = place_run.result.ok
        self._active_task_add_event(
            "action_result",
            "placeHere kwargs={'block_type': 'furnace'} -> " f"{place_result}",
            ok=place_ok,
        )
        await self._record_event("action_result", {
            "command": "placeHere",
            "args": [],
            "kwargs": {"block_type": "furnace"},
            "result": place_result,
            "direct": "cook_request",
        })
        await self._send_chat(f"Done: {place_result}")
        return place_ok

    async def _try_direct_item_request(self, bot_state: BotState) -> bool | None:
        """Bounded get/find/give item workflow with default quantity 8."""
        if not self._last_sender:
            return None
        if _cook_request_is_compound(self._last_message):
            logger.info(
                "Direct item skipped for compound cook request: message=%r",
                self._last_message,
            )
            self._active_task_add_event(
                "direct_skip",
                "Skipped direct item delivery because cooking is part of this request.",
                ok=None,
            )
            return None
        request = _parse_item_request(self._last_message)
        if request is None:
            return None

        logger.info(
            "Direct item request matched: message=%r item=%s source=%s qty=%d",
            self._last_message,
            request.item_name,
            request.source_block,
            request.quantity,
        )
        generic_wood_request = _is_generic_wood_request(self._last_message) and (
            request.item_name == "oak_log"
            or request.source_block == "oak_log"
            or _wood_family_from_log(request.item_name) is not None
        )
        effective_source_block = request.source_block
        if generic_wood_request:
            best_initial_log = _best_inventory_log_item(bot_state)
            if best_initial_log is not None and best_initial_log[1] >= request.quantity:
                effective_source_block = best_initial_log[0]
            else:
                nearby_log = self._preferred_nearby_log_block(bot_state)
                if nearby_log is not None:
                    effective_source_block = nearby_log
        await self._send_chat(
            f"I'll get {request.quantity} {request.item_name} and bring it to you."
        )

        have = (
            _inventory_log_count_from_state(bot_state)
            if generic_wood_request
            else _inventory_count_from_state(bot_state, request.item_name)
        )
        self._active_task_add_event(
            "inventory_check",
            f"Have {have} {request.item_name}; need {request.quantity}.",
            ok=have >= request.quantity,
        )
        if have < request.quantity:
            missing = request.quantity - have
            if effective_source_block is None:
                await self._send_chat(
                    f"I don't know a direct block source for {request.item_name} yet."
                )
                await self._finish_active_task(
                    "failed",
                    reason=f"No direct source known for {request.item_name}.",
                )
                return True

            for command, kwargs in [
                *self._gather_steps_for_item_request(effective_source_block, missing),
            ]:
                command_run = await self._run_agent_command(command, [], kwargs)
                result = command_run.raw
                self._record_action(command, [], result)
                ok = command_run.result.ok
                self._active_task_add_event(
                    "action_result",
                    f"{command} kwargs={kwargs} -> {result}",
                    ok=ok,
                )
                await self._record_event("action_result", {
                    "command": command,
                    "args": [],
                    "kwargs": kwargs,
                    "result": result,
                    "direct": "item_request",
                })
                await self._send_chat(f"Done: {result}")
                if not ok:
                    recovered = await self._recover_missing_dependency(
                        command,
                        [],
                        kwargs,
                        result,
                    )
                    if recovered:
                        if self._active_task is None or not self._running:
                            return True
                        continue
                    await self._finish_active_task("failed", reason=str(result or ""))
                    return True

        delivery_item_name = request.item_name
        delivery_quantity = request.quantity
        latest_state = await self.observe()
        if not self._running:
            return True
        if generic_wood_request:
            best_log = _best_inventory_log_item(latest_state)
            if best_log is not None:
                delivery_item_name = best_log[0]
                delivery_quantity = min(request.quantity, best_log[1])
            elif (
                effective_source_block is not None
                and _wood_family_from_log(effective_source_block)
            ):
                delivery_item_name = effective_source_block
        else:
            candidate = self._delivery_candidate_from_state(latest_state)
            if candidate is not None:
                delivery_item_name, delivery_quantity = candidate

        final_ok = True
        for command, kwargs in [
            ("goToPlayer", {"player_name": self._last_sender, "closeness": 3}),
            (
                "givePlayer",
                {
                    "player_name": self._last_sender,
                    "item_name": delivery_item_name,
                    "num": delivery_quantity,
                    "_delivery_prepositioned": True,
                },
            ),
        ]:
            command_run = await self._run_agent_command(command, [], kwargs)
            result = command_run.raw
            self._record_action(command, [], result)
            ok = (
                _delivery_succeeded(result)
                if command == "givePlayer" else command_run.result.ok
            )
            self._active_task_add_event(
                "action_result",
                f"{command} kwargs={kwargs} -> {result}",
                ok=ok,
            )
            await self._record_event("action_result", {
                "command": command,
                "args": [],
                "kwargs": kwargs,
                "result": result,
                "direct": "item_request",
            })
            await self._send_chat(f"Done: {result}")
            if not ok:
                final_ok = False
                break
        await self._finish_active_task(
            "succeeded" if final_ok else "failed",
            reason="Direct item request completed." if final_ok else "Direct item request failed.",
        )
        return True

    @staticmethod
    def _gather_steps_for_item_request(
        source: str,
        missing: int,
    ) -> list[tuple[str, dict[str, Any]]]:
        animal_sources = {"pig", "cow", "sheep", "chicken"}
        if source in animal_sources:
            return [
                ("searchForEntity", {"entity_type": source, "search_range": 64}),
                ("attack", {"entity_type": source, "range": 64}),
                ("pickupNearbyItems", {"distance": 8}),
            ]
        return [
            ("searchForBlock", {"block_type": source, "search_range": 64}),
            ("collectBlocks", {"block_type": source, "count": missing}),
        ]

    async def _recover_nearby_miss(
        self,
        command_name: str,
        args: list[Any],
        kwargs: dict[str, Any] | None,
        result: str,
        seen: set[tuple[str, str]],
    ) -> bool:
        """Return to the player, then search around them after a local miss."""
        if not self._last_sender:
            return False
        cause = _classify_failure(result)
        if cause is None or cause.kind != "nearby_miss":
            return False

        target = self._target_from_failed_search(command_name, args, kwargs)
        if not target:
            target = cause.target or ""
        if not target:
            return False
        key = (command_name, target)
        if key in seen:
            return False
        seen.add(key)

        state = await self.observe()
        if not self._running:
            return False
        plan = self._recovery_manager.plan(
            command_name=command_name,
            cause=cause,
            state=state,
            requester=self._last_sender,
            target_hint=target,
        )
        if plan is None:
            return False

        logger.info(
            "Nearby miss recovery: command=%s target=%s player=%s steps=%s",
            command_name,
            target,
            self._last_sender,
            [step.command for step in plan.steps],
        )
        self._history.append(ConversationTurn(
            role="system",
            content=(
                f"{command_name} could not find {target} nearby. Move back to "
                f"{self._last_sender} first, then search for {target} in a "
                "64 block radius around the player. Do not retry the same "
                "failed command at the old location."
            ),
        ))

        for step in plan.steps:
            recover_command = step.command
            recover_args = list(step.args)
            recover_kwargs = step.kwargs_dict()
            action_desc = _describe_action(
                recover_command,
                recover_args if recover_args else list(recover_kwargs.values()),
            )
            await self._send_chat(action_desc)
            command_run = await self._run_agent_command(
                recover_command,
                recover_args,
                recover_kwargs,
            )
            recover_result = command_run.raw
            action_result = command_run.result
            self._record_action(recover_command, recover_args, recover_result)
            recover_ok = action_result.ok
            self._active_task_add_event(
                "action_result",
                f"{recover_command} kwargs={recover_kwargs} -> {recover_result}",
                ok=recover_ok,
            )
            self._active_task_add_effects(recover_command, action_result)
            await self._record_event("action_result", {
                "command": recover_command,
                "args": recover_args,
                "kwargs": recover_kwargs,
                "result": recover_result,
                "summary": action_result.summary,
                "recovery": "nearby_miss_return_to_player",
            })
            await self._send_chat(action_result.summary)
            self._history.append(ConversationTurn(
                role="system",
                content=action_result.history_content,
            ))
            if not recover_ok:
                break
        return True

    async def _push_dependency_subgoal(
        self,
        parent: ActiveTask,
        plan: Any,
        state: BotState,
        *,
        retry_command: str,
        retry_args: list[Any],
        retry_kwargs: dict[str, Any],
    ) -> ActiveTask:
        message = plan.subgoal_message or f"Resolve dependency: {plan.reason}"
        resume_command = retry_command if plan.retry_original else None
        subgoal = self._task_manager.push_subgoal(
            requester=parent.requester,
            message=message,
            baseline_state=state,
            history_len=len(self._history),
            planning_strategy=parent.planning_strategy,
            parent_id=parent.id,
            reason=plan.reason,
            resume_command=resume_command,
            resume_args=retry_args if plan.retry_original else [],
            resume_kwargs=retry_kwargs if plan.retry_original else {},
        )
        self._active_task_stack = self._task_manager.stack
        self._active_task = subgoal
        parent.events.append(ActiveTaskEvent(
            kind="subgoal_start",
            message=f"{subgoal.id}: {message}; reason={plan.reason}",
            ok=None,
        ))
        logger.info(
            "ActiveTask subgoal start: parent=%s subgoal=%s kind=%s reason=%s resume=%s",
            parent.id,
            subgoal.id,
            getattr(plan, "subgoal_kind", "dependency"),
            plan.reason,
            resume_command,
        )
        await self._record_event("subgoal_start", {
            "parent_task_id": parent.id,
            "task_id": subgoal.id,
            "message": message,
            "reason": plan.reason,
            "subgoal_kind": getattr(plan, "subgoal_kind", "dependency"),
            "resume_command": resume_command,
            "stack_depth": len(self._active_task_stack),
        })
        self._history.append(ConversationTurn(
            role="system",
            content=(
                f"Internal dependency subgoal started: {message}. "
                f"Parent task remains: {parent.original_message}. "
                "Finish this subgoal, then resume the parent command."
            ),
        ))
        return subgoal

    async def _pop_dependency_subgoal(
        self,
        status: str,
        *,
        reason: str = "",
    ) -> ActiveTask | None:
        subgoal = self._active_task
        if subgoal is None or subgoal.task_type != "subgoal":
            return self._active_task
        if reason:
            subgoal.events.append(ActiveTaskEvent(kind="finish_reason", message=reason))
        summary = self._deterministic_task_summary(subgoal)
        popped = self._task_manager.pop_active(status=status, summary=summary)
        parent = self._task_manager.active
        self._active_task_stack = self._task_manager.stack
        self._active_task = parent
        if parent is not None and popped is not None:
            parent.events.append(ActiveTaskEvent(
                kind="subgoal_finish",
                message=f"{popped.id} status={status}: {summary[:220]}",
                ok=status == "succeeded",
            ))
        logger.info(
            "ActiveTask subgoal finish: subgoal=%s status=%s parent=%s summary=%s",
            popped.id if popped is not None else None,
            status,
            parent.id if parent is not None else None,
            _log_preview(summary),
        )
        await self._record_event("subgoal_finish", {
            "task_id": popped.id if popped is not None else None,
            "parent_task_id": parent.id if parent is not None else None,
            "status": status,
            "summary": summary,
            "stack_depth": len(self._active_task_stack),
        })
        self._history.append(ConversationTurn(
            role="system",
            content=(
                f"Internal dependency subgoal {status}: {summary}. "
                "Resume the parent task now."
            ),
        ))
        return parent

    async def _recover_missing_dependency(
        self,
        command_name: str,
        args: list[Any],
        kwargs: dict[str, Any] | None,
        result: Any,
    ) -> bool:
        """Recover from explicit dependency failures before failing the task."""
        text = str(result or "")
        task = self._active_task
        if task is None:
            return False
        if task.recovery_attempts >= _MAX_RECOVERY_ATTEMPTS_PER_TASK:
            logger.info(
                "Recovery budget exhausted: task=%s attempts=%d result=%s",
                task.id,
                task.recovery_attempts,
                _log_preview(text),
            )
            return False

        retry_command = command_name
        retry_args = list(args)
        retry_kwargs = dict(kwargs or {})
        cause = _classify_failure(result)
        if cause is None:
            return False
        if cause.kind == "nearby_miss":
            return False
        state = await self.observe()
        if not self._running:
            return False
        plan = self._recovery_manager.plan(
            command_name=command_name,
            cause=cause,
            state=state,
            requester=self._last_sender,
            target_hint=self._target_from_failed_search(command_name, args, kwargs),
        )
        if plan is None:
            return False

        parent_task = task
        parent_task.recovery_attempts += 1
        logger.info(
            "Recovering command failure: task=%s attempt=%d command=%s cause=%s steps=%s retry=%s",
            parent_task.id,
            parent_task.recovery_attempts,
            command_name,
            plan.reason,
            [step.command for step in plan.steps],
            plan.retry_original,
        )
        if plan.steps or plan.retry_original:
            recovery_instruction = (
                f"The previous command failed because {plan.reason}. "
                f"Run the recovery plan, then retry {command_name} if needed."
            )
            recovery_event = f"{plan.reason}; executing recovery plan."
        else:
            recovery_instruction = (
                f"The previous command failed because {plan.reason}. "
                "Do not retry the same command with the same arguments. "
                "Use the next observation to choose a different route, target, or approach."
            )
            recovery_event = f"{plan.reason}; returning to planner for a fresh approach."
        self._active_task_add_event(
            "recovery",
            recovery_event,
        )
        self._history.append(ConversationTurn(
            role="system",
            content=recovery_instruction,
        ))

        await self._push_dependency_subgoal(
            parent_task,
            plan,
            state,
            retry_command=retry_command,
            retry_args=retry_args,
            retry_kwargs=retry_kwargs,
        )

        for step in plan.steps:
            recover_result = await self._run_recovery_action(
                step.command,
                list(step.args),
                step.kwargs_dict(),
                recovery_kind="missing_dependency",
            )
            if not _action_succeeded(recover_result):
                await self._pop_dependency_subgoal(
                    "failed",
                    reason=f"Recovery step {step.command} failed: {recover_result}",
                )
                return False

        await self._pop_dependency_subgoal("succeeded", reason=plan.reason)
        if self._active_task is None:
            return True

        retry_result = result
        if plan.retry_original:
            retry_result = await self._run_recovery_action(
                retry_command,
                retry_args,
                retry_kwargs,
                recovery_kind="retry_after_missing_dependency",
            )
            if not _action_succeeded(retry_result):
                return False

        retry_state = await self.observe()
        if not self._running:
            return True
        await self._maybe_finish_after_command_completion(
            command_name,
            retry_result,
            retry_state,
        )
        return True

    async def _run_recovery_action(
        self,
        command: str,
        args: list[Any],
        kwargs: dict[str, Any],
        *,
        recovery_kind: str,
    ) -> Any:
        action_desc = _describe_action(command, args if args else list(kwargs.values()))
        await self._send_chat(action_desc)
        command_run = await self._run_agent_command(command, args, kwargs)
        result = command_run.raw
        action_result = command_run.result
        self._record_action(command, args, result)
        ok = action_result.ok
        self._active_task_add_event(
            "action_result",
            f"{command} args={args} kwargs={kwargs} -> {result}",
            ok=ok,
        )
        self._active_task_add_effects(command, action_result)
        await self._record_event("action_result", {
            "command": command,
            "args": args,
            "kwargs": kwargs,
            "result": result,
            "summary": action_result.summary,
            "recovery": recovery_kind,
        })
        self._history.append(ConversationTurn(role="system", content=action_result.history_content))
        await self._send_chat(action_result.summary)
        return result

    @staticmethod
    def _target_from_failed_search(
        command_name: str,
        args: list[Any],
        kwargs: dict[str, Any] | None,
    ) -> str:
        if command_name in {"collectBlocks", "searchForBlock"}:
            return str((kwargs or {}).get("block_type", args[0] if args else "")).strip()
        if command_name == "searchForEntity":
            return str((kwargs or {}).get("entity_type", args[0] if args else "")).strip()
        return ""

    async def _fallback_after_repeated_query(
        self,
        query_name: str,
        bot_state: BotState,
        last_result: str,
    ) -> bool | None:
        """Deterministic escape hatch when the LLM repeats a read-only query."""
        goal = self._last_message.lower()
        if query_name != "inventory" or "craft" not in goal:
            return None
        target = _craft_target_from_message(self._last_message)
        if not target:
            return None

        logger.info(
            "Fallback after repeated inventory: target=%s inventory=%s",
            target,
            _log_preview(last_result),
        )
        plan_run = await self._run_agent_command(
            "getCraftingPlan",
            [],
            {"target_item": target, "quantity": 1},
        )
        plan = plan_run.raw
        self._record_action("getCraftingPlan", [target, 1], plan)
        await self._record_event("action_result", {
            "command": "getCraftingPlan",
            "args": [],
            "kwargs": {"target_item": target, "quantity": 1},
            "result": plan,
            "fallback": "repeated_inventory",
        })
        if plan:
            self._history.append(ConversationTurn(role="system", content=plan))
            await self._send_chat(f"I already checked inventory. Next plan:\n{plan[:300]}")

        next_actions = self._next_crafting_actions_from_state(target, bot_state)
        if not next_actions:
            return True

        for command, args, kwargs in next_actions:
            action_desc = _describe_action(command, args if args else list(kwargs.values()))
            await self._send_chat(action_desc)
            command_run = await self._run_agent_command(command, args, kwargs)
            result = command_run.raw
            self._record_action(command, args, result)
            await self._record_event("action_result", {
                "command": command,
                "args": args,
                "kwargs": kwargs,
                "result": result,
                "fallback": "repeated_inventory",
            })
            await self._send_chat(f"Done: {result}")
            if not _action_succeeded(result):
                break
        return True

    def _next_crafting_actions_from_state(
        self, target: str, bot_state: BotState,
    ) -> list[tuple[str, list[Any], dict[str, Any]]]:
        """Pick a conservative concrete action for common repeated-query craft loops."""
        inventory = {item.name: item.count for item in bot_state.inventory}
        if target == "bed":
            bed = next((name for name in inventory if name.endswith("_bed")), None)
            if bed:
                return []
            knowledge_action = self._bed_actions_from_knowledge(inventory)
            if knowledge_action:
                return knowledge_action
            wool_counts = {
                name: count for name, count in inventory.items()
                if name.endswith("_wool")
            }
            for wool_name, count in wool_counts.items():
                if count >= 3:
                    bed_name = wool_name.removesuffix("_wool") + "_bed"
                    return [("craftRecipe", [], {"recipe_name": bed_name, "num": 1})]
            return [("searchForEntity", [], {"entity_type": "sheep", "search_range": 64})]
        return []

    def _bed_actions_from_knowledge(
        self, inventory: dict[str, int],
    ) -> list[tuple[str, list[Any], dict[str, Any]]]:
        """Use the KnowledgeGraph missing-counts to make bed collection finite."""
        if self._knowledge is None:
            return []
        try:
            plan = self._knowledge.get_missing_materials("bed", inventory=inventory)
        except Exception:
            logger.exception("KnowledgeGraph missing-material query failed")
            return []

        target = str(plan.get("target") or "bed")
        if plan.get("have_all"):
            return [("craftRecipe", [], {"recipe_name": target, "num": 1})]

        missing_counts = plan.get("missing_counts", {}) or {}
        wool_missing = [
            int(count)
            for name, count in missing_counts.items()
            if str(name).endswith("_wool")
        ]
        if not wool_missing:
            return []

        # One sheep normally drops one wool. Cap the deterministic fallback so
        # a malformed recipe or inventory state cannot run the bot forever.
        sheep_to_try = min(max(max(wool_missing), 1), 3)
        actions: list[tuple[str, list[Any], dict[str, Any]]] = []
        for _ in range(sheep_to_try):
            actions.append((
                "searchForEntity",
                [],
                {"entity_type": "sheep", "search_range": 64},
            ))
            actions.append((
                "attack",
                [],
                {"entity_type": "sheep", "range": 64},
            ))
        return actions

    async def _try_phase5a_skill(self, situation: str, bot_state: BotState) -> bool:
        """Run a high-confidence Phase 5a workflow skill before calling the LLM."""
        if self._skill_matcher is None or self._skill_store is None:
            return False
        match = await self._skill_matcher.match(situation, bot_state, min_score=0.9)
        if match is None:
            return False

        skill = match.skill
        logger.info("Phase5a skill matched: %s score=%.2f", skill.name, match.score)
        self._history.append(ConversationTurn(
            role="system",
            content=f"Phase5a skill selected: {skill.name} ({match.score:.2f})",
        ))
        await self._send_chat(f"Using skill: {skill.description or skill.name}")

        success = True
        for step in skill.steps:
            if self._shut_up or not self._running:
                success = False
                break
            action_desc = _describe_action(
                step.command,
                step.args if step.args else list(step.kwargs.values()),
            )
            await self._send_chat(action_desc)
            command_run = await self._run_agent_command(step.command, step.args, step.kwargs)
            result = command_run.raw
            self._record_action(step.command, step.args, result)
            await self._record_event("action_result", {
                "command": step.command,
                "args": step.args,
                "kwargs": step.kwargs,
                "result": result,
                "summary": command_run.result.summary,
                "skill": skill.name,
            })
            if not command_run.result.ok:
                success = False
                fallback = self._skill_failure_hint(match, result or "")
                if fallback:
                    self._history.append(ConversationTurn(role="system", content=fallback))
                await self._send_chat(f"Skill failed: {result or 'no response'}")
                break
            self._history.append(ConversationTurn(role="system", content=result))
            await self._send_chat(f"Done: {result}")

        await self._skill_store.record_outcome(skill, success=success)
        return success

    def _skill_failure_hint(self, match: SkillMatch, result: str) -> str | None:
        result_lower = result.lower()
        for mode in match.skill.failure_modes:
            if mode.pattern.lower() in result_lower:
                parts = [f"Skill failure mode matched: {mode.pattern}."]
                if mode.next_skill:
                    parts.append(f"Try skill '{mode.next_skill}' next.")
                if mode.hint:
                    parts.append(mode.hint)
                return " ".join(parts)
        return None

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
        args, kwargs = self._normalize_player_command_args(name, args, kwargs)
        args, kwargs = await self._normalise_wood_resource_args(name, args, kwargs)
        args, kwargs = await self._normalise_seed_grass_args(name, args, kwargs)
        hooks = _command_preflight_hooks(name)
        if "delivery_preposition" in hooks:
            kwargs = dict(kwargs or {})
            prepositioned = bool(kwargs.pop("_delivery_prepositioned", False))
            if not prepositioned:
                player_name = str(
                    kwargs.get("player_name", args[0] if args else self._last_sender or "")
                ).strip()
                if not player_name:
                    return "FAILED: Cannot deliver item without a target player."
                move_result = await self.execute_command(
                    "goToPlayer",
                    [],
                    {"player_name": player_name, "closeness": 3},
                )
                self._record_action("goToPlayer", [], move_result)
                move_ok = _action_succeeded(move_result)
                self._active_task_add_event(
                    "action_result",
                    (
                        "goToPlayer kwargs="
                        f"{{'player_name': {player_name!r}, 'closeness': 3}} "
                        f"-> {move_result}"
                    ),
                    ok=move_ok,
                )
                await self._record_event("action_result", {
                    "command": "goToPlayer",
                    "args": [],
                    "kwargs": {"player_name": player_name, "closeness": 3},
                    "result": move_result,
                    "delivery_preposition": True,
                })
                if not move_ok:
                    return (
                        "FAILED: Could not get close enough to deliver item before "
                        f"givePlayer: {move_result}"
                    )
        if name == "getCraftingPlan":
            target = str((kwargs or {}).get("target_item", values[0] if values else ""))
            quantity = int((kwargs or {}).get("quantity", values[1] if len(values) > 1 else 1) or 1)
            kg_plan = await self._get_knowledge_crafting_plan(target, quantity)
            if kg_plan is not None:
                return kg_plan
        preflight_result = await self._preflight_action_with_observation(name, args, kwargs)
        if preflight_result is not None:
            return preflight_result
        if "coordinate_validation" in hooks:
            coord_error = self._validate_coordinates_before_move(args, kwargs)
            if coord_error:
                return f"FAILED: {coord_error}"
        if "bed_dye_misconception" in hooks:
            dye_hint = await self._bed_dye_misconception_hint(args, kwargs)
            if dye_hint:
                return dye_hint
        if "missing_materials" in hooks:
            args, kwargs = await self._normalise_craft_recipe_args(args, kwargs)
            missing_hint = await self._craft_recipe_missing_materials_hint(args, kwargs)
            if missing_hint:
                return missing_hint
        if name == "stfu":
            if not _player_requested_mute(self._last_message):
                return "Ignored stfu because the player did not explicitly ask me to mute."
            self.shut_up()
            return "Bot muted."
        if name == "stop" and not _player_requested_stop(self._last_message):
            return "Ignored stop because the player did not explicitly ask me to stop."
        if name == "unshut":
            self.unshut()
            return "Bot unmuted."
        if name == "clearChat":
            self._history.clear()
            return "Chat history cleared."
        return await self.execute_command(command_name, args, kwargs)

    async def _preflight_action_with_observation(
        self,
        command_name: str,
        args: list[Any],
        kwargs: dict[str, Any] | None,
    ) -> str | None:
        """Refresh state before world-changing actions and skip wasteful tool setup."""
        hooks = _command_preflight_hooks(command_name)
        if not hooks or command_name not in _PRE_ACTION_OBSERVE_COMMANDS:
            return None

        was_running = self._running
        state = await self.observe()
        if was_running and not self._running:
            return "FAILED: observe() failed before action."

        nearby = set(state.nearby_block_types)
        if "skip_reusable_workstation" in hooks:
            block_type = str((kwargs or {}).get("block_type", args[0] if args else "")).strip()
            if block_type in _REUSABLE_WORKSTATIONS and block_type in nearby:
                return (
                    f"SKIPPED: {block_type} is already nearby. "
                    f"Use the existing {block_type} instead of placing another one."
                )

        if "skip_nearby_furnace" in hooks:
            recipe_name = str((kwargs or {}).get("recipe_name", args[0] if args else "")).strip()
            if recipe_name == "furnace" and "furnace" in nearby:
                return (
                    "SKIPPED: furnace is already nearby. "
                    "Use the existing furnace instead of crafting another one."
                )

        if "knowledge_tool_requirement" in hooks and self._knowledge is not None:
            block_type = str((kwargs or {}).get("block_type", args[0] if args else "")).strip()
            if block_type:
                inventory = [
                    {"name": item.name, "count": item.count}
                    for item in state.inventory
                ]
                try:
                    tool_choice = self._knowledge.select_tool_for_block(block_type, inventory)
                except Exception:
                    logger.exception("KnowledgeGraph collectBlocks preflight failed")
                    tool_choice = None
                if tool_choice and not bool(tool_choice.get("can_mine", True)):
                    min_tool = str(tool_choice.get("min_tool") or "").strip()
                    if min_tool:
                        inv_text = ", ".join(
                            f"{item.name} x{item.count}" for item in state.inventory
                        ) or "nothing"
                        return (
                            f"FAILED: Need a {min_tool} to mine {block_type}. "
                            f"You have: {inv_text}. Craft a {min_tool} first, then retry."
                        )

        if "smelt_dependencies" in hooks or "defer_log_charcoal" in hooks:
            item_name = str((kwargs or {}).get("item_name", args[0] if args else "")).strip()
            count = int((kwargs or {}).get("num", args[1] if len(args) > 1 else 1) or 1)
            if "smelt_dependencies" in hooks:
                smelt_preflight = self._smelt_item_preflight_hint(item_name, count, state)
                if smelt_preflight is not None:
                    return smelt_preflight
            if "defer_log_charcoal" in hooks and self._should_defer_log_charcoal_smelt(
                item_name,
                state,
            ):
                return (
                    "FAILED: Do not smelt logs into charcoal for this task yet. "
                    "HINT: For torches or generic crafting, use existing coal/charcoal "
                    "or searchForBlock(\"coal_ore\", 64) then collectBlocks(\"coal_ore\", n). "
                    "Only smelt logs if the player explicitly asked for charcoal or coal "
                    "has already been searched for and is unavailable."
                )

        return None

    def _smelt_item_preflight_hint(
        self,
        item_name: str,
        count: int,
        state: BotState,
    ) -> str | None:
        """Catch obvious smelting dependency failures before opening a furnace."""
        if not item_name:
            return None
        count = max(int(count or 1), 1)
        input_name = _normalise_smelt_input(item_name)
        inventory = {item.name: item.count for item in state.inventory}
        have_input = inventory.get(input_name, 0)
        if have_input < count:
            missing = count - have_input
            return (
                f"FAILED: Missing materials for smeltItem: {input_name} x{missing}.\n"
                f"HINT: Gather {missing}x {input_name} before retrying smeltItem."
            )

        if "furnace" not in state.nearby_block_types and not self._recently_found_block("furnace"):
            return (
                "FAILED: Missing materials for smeltItem: furnace x1.\n"
                "HINT: Craft or place a furnace before retrying smeltItem."
            )

        if not _inventory_has_smelting_fuel(state, count):
            fuel_needed = max(1, math.ceil(count / 8))
            return (
                f"FAILED: Missing materials for smeltItem: coal x{fuel_needed}.\n"
                "HINT: Use coal/charcoal or gather fuel before retrying smeltItem."
            )

        if input_name in _COMMON_SMELT_OUTPUTS:
            return None
        # Avoid blocking uncommon-but-valid recipes from the Mineflayer side;
        # only surface a hint so the planner has context.
        return None

    def _recently_found_block(self, block_type: str) -> bool:
        target = block_type.strip().lower()
        for action in reversed(self._recent_actions[-6:]):
            if action.get("name") != "searchForBlock":
                continue
            args = list(action.get("args") or [])
            searched = str(args[0] if args else "").strip().lower()
            result = str(action.get("result") or "").lower()
            if searched and searched != target and target not in result:
                continue
            if "failed:" in result or result.startswith("no ") or " not found" in result:
                return False
            if searched == target or target in result:
                return True
            return True
        return False

    def _should_defer_log_charcoal_smelt(self, item_name: str, state: BotState) -> bool:
        family = _wood_family_from_log(item_name)
        if family is None:
            return False
        if _is_explicit_charcoal_request(self._last_message):
            return False
        if not _is_torch_or_light_request(self._last_message):
            return False
        if self._recent_failed_coal_search():
            return False
        inventory = {item.name: item.count for item in state.inventory}
        if inventory.get("coal", 0) > 0 or inventory.get("charcoal", 0) > 0:
            return True
        nearby = set(state.nearby_block_types)
        if nearby.intersection(_COAL_ORE_BLOCKS):
            return True
        return True

    def _recent_failed_coal_search(self) -> bool:
        for action in reversed(self._recent_actions[-8:]):
            if action.get("name") != "searchForBlock":
                continue
            args = list(action.get("args") or [])
            target = str(args[0] if args else "").lower()
            if target not in _COAL_ORE_BLOCKS:
                continue
            result = str(action.get("result") or "").lower()
            if "failed:" in result or "no " in result or "not found" in result:
                return True
        return False

    async def _normalise_wood_resource_args(
        self,
        command_name: str,
        args: list[Any],
        kwargs: dict[str, Any] | None,
    ) -> tuple[list[Any], dict[str, Any] | None]:
        """Use any compatible wood type unless the player explicitly requested oak."""
        if _is_explicit_oak_request(self._last_message):
            return args, kwargs
        if command_name not in {"searchForBlock", "collectBlocks", "craftRecipe"}:
            return args, kwargs

        kw = dict(kwargs or {})
        values = args if args else list(kw.values())

        if command_name in {"searchForBlock", "collectBlocks"}:
            param = "block_type"
            raw_block = str(kw.get(param, values[0] if values else "")).strip().lower()
            if raw_block not in _GENERIC_WOOD_BLOCK_ALIASES and raw_block not in _WOOD_LOG_BLOCKS:
                return args, kwargs

            state = await self.observe()
            if not self._running:
                return args, kwargs
            preferred = self._preferred_nearby_log_block(state)
            if preferred is None or preferred == raw_block:
                return args, kwargs
            logger.info(
                "Normalizing wood block target: command=%s requested=%s preferred=%s",
                command_name,
                raw_block,
                preferred,
            )
            if args:
                new_args = list(args)
                new_args[0] = preferred
                return new_args, kwargs
            kw[param] = preferred
            return args, kw

        recipe_name = str(kw.get("recipe_name", values[0] if values else "")).strip().lower()
        if recipe_name not in _WOOD_PLANK_ITEMS:
            return args, kwargs

        state = await self.observe()
        if not self._running:
            return args, kwargs
        preferred_planks = self._preferred_planks_item(state)
        if preferred_planks is None or preferred_planks == recipe_name:
            return args, kwargs
        logger.info(
            "Normalizing planks craft target: requested=%s preferred=%s",
            recipe_name,
            preferred_planks,
        )
        if args:
            new_args = list(args)
            new_args[0] = preferred_planks
            return new_args, kwargs
        kw["recipe_name"] = preferred_planks
        return args, kw

    async def _normalise_seed_grass_args(
        self,
        command_name: str,
        args: list[Any],
        kwargs: dict[str, Any] | None,
    ) -> tuple[list[Any], dict[str, Any] | None]:
        """Prefer nearby seed-dropping grass before searching for taller grass."""
        if command_name not in {"searchForBlock", "breakNearbyBlocks", "collectBlocks"}:
            return args, kwargs

        task_message = (
            self._active_task.original_message
            if self._active_task is not None else self._last_message
        )
        if not _is_seed_source_request(task_message or self._last_message):
            return args, kwargs

        kw = dict(kwargs or {})
        values = args if args else list(kw.values())
        raw_block = str(kw.get("block_type", values[0] if values else "")).strip().lower()
        if raw_block not in _SEED_GRASS_BLOCKS:
            return args, kwargs

        state = await self.observe()
        if not self._running:
            return args, kwargs
        nearby = {str(block).strip().lower() for block in state.nearby_block_types}
        preferred = raw_block
        if "short_grass" in nearby:
            preferred = "short_grass"
        elif "tall_grass" in nearby:
            preferred = "tall_grass"
        elif raw_block == "grass":
            preferred = "short_grass"
        if preferred == raw_block:
            return args, kwargs

        logger.info(
            "Normalizing seed grass target: command=%s requested=%s preferred=%s nearby=%s",
            command_name,
            raw_block,
            preferred,
            sorted(nearby),
        )
        self._history.append(ConversationTurn(
            role="system",
            content=(
                f"For seed gathering, {preferred} can drop wheat_seeds. "
                f"Use {preferred} instead of {raw_block} because it is nearby."
            ),
        ))
        self._active_task_add_event(
            "normalized_command",
            f"{command_name}: {raw_block} -> {preferred} for seed gathering",
            ok=None,
        )
        if args:
            new_args = list(args)
            new_args[0] = preferred
            return new_args, kwargs
        kw["block_type"] = preferred
        return args, kw

    @staticmethod
    def _preferred_nearby_log_block(state: BotState) -> str | None:
        for block_name in state.nearby_block_types:
            name = str(block_name).strip().lower()
            if name in _WOOD_LOG_BLOCKS:
                return name
        return None

    @staticmethod
    def _preferred_planks_item(state: BotState) -> str | None:
        for item in state.inventory:
            name = item.name.strip().lower()
            if name in _WOOD_PLANK_ITEMS and item.count > 0:
                return name
        for item in state.inventory:
            family = _wood_family_from_log(item.name)
            if family is not None and item.count > 0:
                return _planks_for_wood_family(family)
        return None

    def _blocked_model_control_command(self, command_name: str) -> str | None:
        if command_name == "stfu" and not _player_requested_mute(self._last_message):
            return "Ignored stfu because the player did not explicitly ask me to mute."
        if command_name == "stop" and not _player_requested_stop(self._last_message):
            return "Ignored stop because the player did not explicitly ask me to stop."
        return None

    def _normalize_player_command_args(
        self,
        command_name: str,
        args: list[Any],
        kwargs: dict[str, Any] | None,
    ) -> tuple[list[Any], dict[str, Any] | None]:
        """Preserve actual chat-sender casing for player-target commands."""
        if not self._last_sender:
            return args, kwargs
        player_commands = {
            "goToPlayer": "player_name",
            "followPlayer": "player_name",
            "attackPlayer": "player_name",
            "givePlayer": "player_name",
            "startConversation": "player_name",
            "endConversation": "player_name",
            "lookAtPlayer": "player_name",
        }
        param = player_commands.get(command_name)
        if param is None:
            return args, kwargs

        sender = self._last_sender
        new_args = list(args)
        new_kwargs = dict(kwargs or {})
        if param in new_kwargs:
            current = str(new_kwargs[param])
            if current.lower() == sender.lower() or current.lower() in {"me", "player"}:
                new_kwargs[param] = sender
            return new_args, new_kwargs
        if new_args:
            current = str(new_args[0])
            if current.lower() == sender.lower() or current.lower() in {"me", "player"}:
                new_args[0] = sender
        return new_args, new_kwargs or kwargs

    def _validate_coordinates_before_move(
        self, args: list[Any], kwargs: dict[str, Any] | None,
    ) -> str | None:
        """Validate recalled/manual coordinates before movement."""
        values = args if args else list((kwargs or {}).values())
        raw_x = (kwargs or {}).get("x", values[0] if len(values) > 0 else None)
        raw_y = (kwargs or {}).get("y", values[1] if len(values) > 1 else None)
        raw_z = (kwargs or {}).get("z", values[2] if len(values) > 2 else None)
        try:
            x, y, z = float(raw_x), float(raw_y), float(raw_z)
        except (TypeError, ValueError):
            return "coordinates must include numeric x, y, and z before moving"
        if not (-64 <= y <= 320):
            return f"coordinate y={y:g} is outside the valid world height"
        if abs(x) > 30_000_000 or abs(z) > 30_000_000:
            return "coordinates are outside the Minecraft world border"
        return None

    # ── Intent-specific handlers ────────────────────────────────────

    async def _handle_meta_command(self, message: str, source: str) -> None:
        """Route player memory commands to MemoryService."""
        logger.info("META_COMMAND from %s: %s", source, message)
        if self._memory is None:
            await self._send_chat("Memory is not available right now.")
            return

        lowered = message.lower().strip()
        try:
            if re.search(r"\bforget\b|忘记", lowered):
                name = _memory_name_from_message(message, verbs=("forget", "unforget"))
                deleted = await self._memory.forget(name)
                await self._send_chat(
                    f"Forgot memory '{name}'." if deleted else f"I don't know '{name}'."
                )
                return
            if re.search(r"\bunforget\b", lowered):
                name = _memory_name_from_message(message, verbs=("unforget",))
                restored = await self._memory.unforget(name)
                await self._send_chat(
                    f"Memory '{name}' can be written again."
                    if restored else f"'{name}' was not forgotten."
                )
                return

            record = await self._build_memory_record_from_meta(message, source)
            path = await self._memory.remember(record, is_player_requested=True)
            await self._send_chat(f"Remembered {record.name} ({record.type.value}).")
            logger.info("Memory command wrote %s to %s", record.name, path)
        except Exception as exc:
            logger.exception("Memory command failed")
            await self._send_chat(f"I couldn't save that memory: {exc}")

    async def _build_memory_record_from_meta(self, message: str, source: str) -> MemoryRecord:
        """Create a deterministic memory candidate from a meta command."""
        state = await self.observe()
        lowered = message.lower()
        text = _strip_memory_verbs(message)
        now_desc = text[:120] or message[:120]

        is_here = bool(
            re.search(r"\b(here|this place|mark this|mark here)\b|这里|这儿|当前位置", lowered)
        )
        is_feedback_like = bool(re.search(r"\b(prefer|avoid)\b|偏好|避免|不要|别", lowered))

        if is_here:
            name = _memory_name_from_message(text, default="marked_place")
            pos = state.position
            body = (
                f"{text}\n\n"
                f"Position: ({pos.x:.0f}, {pos.y:.0f}, {pos.z:.0f})\n"
                f"World: default\n"
                f"Marked by: {source}\n\n"
                "**Why:** Player explicitly asked me to remember this location.\n\n"
                "**How to apply:** Recall this place by name before navigating, "
                "then verify the destination is still reachable before moving."
            )
            return MemoryRecord(
                name=name,
                description=now_desc,
                type=MemoryType.PLACE,
                scope=Scope.WORLD,
                confidence=0.95,
                source="player_marked",
                tags=["place", "player_marked"],
                body=body,
            )

        if is_feedback_like:
            name = _memory_name_from_message(text, default="player_preference")
            return MemoryRecord(
                name=name,
                description=now_desc,
                type=MemoryType.FEEDBACK,
                scope=Scope.BOT,
                confidence=0.9,
                source="player_confirmed",
                tags=["preference", "feedback"],
                body=(
                    f"{text}\n\n"
                    f"**Why:** {source} explicitly gave this preference or avoidance rule.\n\n"
                    "**How to apply:** Check current state before acting; if the rule "
                    "does not fit the situation, ask for clarification instead of forcing it."
                ),
            )

        name = _memory_name_from_message(text, default="remembered_fact")
        return MemoryRecord(
            name=name,
            description=now_desc,
            type=MemoryType.FACT,
            scope=Scope.WORLD,
            confidence=0.9,
            source="player_confirmed",
            tags=["player_memory"],
            body=(
                f"{text}\n\n"
                f"**Why:** {source} explicitly asked me to remember this.\n\n"
                "**How to apply:** Recall this fact when answering related questions "
                "or planning actions in this world."
            ),
        )

    async def _handle_feedback(self, message: str, source: str) -> None:
        """Persist player feedback with recent action context."""
        recent = [a["name"] for a in self._recent_actions]
        logger.info(
            "FEEDBACK from %s: %s (last %d actions: %s)",
            source, message, len(self._recent_actions), recent,
        )
        if self._memory is None:
            await self._send_chat(f"Got it — I'll learn from '{message}'.")
            return

        name = _memory_name_from_message(message, default="player_feedback")
        trace = "\n".join(
            f"- {a['name']} args={a['args']} result={a['result']}"
            for a in self._recent_actions
        ) or "- No recent action trace."
        record = MemoryRecord(
            name=name,
            description=message[:120],
            type=MemoryType.FEEDBACK,
            scope=Scope.BOT,
            confidence=0.9,
            source="player_confirmed",
            tags=["feedback", "player_correction"],
            body=(
                f"{message}\n\n"
                f"**Why:** {source} corrected my recent behavior.\n\n"
                f"**Recent action trace:**\n{trace}\n\n"
                "**How to apply:** Avoid repeating the corrected behavior in similar "
                "state/action contexts. If unsure, ask before acting."
            ),
        )
        try:
            await self._memory.remember(record, is_player_requested=True)
            await self._send_chat(f"Got it — saved feedback '{record.name}'.")
        except Exception as exc:
            logger.exception("Feedback memory write failed")
            await self._send_chat(f"Got it, but I couldn't save the feedback: {exc}")

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
        try:
            exec_result = await self._bridge.execute_code(code, timeout_ms=60000)
        except Exception as exc:
            logger.warning("Dynamic action execution failed: %s", exc)
            return f"FAILED: Dynamic action failed: {exc}"
        if isinstance(exec_result, dict):
            output = exec_result.get("output") or exec_result.get("result") or ""
            output_text = str(output)
            has_logged_error = bool(re.search(r"(^|\n)(FAILED:|Error:)\s", output_text))
            status = "ok" if exec_result.get("ok", True) and not has_logged_error else "failed"
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

                if _action_succeeded(result):
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
        chat_text = _compact_chat_text(text)
        if not chat_text:
            return
        if chat_text != text.strip():
            logger.info(
                "Compacted in-game chat: shown=%s full=%s",
                chat_text,
                _log_preview(text, limit=1000),
            )
        try:
            await self._bridge.chat(chat_text)
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

    async def run_loop(self, tick_interval: float = 0.25) -> None:
        """Run the agent main loop: safety check → quick observe → modes.

        Top-level loop, default 4 TPS (250 ms). Only does a *quick* observe
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
        """Build a memory string from index + recalled long-term records."""
        if self._memory is None:
            return ""

        parts: list[str] = []
        injection = await self._memory.plan_injection(token_budget=1200)
        injection.full = [
            r for r in injection.full if _memory_record_matches_task(r, message)
        ]
        injection.summary = [
            r for r in injection.summary if _memory_record_matches_task(r, message)
        ]
        injection.index = [
            name for name in injection.index if not _is_navigation_only_request(message)
            or not name.startswith("task_")
            or re.search(r"跟|过来|重生点|坐标|位置|come|follow|spawn|place", name, re.I)
        ]
        if injection.full or injection.summary or injection.index:
            parts.append("Memory index:")
            full_records = sorted(injection.full, key=_memory_record_is_failed_task)
            summary_records = sorted(injection.summary, key=_memory_record_is_failed_task)
            failed_summary = [r for r in summary_records if _memory_record_is_failed_task(r)]
            for record in full_records[:3]:
                parts.append(
                    f"- {record.description} ({record.type.value}/{record.name})\n"
                    f"  {record.body[:300]}"
                )
            for record in [r for r in summary_records if not _memory_record_is_failed_task(r)][:8]:
                parts.append(f"- {record.description} ({record.type.value}/{record.name})")
            for record in failed_summary[:2]:
                parts.append(
                    f"- Failed memory, lesson only: {record.description} "
                    f"({record.type.value}/{record.name})"
                )
            for name in injection.index[:20]:
                parts.append(f"- {name}")
        logger.info(
            "Agent memory injection selected: full=%s summary=%s index=%s",
            [r.name for r in sorted(injection.full, key=_memory_record_is_failed_task)[:3]],
            [
                r.name
                for r in sorted(injection.summary, key=_memory_record_is_failed_task)[:8]
            ],
            injection.index[:20],
        )

        keywords = [w for w in message.lower().split() if len(w) > 2] if message else []
        results = await self._memory.recall(MemoryQuery(
            keywords=keywords, limit=5, min_confidence=0.5,
        ))
        results = [
            r for r in results if _memory_record_matches_task(r.record, message)
        ]
        if results:
            parts.append("Matched memory details:")
            normal_results = [
                r for r in results if not _memory_record_is_failed_task(r.record)
            ]
            failed_results = [
                r for r in results if _memory_record_is_failed_task(r.record)
            ]
            for r in normal_results:
                parts.append(
                    f"- {r.record.description} ({r.record.type.value}/{r.record.name})"
                )
            for r in failed_results[:1]:
                parts.append(
                    f"- Failed memory, lesson only: {r.record.description} "
                    f"({r.record.type.value}/{r.record.name})"
                )
        logger.info(
            "Agent memory recall for prompt: message=%r keywords=%s hits=%s",
            message,
            keywords,
            [
                {
                    "name": r.record.name,
                    "score": round(r.score, 3),
                    "type": r.record.type.value,
                    "scope": r.record.scope.value,
                }
                for r in results
            ],
        )
        return "\n".join(parts)

    async def _record_event(
        self, event_type: str, payload: dict[str, Any],
    ) -> None:
        """Fire-and-forget L1 event recording."""
        if self._memory is not None:
            await self._memory.record_event(event_type, payload)

    async def _replace_active_task(
        self,
        requester: str,
        message: str,
        *,
        planning_strategy: PlanningStrategy = PlanningStrategy.REACT,
    ) -> None:
        """Start a new lightweight task, cancelling any unfinished one."""
        await self._finish_active_task("cancelled", reason="Replaced by a newer player command.")
        baseline_state = (
            await self.observe()
            if self._running and self._bridge is not None
            else BotState()
        )
        task = self._task_manager.start(
            requester=requester,
            message=message,
            baseline_state=baseline_state,
            history_len=len(self._history),
            planning_strategy=planning_strategy,
            timeout_s=300.0,
        )
        task_id = task.id
        self._active_task_stack = self._task_manager.stack
        self._active_task = task
        spec = self._active_task.completion_spec
        if spec is not None:
            self._active_task.events.append(ActiveTaskEvent(
                kind="completion_spec",
                message=(
                    f"kind={spec.kind} targets={list(spec.target_items)} "
                    f"acceptable={list(spec.acceptable_items)} quantity={spec.quantity} "
                    f"requires_delivery={spec.requires_delivery} "
                    f"delivery_score={spec.delivery_score:.2f}"
                ),
            ))
        logger.info(
            "ActiveTask start: id=%s requester=%s planning_strategy=%s "
            "completion_spec=%s delivery=%s message=%r",
            task_id,
            requester,
            planning_strategy.value,
            spec.kind if spec is not None else None,
            spec.requires_delivery if spec is not None else None,
            message,
        )
        await self._record_event("task_start", {
            "task_id": task_id,
            "requester": requester,
            "message": message,
            "planning_strategy": planning_strategy.value,
            "stack_depth": len(self._active_task_stack),
            "baseline_inventory": self._active_task.baseline_inventory,
        })

    def _active_task_add_snapshot(self, state: BotState, *, note: str) -> None:
        task = self._active_task
        if task is None or task.status != "running":
            return
        snapshots = task.snapshots if task.snapshots is not None else []
        snapshots.append(ActiveTaskSnapshot(
            seq=len(snapshots),
            note=note,
            inventory=_inventory_counts_from_state(state),
            position=_position_tuple_from_state(state),
        ))
        task.snapshots = snapshots

    def _active_task_add_event(
        self,
        kind: str,
        message: str,
        *,
        ok: bool | None = None,
    ) -> None:
        task = self._active_task
        if task is None or task.status != "running":
            return
        task.events.append(ActiveTaskEvent(kind=kind, message=message, ok=ok))
        if kind == "action_result":
            task.attempts += 1
            if ok is False:
                task.failures += 1

    def _active_task_add_effects(self, command_name: str, result: Any) -> None:
        task = self._active_task
        if task is None or task.status != "running":
            return
        if isinstance(result, ActionResult):
            effects = list(result.effects)
        else:
            effects = _action_effects_from_result(command_name, result)
        if not effects:
            return
        task.effects.extend(effects)
        logger.info(
            "ActiveTask effects: task=%s command=%s effects=%s",
            task.id,
            command_name,
            [
                {
                    "type": effect.type,
                    "name": effect.name,
                    "count": effect.count,
                    "item": effect.item,
                }
                for effect in effects
            ],
        )

    def _apply_delivery_joint_judgement(
        self,
        decision: DispatchDecision | None,
    ) -> None:
        task = self._active_task
        if task is None or task.status != "running" or task.completion_spec is None:
            return
        small_score = self._small_model_delivery_score(decision)
        if small_score is None:
            return
        spec = task.completion_spec
        combined = (
            spec.delivery_score * _DELIVERY_PRIMARY_WEIGHT
            + small_score * _DELIVERY_SMALL_MODEL_WEIGHT
        )
        requires_delivery = combined >= _DELIVERY_DECISION_THRESHOLD
        if (
            requires_delivery == spec.requires_delivery
            and abs(combined - spec.delivery_score) < 0.01
        ):
            return
        task.completion_spec = CompletionSpec(
            kind=spec.kind,
            target_items=spec.target_items,
            acceptable_items=spec.acceptable_items,
            quantity=spec.quantity,
            terminal_commands=spec.terminal_commands,
            intermediate_commands=spec.intermediate_commands,
            requires_delivery=requires_delivery,
            delivery_score=combined,
            delivery_reason=(
                f"{spec.delivery_reason}; joint delivery judgement "
                f"primary={spec.delivery_score:.2f} small={small_score:.2f}"
            ),
        )
        self._active_task_add_event(
            "completion_spec",
            (
                "delivery_judgement "
                f"primary={spec.delivery_score:.2f} small={small_score:.2f} "
                f"combined={combined:.2f} requires_delivery={requires_delivery}"
            ),
        )
        logger.info(
            "ActiveTask delivery judgement: task=%s primary=%.2f small=%.2f "
            "combined=%.2f requires_delivery=%s",
            task.id,
            spec.delivery_score,
            small_score,
            combined,
            requires_delivery,
        )

    @staticmethod
    def _small_model_delivery_score(decision: DispatchDecision | None) -> float | None:
        if decision is None:
            return None
        text = " ".join((
            decision.route,
            decision.task_shape,
            decision.reason,
            " ".join(decision.requested_outputs),
            " ".join(decision.required_steps),
        )).lower()
        if decision.route in {"direct_item", "direct_cook"}:
            return 0.90
        if decision.requested_outputs:
            return 0.80
        if re.search(r"\b(deliver|give|bring|fetch|hand)\b|交付|给|拿给|带给", text):
            return 0.75
        if decision.route in {"direct_action", "chat", "ask_clarify"}:
            return 0.10
        return 0.40

    async def _report_finished_task_to_player(
        self,
        task: ActiveTask,
        *,
        status: str,
        reason: str = "",
    ) -> None:
        """Move near the requester before giving a concise final task report."""
        requester = task.requester or self._last_sender
        moved = False
        move_result: Any = None
        should_move = requester and not (
            status == "succeeded" and _player_requested_follow_me(task.original_message)
        )
        if should_move:
            try:
                command_run = await self._run_agent_command(
                    "goToPlayer",
                    [],
                    {"player_name": requester, "closeness": 3},
                )
                move_result = command_run.raw
                moved = command_run.result.ok
                self._record_action("goToPlayer", [], move_result)
                task.events.append(ActiveTaskEvent(
                    kind="failure_report_move",
                    message=f"goToPlayer({requester}, 3) -> {move_result}",
                    ok=moved,
                ))
                await self._record_event("action_result", {
                    "command": "goToPlayer",
                    "args": [],
                    "kwargs": {"player_name": requester, "closeness": 3},
                    "result": move_result,
                    "summary": command_run.result.summary,
                    "failure_report": True,
                })
            except Exception as exc:
                logger.debug(
                    "Failed to move to player before task failure report",
                    exc_info=True,
                )
                task.events.append(ActiveTaskEvent(
                    kind="failure_report_move",
                    message=f"Could not move to {requester}: {exc}",
                    ok=False,
                ))

        detail = reason.strip() or task.summary or task.original_message
        detail = _log_preview(detail, limit=90)
        if status == "succeeded":
            prefix = "Task complete"
        else:
            prefix = "Task failed"
            if requester and not moved:
                prefix = "Task failed; I could not reach you"
        await self._send_chat(
            f"{prefix}: {detail}. I can accept a new command now."
        )

    async def _finish_active_task(self, status: str, *, reason: str = "") -> None:
        """Summarise, write memory, then clear the current active task."""
        task = self._active_task
        if task is None or task.status != "running":
            return
        if (
            status == "succeeded"
            and not self._active_task_has_delivery(task)
            and not self._active_task_has_completion_evidence(task)
        ):
            delivered = await self._maybe_deliver_active_task_result()
            if delivered is False:
                status = "failed"
                reason = (
                    f"{reason} Could not deliver requested item to player."
                    if reason else "Could not deliver requested item to player."
                )
            elif delivered is True:
                reason = (
                    f"{reason} Delivered requested item to player."
                    if reason else "Delivered requested item to player."
                )
            elif self._active_task_expects_delivery(task):
                status = "failed"
                reason = (
                    f"{reason} No requested item was available to deliver."
                    if reason else "No requested item was available to deliver."
                )
        task.status = status
        if reason:
            task.events.append(ActiveTaskEvent(kind="finish_reason", message=reason, ok=None))
        task.summary = await self._summarize_active_task(task)
        if task.status in {"succeeded", "failed"}:
            await self._report_finished_task_to_player(
                task,
                status=task.status,
                reason=reason,
            )
        logger.info(
            "ActiveTask finish: id=%s status=%s attempts=%d failures=%d summary=%s",
            task.id,
            task.status,
            task.attempts,
            task.failures,
            _log_preview(task.summary),
        )
        await self._record_event("task_finish", {
            "task_id": task.id,
            "status": task.status,
            "attempts": task.attempts,
            "failures": task.failures,
            "summary": task.summary,
            "stack_depth": len(self._active_task_stack),
        })
        await self._remember_active_task(task)
        self._active_task = None
        self._task_manager.clear()
        self._active_task_stack = self._task_manager.stack

    @staticmethod
    def _active_task_has_delivery(task: ActiveTask) -> bool:
        return any(
            event.kind == "action_result"
            and ("givePlayer" in event.message or "give_to_player" in event.message)
            and event.ok is not False
            for event in (task.events or [])
        )

    @staticmethod
    def _active_task_has_completion_evidence(task: ActiveTask) -> bool:
        return any(
            event.kind == "completion_evidence" and event.ok is True
            for event in (task.events or [])
        )

    @staticmethod
    def _active_task_expects_delivery(task: ActiveTask) -> bool:
        if task.completion_spec is not None:
            return task.completion_spec.requires_delivery
        message = task.original_message or ""
        return (
            _parse_cook_request(message) is not None
            or _parse_craft_request(message) is not None
            or _parse_item_request(message) is not None
        )

    async def _maybe_deliver_active_task_result(self) -> bool | None:
        """Best-effort generic delivery hook for successful LLM-driven tasks."""
        if not self._last_sender:
            return None
        state = await self.observe()
        if not self._running:
            return False

        candidate = self._delivery_candidate_from_state(state)
        if candidate is None:
            return None
        item_name, quantity = candidate
        logger.info(
            "Auto-delivering active task result: item=%s quantity=%d player=%s",
            item_name,
            quantity,
            self._last_sender,
        )
        return await self._deliver_item_to_player(
            item_name,
            quantity,
            reason="active_task_auto_delivery",
        )

    def _delivery_candidate_from_state(self, state: BotState) -> tuple[str, int] | None:
        task = self._active_task
        spec = task.completion_spec if task is not None else None
        if spec is not None and spec.requires_delivery and spec.acceptable_items:
            for item_name in spec.acceptable_items:
                have = _inventory_count_from_state(state, item_name)
                requested = spec.quantity or have
                if have >= requested:
                    return item_name, requested

        cook_request = _parse_cook_request(self._last_message)
        if cook_request is not None:
            have = _inventory_count_from_state(state, cook_request.cooked_item)
            if have > 0:
                quantity = cook_request.quantity or have
                return cook_request.cooked_item, min(quantity, have)

        craft_request = _parse_craft_request(self._last_message)
        if craft_request is not None:
            have = _inventory_count_from_state(state, craft_request.item_name)
            if have >= craft_request.quantity:
                return craft_request.item_name, craft_request.quantity

        item_request = _parse_item_request(self._last_message)
        if item_request is not None:
            have = _inventory_count_from_state(state, item_request.item_name)
            if have >= item_request.quantity:
                return item_request.item_name, item_request.quantity

        return None

    async def _summarize_active_task(self, task: ActiveTask) -> str:
        fallback = self._deterministic_task_summary(task)
        if self._memory is None or self._prompter is None:
            return fallback
        event_lines = self._active_task_event_lines(task, limit=12)
        prompt = (
            "Summarize this Minecraft bot task in one concise sentence for long-term memory. "
            "Mention the player request, final status, and the most important "
            "success or failure cause. "
            "Do not include logs or stack traces.\n\n"
            f"Requester: {task.requester}\n"
            f"Request: {task.original_message}\n"
            f"Status: {task.status}\n"
            f"Attempts: {task.attempts}\n"
            f"Failures: {task.failures}\n"
            f"Events:\n{event_lines}"
        )
        try:
            response = await self._prompter.send_request(LLMInput(
                history=[{"role": "user", "content": prompt}],
                system_message="You write compact operational memories for a Minecraft bot.",
                tool_choice="none",
            ))
        except Exception:
            logger.exception("ActiveTask LLM summary failed; using deterministic summary")
            return fallback
        summary = response.content.strip()
        if not summary:
            return fallback
        return summary[:500]

    def _deterministic_task_summary(self, task: ActiveTask) -> str:
        tail = ""
        events = task.events or []
        for event in reversed(events):
            if event.kind == "action_result" or event.kind == "finish_reason":
                tail = event.message
                break
        if tail:
            return (
                f"{task.requester} asked: {task.original_message}. "
                f"Task {task.status} after {task.attempts} action(s), "
                f"{task.failures} failure(s). Last note: {tail[:220]}"
            )
        return (
            f"{task.requester} asked: {task.original_message}. "
            f"Task {task.status} after {task.attempts} action(s), "
            f"{task.failures} failure(s)."
        )

    @staticmethod
    def _active_task_event_lines(task: ActiveTask, *, limit: int = 12) -> str:
        events = task.events or []
        selected = events[-limit:]
        lines = []
        for event in selected:
            ok = "" if event.ok is None else (" ok" if event.ok else " failed")
            lines.append(f"- {event.kind}{ok}: {event.message[:260]}")
        return "\n".join(lines) or "- no events"

    def _active_task_prompt_context(self) -> str:
        task = self._active_task
        if task is None or task.status != "running":
            return ""
        spec = task.completion_spec
        spec_lines = ""
        if spec is not None:
            spec_lines = (
                "completion_spec:\n"
                f"- kind: {spec.kind}\n"
                f"- target_items: {list(spec.target_items)}\n"
                f"- acceptable_items: {list(spec.acceptable_items)}\n"
                f"- quantity: {spec.quantity}\n"
                f"- terminal_commands: {sorted(spec.terminal_commands)}\n"
                f"- intermediate_commands: {sorted(spec.intermediate_commands)}\n"
                f"- requires_delivery: {spec.requires_delivery}\n"
            )
        baseline_items = ""
        if task.baseline_inventory:
            relevant = (
                set(spec.acceptable_items if spec is not None else ())
                | set(spec.target_items if spec is not None else ())
            )
            baseline_subset = {
                name: count
                for name, count in task.baseline_inventory.items()
                if not relevant or name in relevant
            }
            if baseline_subset:
                baseline_items = f"baseline_inventory: {baseline_subset}\n"
        parent_context = ""
        if task.task_type == "subgoal" and len(self._active_task_stack) > 1:
            parent = self._active_task_stack[-2]
            parent_context = (
                "PARENT TASK\n"
                f"- id: {parent.id}\n"
                f"- request: {parent.original_message}\n"
                f"- resume_command: {task.resume_command}\n"
                "Rule: finish this internal dependency subgoal first, then "
                "resume the parent task. Do not treat the subgoal as a new "
                "player request.\n"
            )
        return (
            "ACTIVE TASK\n"
            f"id: {task.id}\n"
            f"type: {task.task_type}\n"
            f"stack_depth: {len(self._active_task_stack)} (only top task is active)\n"
            f"requester: {task.requester}\n"
            f"request: {task.original_message}\n"
            f"reason: {task.reason}\n"
            f"planning_strategy: {task.planning_strategy.value}\n"
            f"attempts: {task.attempts}\n"
            f"failures: {task.failures}\n"
            f"snapshots: {len(task.snapshots or [])}\n"
            f"{parent_context}"
            f"{baseline_items}"
            f"{spec_lines}"
            "recent task events:\n"
            f"{self._active_task_event_lines(task, limit=6)}\n"
            "Task boundary: complete only this request from the requester. "
            "Do not resume older memory tasks or adjacent goals unless they are "
            "strictly required by this request. Failed memories are lessons, not "
            "pending objectives. Use recent task events to avoid repeating "
            "completed or failed steps."
        )

    def _history_for_active_task_prompt(self) -> list[ConversationTurn]:
        task = self._active_task
        if task is None or task.status != "running":
            return self._history
        start = max(0, min(task.history_start_index, len(self._history)))
        return self._history[start:]

    @staticmethod
    def _planning_strategy_prompt(task: ActiveTask) -> str:
        if task.planning_strategy == PlanningStrategy.ROLLING_PLAN:
            return (
                "Planning strategy: rolling_plan. Think 2-3 short steps ahead "
                "for this stable workflow, but execute ONLY the immediate next "
                "command. Future steps must be plain text without !command "
                "syntax or tool calls. After the command runs, observe the new "
                "state and revise or discard the remaining plan if reality "
                "changed. Do not make a full long-horizon plan."
            )
        return (
            "Planning strategy: react. Choose only the best immediate next "
            "command from the latest observation; do not write future steps."
        )

    async def _remember_active_task(self, task: ActiveTask) -> None:
        if self._memory is None:
            return
        quality = self._active_task_memory_quality(task)
        if quality is None:
            logger.info(
                "ActiveTask memory skipped by quality gate: task=%s status=%s summary=%s",
                task.id,
                task.status,
                _log_preview(task.summary),
            )
            return
        confidence, quality_tags = quality
        spec = task.completion_spec
        task_kind = spec.kind if spec is not None else "unknown"
        record = MemoryRecord(
            name=f"task_{task.id}",
            description=task.summary[:150],
            type=MemoryType.EPISODIC,
            scope=Scope.WORLD,
            confidence=confidence,
            source="llm_reflected",
            tags=[
                "active_task",
                f"task_{task.status}",
                f"task_kind_{task_kind}",
                f"requester_{_slugify_task_name(task.requester, limit=24)}",
                *quality_tags,
            ],
            body=(
                f"**Request:** {task.requester}: {task.original_message}\n\n"
                f"**Status:** {task.status}\n\n"
                f"**Task Kind:** {task_kind}\n\n"
                f"**Attempts:** {task.attempts}\n\n"
                f"**Failures:** {task.failures}\n\n"
                f"**Summary:** {task.summary}\n\n"
                f"**Completion Evidence:** "
                f"{'yes' if self._active_task_has_completion_evidence(task) else 'no'}\n\n"
                f"**Events:**\n{self._active_task_event_lines(task, limit=20)}\n"
            ),
        )
        try:
            await self._memory.remember(record)
        except Exception:
            logger.exception("ActiveTask memory write failed")

    def _active_task_memory_quality(self, task: ActiveTask) -> tuple[float, list[str]] | None:
        """Return confidence/tags for a task memory, or None to skip writing."""
        has_completion = self._active_task_has_completion_evidence(task)
        has_delivery = self._active_task_has_delivery(task)
        contradicts = _summary_contradicts_task_status(task.summary, task.status)
        tags: list[str] = []

        if task.status == "succeeded":
            if contradicts:
                return None
            if not has_completion and not has_delivery:
                return None
            tags.append("memory_quality_verified")
            return 0.82, tags

        if task.status == "failed":
            tags.extend(["memory_quality_lesson_only", "memory_quality_low_confidence"])
            if contradicts:
                tags.append("memory_quality_summary_conflict")
            return 0.45, tags

        if task.status == "cancelled":
            if task.attempts <= 0:
                return None
            tags.extend(["memory_quality_lesson_only", "memory_quality_cancelled"])
            return 0.35, tags

        tags.append("memory_quality_low_confidence")
        return 0.40, tags

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

    async def _craft_recipe_missing_materials_hint(
        self,
        args: list[Any],
        kwargs: dict[str, Any] | None,
    ) -> str | None:
        """Avoid Mineflayer crafting when the deterministic plan says ingredients are missing."""
        if self._knowledge is None:
            return None

        kw = dict(kwargs or {})
        values = args if args else list(kw.values())
        recipe_name = str(kw.get("recipe_name", values[0] if values else "")).strip()
        if not recipe_name:
            return None
        quantity = int(kw.get("num", values[1] if len(values) > 1 else 1) or 1)

        bot_state = await self.observe()
        inventory = [{"name": item.name, "count": item.count} for item in bot_state.inventory]
        try:
            plan = self._knowledge.get_missing_materials(
                recipe_name, inventory=inventory, quantity=quantity,
            )
        except Exception:
            logger.exception("KnowledgeGraph missing-material preflight failed")
            return None

        missing_counts = dict(plan.get("missing_counts", {}) or {})
        if not missing_counts:
            return None

        if "crafting_table" in missing_counts:
            bot_state = await self.observe()
            if "crafting_table" in bot_state.nearby_block_types:
                missing_counts.pop("crafting_table", None)
        if not missing_counts:
            return None

        missing_text = ", ".join(
            f"{name} x{count}" for name, count in sorted(missing_counts.items())
        )
        target = str(plan.get("target") or recipe_name)
        steps = "; ".join(str(step) for step in plan.get("steps", []) if step)
        return (
            f"FAILED: Missing materials for {target}: {missing_text}.\n"
            f"HINT: {steps or 'Gather the missing materials before retrying craftRecipe.'}"
        )

    async def _bed_dye_misconception_hint(
        self,
        args: list[Any],
        kwargs: dict[str, Any] | None,
    ) -> str | None:
        """Catch the common wool→dye misconception during bed crafting."""
        if "bed" not in self._last_message.lower():
            return None

        kw = dict(kwargs or {})
        values = args if args else list(kw.values())
        recipe_name = str(kw.get("recipe_name", values[0] if values else "")).lower()
        if recipe_name not in {"white_dye", "light_gray_dye", "gray_dye"}:
            return None

        bot_state = await self.observe()
        wool = [item for item in bot_state.inventory if item.name.endswith("_wool")]
        if not wool:
            return None

        best = max(wool, key=lambda item: item.count)
        color = best.name.removesuffix("_wool")
        needed = max(3 - best.count, 0)
        if needed:
            next_step = f"collect {needed} more {best.name}"
        else:
            next_step = f"craft {color}_bed"
        return (
            "FAILED: Wool cannot be crafted into dye. For a bed, use 3 matching "
            f"wool blocks directly with 3 planks. You have {best.name} x{best.count}; "
            f"next step: {next_step}, not craft {recipe_name}."
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
    """Compatibility wrapper for tests and callers importing from agent.core."""
    return command_defs()



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
