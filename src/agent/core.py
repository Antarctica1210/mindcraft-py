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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from llm import ExampleStore, InputIntent, InputRouter, Prompter
from llm.dispatch_gate import DispatchDecision, DispatchGate
from llm.message_builder import CommandDef, ConversationTurn, MessageBuilder
from llm.prompter import LLMInput, LLMOutput
from llm.tool_parser import parse_response
from memory.file_store import ScopedFileMemoryStore
from memory.models import MemoryQuery, MemoryRecord, MemoryType, Scope
from memory.service import MemoryService
from skills.loader import SkillStore
from skills.matcher import SkillMatch, SkillMatcher
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
    "countEntities",
    "nearbyBlocks",
    "craftable",
    "help",
    "modes",
    "getCraftingPlan",
    "searchWiki",
    "showVillagerTrades",
})
_PRE_ACTION_OBSERVE_COMMANDS: frozenset[str] = frozenset({
    "attack",
    "breakNearbyBlocks",
    "collectBlocks",
    "consume",
    "craftRecipe",
    "discard",
    "equip",
    "givePlayer",
    "goToCoordinates",
    "goToPlayer",
    "moveAway",
    "moveTo",
    "placeHere",
    "searchForBlock",
    "searchForEntity",
    "smeltItem",
})
_REUSABLE_WORKSTATIONS: frozenset[str] = frozenset({
    "crafting_table",
    "furnace",
    "campfire",
    "blast_furnace",
    "smoker",
})
_DELIVERY_RESULT_RE = re.compile(r"\bGave\s+(?P<count>\d+)x\s+", re.IGNORECASE)

# Loop guards for the plan loop (mitigations after the 2026-05-12 bed-craft
# log where the model spammed searchWiki with paraphrased queries).
#
# _MAX_REPEATED_QUERY_PER_NAME: how many calls to the SAME query command (by
# name, regardless of arguments) we tolerate inside one user turn before
# injecting a "pivot to a concrete action" reminder. The pre-existing
# exact-arg repeat check still runs first; this catches the paraphrased
# case ("bed recipe" → "crafting bed" → "minecraft bed").
#
# _MAX_CONSECUTIVE_QUERY_CALLS: how many read-only queries the model may
# emit IN A ROW before we bail. Resets on any non-query (action) command,
# so legitimate alternating chains like searchForEntity → attack →
# searchForEntity → attack (the JS bed-craft pattern, ~11 iterations) are
# not penalised. This is the primary loop guard.
#
# _MAX_TOOL_CALLS_PER_TURN: absolute backstop on total tool-call attempts
# within one user turn. Set generously above the JS empirical floor of 19
# iterations so genuine multi-step crafting completes; this only fires
# against truly runaway models.
_MAX_REPEATED_QUERY_PER_NAME = 3
_MAX_CONSECUTIVE_QUERY_CALLS = 4
_MAX_TOOL_CALLS_PER_TURN = 25


@dataclass
class PendingTask:
    """Tracks a failed command so the bot can resume with player guidance."""

    command_name: str
    command_args: list[Any]
    command_kwargs: dict[str, Any] | None
    goal: str          # what the player originally asked for
    player_name: str   # who to follow for guidance
    failed_reason: str  # why the command failed


@dataclass
class ActiveTaskEvent:
    """One state transition or command result inside an active task."""

    kind: str
    message: str
    ok: bool | None = None


@dataclass
class ActiveTask:
    """Lightweight single-task tracker for one player request."""

    id: str
    requester: str
    original_message: str
    status: str = "running"
    attempts: int = 0
    failures: int = 0
    events: list[ActiveTaskEvent] | None = None
    summary: str = ""
    requires_ctable: bool = False
    timeout_s: float | None = None  # task-level timeout (seconds), None = no limit
    started_at: float = 0.0

    def __post_init__(self) -> None:
        if self.events is None:
            self.events = []


# ── Helpers ──────────────────────────────────────────────────────────


def _looks_like_crafting_request(message: str) -> bool:
    """Heuristic: does this message ask to craft an item that likely needs a table?"""
    text = message.strip().lower()
    return bool(re.search(r"\b(craft|make|build|制作|合成|建造|做)\b", text))


def _is_hard_failure(result: str) -> bool:
    """Return True for failures the LLM cannot recover from by retrying.

    Soft failures ("no oak_log nearby", "player not found") are fed back
    to the LLM so it can replan with a different approach.  Hard failures
    (command does not exist, bridge error) require player intervention.
    """
    hard_patterns = [
        "does not exist",
        "BridgeError",
        "Cannot read properties of undefined",
        "offhand",
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
    """Prompt nudge when the model produces reasoning-only output.

    Worded to cover both modes: tool-calling endpoints read this as
    "emit a function call", plain-text endpoints read it as "use the
    !commandName(args) syntax".
    """
    return (
        "You must use a command to respond to this request — either emit "
        "a tool call/function call OR use the !commandName(args) syntax in your "
        "text reply (e.g. !craftRecipe(\"oak_planks\", 1) or "
        "!searchForEntity(\"sheep\", 64)). Do NOT respond with plain text "
        "alone. If you are unsure which command fits, call !help or !stats "
        "first."
    )


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


def _action_succeeded(result: Any) -> bool:
    if result is None:
        return False
    text = str(result).strip()
    if not text:
        return False
    return not (text.startswith("FAILED:") or text.startswith("SKIPPED:"))


def _delivery_succeeded(result: Any) -> bool:
    if not _action_succeeded(result):
        return False
    match = _DELIVERY_RESULT_RE.search(str(result))
    if match is None:
        return True
    return int(match.group("count")) > 0


_NEARBY_MISS_RE = re.compile(
    r"No\s+(?P<target>[A-Za-z0-9_]+)\s+nearby\s+\((?P<range>\d+)\s+block radius\)",
    re.IGNORECASE,
)

_ITEM_REQUEST_RE = re.compile(
    r"\b(?:get|collect|gather|fetch|bring|give|find|search|mine|eat|consume)\b|"
    r"\b(?:want|need|would\s+like)\s+(?:to\s+)?(?:eat|get|have|consume)\b|"
    r"(?:给我|拿|获取|采集|收集|找|寻找|挖|挖一点|采一点)",
    re.IGNORECASE,
)
_ITEM_ALIAS_SOURCES: dict[str, tuple[str, str | None]] = {
    "wood": ("oak_log", "oak_log"),
    "log": ("oak_log", "oak_log"),
    "logs": ("oak_log", "oak_log"),
    "oak": ("oak_log", "oak_log"),
    "oak_log": ("oak_log", "oak_log"),
    "coal": ("coal", "coal_ore"),
    "coal_ore": ("coal", "coal_ore"),
    "stone": ("cobblestone", "stone"),
    "cobblestone": ("cobblestone", "cobblestone"),
    "dirt": ("dirt", "dirt"),
    "torch": ("torch", None),
    "torches": ("torch", None),
    "apple": ("apple", None),
    "apples": ("apple", None),
    "pork": ("porkchop", "pig"),
    "porkchop": ("porkchop", "pig"),
    "porkchops": ("porkchop", "pig"),
    "raw_porkchop": ("porkchop", "pig"),
    "cooked_porkchop": ("cooked_porkchop", "pig"),
    "beef": ("beef", "cow"),
    "steak": ("cooked_beef", "cow"),
    "mutton": ("mutton", "sheep"),
    "chicken": ("chicken", "chicken"),
    "木头": ("oak_log", "oak_log"),
    "木": ("oak_log", "oak_log"),
    "原木": ("oak_log", "oak_log"),
    "橡木": ("oak_log", "oak_log"),
    "煤": ("coal", "coal_ore"),
    "煤炭": ("coal", "coal_ore"),
    "煤矿": ("coal", "coal_ore"),
    "石头": ("cobblestone", "stone"),
    "圆石": ("cobblestone", "cobblestone"),
    "泥土": ("dirt", "dirt"),
    "土": ("dirt", "dirt"),
    "火把": ("torch", None),
    "苹果": ("apple", None),
    "猪肉": ("porkchop", "pig"),
    "熟猪肉": ("cooked_porkchop", "pig"),
    "牛肉": ("beef", "cow"),
    "羊肉": ("mutton", "sheep"),
    "鸡肉": ("chicken", "chicken"),
}
_CHINESE_NUMBERS: dict[str, int] = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_CRAFT_ALIASES: dict[str, str] = {
    "torch": "torch",
    "torches": "torch",
    "stick": "stick",
    "sticks": "stick",
    "wooden shovel": "wooden_shovel",
    "wooden_shovel": "wooden_shovel",
    "wooden axe": "wooden_axe",
    "wooden_axe": "wooden_axe",
    "wooden pickaxe": "wooden_pickaxe",
    "wooden_pickaxe": "wooden_pickaxe",
    "crafting table": "crafting_table",
    "crafting_table": "crafting_table",
    "木铲子": "wooden_shovel",
    "木斧头": "wooden_axe",
    "木镐": "wooden_pickaxe",
    "火把": "torch",
    "工作台": "crafting_table",
}
_COOK_ALIASES: dict[str, tuple[str, str]] = {
    "pork": ("porkchop", "cooked_porkchop"),
    "porkchop": ("porkchop", "cooked_porkchop"),
    "porkchops": ("porkchop", "cooked_porkchop"),
    "raw_porkchop": ("porkchop", "cooked_porkchop"),
    "猪肉": ("porkchop", "cooked_porkchop"),
    "beef": ("beef", "cooked_beef"),
    "steak": ("beef", "cooked_beef"),
    "牛肉": ("beef", "cooked_beef"),
    "mutton": ("mutton", "cooked_mutton"),
    "羊肉": ("mutton", "cooked_mutton"),
    "chicken": ("chicken", "cooked_chicken"),
    "鸡肉": ("chicken", "cooked_chicken"),
    "rabbit": ("rabbit", "cooked_rabbit"),
    "cod": ("cod", "cooked_cod"),
    "salmon": ("salmon", "cooked_salmon"),
}


@dataclass(frozen=True)
class ItemRequest:
    item_name: str
    source_block: str | None
    quantity: int = 8


@dataclass(frozen=True)
class CraftRequest:
    item_name: str
    quantity: int = 1
    additive: bool = False


@dataclass(frozen=True)
class CookRequest:
    raw_item: str
    cooked_item: str
    quantity: int | None = None


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


def _parse_item_request(message: str) -> ItemRequest | None:
    text = message.strip().lower()
    if not _ITEM_REQUEST_RE.search(text):
        return None
    if re.search(r"\bcraft\b|制作|合成", text):
        return None

    quantity = _quantity_from_item_request(text)
    for alias in sorted(_ITEM_ALIAS_SOURCES, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])", text):
            item_name, source_block = _ITEM_ALIAS_SOURCES[alias]
            return ItemRequest(item_name=item_name, source_block=source_block, quantity=quantity)
    return None


def _parse_craft_request(message: str) -> CraftRequest | None:
    text = message.strip().lower()
    if not re.search(r"\b(craft|make|build)\b|制作|合成|做", text):
        return None

    quantity = _quantity_from_craft_request(text)
    additive = bool(re.search(r"\b(more|extra|additional|another)\b|再|额外|更多", text))
    for alias in sorted(_CRAFT_ALIASES, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])", text):
            return CraftRequest(
                item_name=_CRAFT_ALIASES[alias],
                quantity=quantity,
                additive=additive,
            )
    return None


def _parse_cook_request(message: str) -> CookRequest | None:
    text = message.strip().lower()
    if not re.search(r"\b(cook|smelt|roast|bake)\b|烤|煮|烧", text):
        return None

    quantity = _optional_quantity_from_request(text)
    for alias in sorted(_COOK_ALIASES, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])", text):
            raw_item, cooked_item = _COOK_ALIASES[alias]
            return CookRequest(raw_item=raw_item, cooked_item=cooked_item, quantity=quantity)
    return None


def _cook_request_is_compound(message: str) -> bool:
    """Return true when cooking is only one step of a larger gather/kill flow."""
    if _parse_cook_request(message) is None:
        return False
    text = message.strip().lower()
    return bool(
        re.search(
            r"\b("
            r"pick\s*up|pickup|grab|take|get|collect|gather|fetch|find|search|"
            r"hunt|kill|attack"
            r")\b|捡|拾取|拿|找|寻找|收集|杀|打|猎|附近|旁边|nearby",
            text,
        )
    )


def _memory_record_is_failed_task(record: Any) -> bool:
    tags = {str(tag).lower() for tag in getattr(record, "tags", []) or []}
    if "task_failed" in tags:
        return True
    body = str(getattr(record, "body", "") or "").lower()
    description = str(getattr(record, "description", "") or "").lower()
    return "**status:** failed" in body or "status: failed" in body or "failed" in description


def _optional_quantity_from_request(text: str) -> int | None:
    digit = re.search(r"\b(\d{1,2})\b", text)
    if digit:
        return max(1, min(int(digit.group(1)), 64))
    for word, value in _CHINESE_NUMBERS.items():
        if word in text:
            return value
    if re.search(r"\b(a|an|one)\b|一个|一块|一根", text):
        return 1
    return None


def _quantity_from_craft_request(text: str) -> int:
    digit = re.search(r"\b(\d{1,2})\b", text)
    if digit:
        return max(1, min(int(digit.group(1)), 64))
    for word, value in _CHINESE_NUMBERS.items():
        if word in text:
            return value
    if re.search(r"\b(a|an|one)\b|一个|一块|一根", text):
        return 1
    return 1


def _quantity_from_item_request(text: str) -> int:
    digit = re.search(r"\b(\d{1,2})\b", text)
    if digit:
        return max(1, min(int(digit.group(1)), 64))
    for word, value in _CHINESE_NUMBERS.items():
        if word in text:
            return value
    if re.search(r"\b(a|an|one)\b|一个|一块|一根|一点", text):
        return 1
    return 8


def _inventory_count_from_state(state: BotState, item_name: str) -> int:
    return sum(item.count for item in state.inventory if item.name == item_name)


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

        messages = self._msg_builder.build_messages(
            history=self._history,
            system_prompt=system_prompt,
        )
        logger.info(
            "Plan prompt built: system_chars=%d messages=%d history_turns=%d "
            "command_docs_chars=%d examples_chars=%d",
            len(system_prompt),
            len(messages),
            len(self._history),
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
        if self._router is not None:
            decision = await self._router.classify_async(message)
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
        if intent == InputIntent.ACTION_COMMAND:
            if _player_requested_stop(message):
                await self._finish_active_task(
                    "cancelled",
                    reason=f"{source} asked me to stop.",
                )
            else:
                await self._replace_active_task(source, message)

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

    # ── Plan loop (action / question / chitchat) ────────────────────

    async def _run_plan_loop(
        self, *, max_responses: int, use_tools: bool, turn_id: int | None = None,
    ) -> bool:
        """Drive plan → parse → execute repeats for tool-capable intents.

        For ``use_tools=False`` (question/chitchat) the LLM is called with
        ``tool_choice="none"`` and the reply is spoken once, no commands.

        For ``use_tools=True`` (action_command) we use ``tool_choice="auto"``
        so the model can both reason in plain text AND emit tool calls in the
        same response — this matches the JS original's "I need wool. !attack
        ..." pattern, which is what carried weaker models through the bed-craft
        trace. The F2 retry path catches the case where a model produces text
        only without a tool call, re-prompting once with a reminder.
        """
        used_command = False
        if turn_id is None:
            turn_id = self._turn_id
        bot_state = await self.observe()
        if not self._running:
            return False
        if turn_id != self._turn_id:
            logger.info(
                "Plan loop superseded before start: turn=%s current=%s",
                turn_id,
                self._turn_id,
            )
            return False
        dispatch_decision: DispatchDecision | None = None
        if use_tools and self._dispatch_gate is not None:
            dispatch_decision = await self._dispatch_gate.classify(self._last_message, bot_state)
        if use_tools:
            if await self._maybe_complete_satisfied_craft(bot_state):
                return True
            if self._dispatch_allows_shortcut(dispatch_decision, "direct_action"):
                direct_result = await self._try_direct_action_command()
                if direct_result is not None:
                    return direct_result
            if self._dispatch_allows_shortcut(dispatch_decision, "direct_cook"):
                cook_result = await self._try_direct_cook_request(bot_state)
                if cook_result is not None:
                    return cook_result
            if self._direct_shortcut_retry_reason is None:
                if self._dispatch_allows_shortcut(dispatch_decision, "direct_item"):
                    item_result = await self._try_direct_item_request(bot_state)
                    if item_result is not None:
                        return item_result
            if self._direct_shortcut_retry_reason is None:
                if self._dispatch_allows_shortcut(dispatch_decision, "skill"):
                    skill_used = await self._try_phase5a_skill(self._last_message, bot_state)
                    if skill_used:
                        return True
            if self._direct_shortcut_retry_reason is not None:
                logger.info(
                    "Limiting LLM recovery after direct shortcut failure to one response: %s",
                    self._direct_shortcut_retry_reason,
                )
                max_responses = min(max_responses, 1)
                self._direct_shortcut_retry_reason = None
        tool_choice = "auto" if use_tools else "none"
        consecutive_failures = 0
        last_failed: tuple[str, str] | None = None  # (command, args_str)
        no_tool_retries = 0  # F2: track re-prompts when LLM omits tool calls
        seen_query_results: dict[str, str] = {}
        warned_repeated_queries: set[str] = set()
        # F1: count calls to each query command by name, regardless of args
        # — catches the paraphrased-query loop case (searchWiki "bed recipe"
        # → "crafting bed" → "minecraft bed") that the exact-arg check above
        # misses.
        query_command_counts: dict[str, int] = {}
        warned_query_command_loops: set[str] = set()
        nearby_miss_recoveries: set[tuple[str, str]] = set()
        # F3a: consecutive read-only query calls — the *primary* loop guard.
        # Resets on any non-query (action) command, so legitimate chains
        # like searchForEntity → attack → searchForEntity → attack are
        # never penalised.  Targets the actual failure mode (query spam).
        consecutive_query_calls = 0
        # F3b: absolute backstop on total tool-call attempts per user turn.
        # Set high (matches JS empirical iteration count + headroom) so
        # genuine multi-step crafting (~19 iterations on bed-craft) is not
        # cut off; only truly runaway models hit this.
        total_tool_calls = 0

        for _ in range(max_responses):
            if turn_id != self._turn_id:
                logger.info("Plan loop superseded: turn=%s current=%s", turn_id, self._turn_id)
                return used_command
            if self._shut_up or not self._running:
                break
            if self._safety and self._safety.state.interrupted:
                logger.warning("Plan loop halted: safety interrupt active")
                break
            if self._active_task and self._active_task.timeout_s is not None:
                elapsed = time.time() - self._active_task.started_at
                if elapsed > self._active_task.timeout_s:
                    logger.warning(
                        "Task %s timed out after %.0fs (limit %.0fs)",
                        self._active_task.id,
                        elapsed,
                        self._active_task.timeout_s,
                    )
                    await self._send_chat(
                        f"I've been working on this for {int(elapsed)}s — "
                        "that's too long. What should I do differently?"
                    )
                    await self._finish_active_task(
                        "failed",
                        reason=f"Task timed out after {elapsed:.0f}s",
                    )
                    return False
            if use_tools and await self._maybe_complete_satisfied_craft(bot_state):
                return True

            # Flush behavior log so the LLM sees what modes have been doing
            # (mirrors JS behavior_log flush before each promptConvo call).
            b_log = self._modes.flush_behavior_log()
            if b_log:
                self._history.append(
                    ConversationTurn(role="system", content=f"Recent behaviors:\n{b_log}"),
                )

            llm_response = await self.plan(bot_state, tool_choice=tool_choice)
            if turn_id != self._turn_id:
                logger.info(
                    "Dropping stale LLM response: turn=%s current=%s",
                    turn_id,
                    self._turn_id,
                )
                return used_command

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
                    "LLM repeated query command %s after reminder — attempting fallback",
                    normalized_name,
                )
                fallback_result = await self._fallback_after_repeated_query(
                    normalized_name,
                    bot_state,
                    seen_query_results.get(cmd_key, ""),
                )
                if fallback_result is not None:
                    return fallback_result or used_command
                await self._send_chat(
                    "I'm stuck repeating the same check instead of taking the next step."
                )
                return used_command

            # F1: count by command name only — catches paraphrased-arg loops
            # that the exact-arg check above doesn't catch.
            query_loop_name: str | None = None
            if use_tools:
                for cmd in parsed.commands:
                    normalized = cmd.name.lstrip("!")
                    if normalized in _QUERY_COMMANDS:
                        query_command_counts[normalized] = (
                            query_command_counts.get(normalized, 0) + 1
                        )
                        if (
                            query_command_counts[normalized]
                            >= _MAX_REPEATED_QUERY_PER_NAME
                        ):
                            query_loop_name = normalized
                            break

            if query_loop_name is not None:
                count = query_command_counts[query_loop_name]
                if query_loop_name not in warned_query_command_loops:
                    warned_query_command_loops.add(query_loop_name)
                    logger.warning(
                        "LLM called %s %d times in this turn (different args) — "
                        "forcing pivot to concrete action",
                        query_loop_name, count,
                    )
                    self._history.append(ConversationTurn(
                        role="system",
                        content=(
                            f"You have already called {query_loop_name} {count} "
                            "times in this turn with different arguments. You "
                            "have enough information. Choose a concrete next "
                            "action NOW: craftRecipe, collectBlocks, "
                            "searchForBlock, searchForEntity, or chat back to "
                            "the player to ask for help. Do NOT call any "
                            "read-only query command again this turn."
                        ),
                    ))
                    continue
                logger.warning(
                    "LLM stuck on %s after pivot reminder — ending turn",
                    query_loop_name,
                )
                await self._send_chat(
                    f"I keep looking up '{query_loop_name}' instead of acting. "
                    "Can you tell me what to do next?"
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

            any_blocked = False
            for cmd in parsed.commands:
                if turn_id != self._turn_id:
                    logger.info(
                        "Command chain superseded: turn=%s current=%s",
                        turn_id,
                        self._turn_id,
                    )
                    return used_command
                if self._shut_up:
                    break
                if self._safety and self._safety.state.interrupted:
                    logger.warning("Command chain halted: safety interrupt")
                    break

                # F3a: track consecutive read-only queries. Resets to 0 on
                # any non-query command, so the JS bed-craft pattern
                # (search → attack → search → attack, ~11 iterations) is
                # never penalised. Catches the real failure mode: pure
                # query spam without any concrete action in between.
                cmd_name_normalized = cmd.name.lstrip("!")
                if cmd_name_normalized in _QUERY_COMMANDS:
                    consecutive_query_calls += 1
                    if consecutive_query_calls > _MAX_CONSECUTIVE_QUERY_CALLS:
                        logger.warning(
                            "Consecutive-query cap (%d) hit — bot is stuck "
                            "in read-only loop; ending turn",
                            _MAX_CONSECUTIVE_QUERY_CALLS,
                        )
                        await self._send_chat(
                            f"I've looked things up {consecutive_query_calls} "
                            "times in a row without doing anything. What "
                            "should I actually do?"
                        )
                        return used_command
                else:
                    consecutive_query_calls = 0

                # F3b: absolute total backstop. Counts everything; only
                # fires against runaway models. Set above JS's 19-iter
                # bed-craft floor so genuine long tasks complete.
                total_tool_calls += 1
                if total_tool_calls > _MAX_TOOL_CALLS_PER_TURN:
                    logger.warning(
                        "Total tool-call backstop (%d) reached — ending turn",
                        _MAX_TOOL_CALLS_PER_TURN,
                    )
                    recent_names = [a["name"] for a in self._recent_actions]
                    await self._send_chat(
                        f"I've tried {_MAX_TOOL_CALLS_PER_TURN} steps without "
                        f"finishing — most recently {', '.join(recent_names[-3:]) or cmd.name}. "
                        "Could you tell me what to try next?"
                    )
                    return used_command

                # Announce each step before executing
                cmd_args = cmd.args
                cmd_kwargs = getattr(cmd, "kwargs", None) or None
                cmd_key = _command_call_key(cmd.name, cmd_args, cmd_kwargs)
                normalized_name = cmd.name.lstrip("!")
                blocked_noop = self._blocked_model_control_command(normalized_name)
                if blocked_noop is not None:
                    logger.warning(
                        "Blocked model-generated control command: %s result=%s",
                        normalized_name,
                        blocked_noop,
                    )
                    self._history.append(ConversationTurn(role="system", content=blocked_noop))
                    self._active_task_add_event(
                        "blocked_command",
                        f"{normalized_name}: {blocked_noop}",
                        ok=False,
                    )
                    any_blocked = True
                    continue
                action_desc = _describe_action(
                    cmd.name,
                    cmd_args if cmd_args else list((cmd_kwargs or {}).values()),
                )
                logger.info(
                    "Bot action step start: command=%s args=%s kwargs=%s desc=%s",
                    cmd.name,
                    cmd_args,
                    cmd_kwargs or {},
                    action_desc,
                )
                await self._send_chat(action_desc)

                action_started = time.perf_counter()
                result = await self._execute_agent_command(cmd.name, cmd_args, cmd_kwargs)
                action_elapsed_ms = (time.perf_counter() - action_started) * 1000
                logger.info(
                    "Bot action step result: command=%s elapsed_ms=%.1f result=%s",
                    cmd.name,
                    action_elapsed_ms,
                    _log_preview(result),
                )
                used_command = True
                self._record_action(cmd.name, cmd_args, result)
                self._active_task_add_event(
                    "action_result",
                    f"{cmd.name} args={cmd_args} kwargs={cmd_kwargs or {}} -> {result}",
                    ok=_action_succeeded(result),
                )
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
                    await self._finish_active_task("failed", reason=result)
                    return used_command

                if is_fail:
                    if await self._recover_nearby_miss(
                        normalized_name,
                        cmd_args,
                        cmd_kwargs,
                        result,
                        nearby_miss_recoveries,
                    ):
                        consecutive_failures = 0
                        last_failed = None
                        bot_state = await self.observe()
                        if not self._running:
                            return used_command
                        continue

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
                        await self._finish_active_task(
                            "failed",
                            reason="3 consecutive action failures.",
                        )
                        return used_command
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
                if not self._running:
                    return used_command
                if use_tools and await self._maybe_complete_satisfied_craft(bot_state):
                    return True

            # If the only command(s) this turn were blocked (e.g. model
            # proposed !stop without the player asking), give the LLM another
            # chance instead of immediately failing the task.
            if any_blocked and not used_command:
                continue

        self._history = self._convo_mgr.compress_history(self._history)
        if use_tools and self._active_task is not None:
            recovered_after_clear = await self._maybe_retry_cook_after_furnace_clear()
            if recovered_after_clear:
                return True
            await self._finish_active_task(
                "succeeded" if used_command else "failed",
                reason="Plan loop ended.",
            )
        return used_command

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
                },
            ),
        ]:
            result = await self._execute_agent_command(command, [], kwargs)
            self._record_action(command, [], result)
            ok = (
                _delivery_succeeded(result)
                if command == "givePlayer" else _action_succeeded(result)
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
                "direct": reason,
            })
            await self._send_chat(f"Done: {result}")
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

        if re.fullmatch(r"(come\s+to\s+me|come\s+here|get\s+over\s+here|come)", text):
            command = "goToPlayer"
            kwargs = {"player_name": self._last_sender, "closeness": 3}
            preface = "On my way."
        elif re.fullmatch(r"(follow\s+me|follow|escort\s+me)", text):
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
        result = await self._execute_agent_command(command, [], kwargs)
        self._record_action(command, [], result)
        ok = _action_succeeded(result)
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

            smelt_result = await self._execute_agent_command(
                "smeltItem",
                [],
                {"item_name": request.raw_item, "num": remaining_to_cook},
            )
            self._record_action("smeltItem", [], smelt_result)
            smelt_ok = _action_succeeded(smelt_result)
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
        search_result = await self._execute_agent_command(
            "searchForBlock",
            [],
            {"block_type": "furnace", "search_range": 64},
        )
        self._record_action("searchForBlock", [], search_result)
        search_ok = _action_succeeded(search_result)
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
            craft_result = await self._execute_agent_command(
                "craftRecipe",
                [],
                {"recipe_name": "furnace", "num": 1},
            )
            self._record_action("craftRecipe", [], craft_result)
            craft_ok = _action_succeeded(craft_result)
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

        place_result = await self._execute_agent_command(
            "placeHere",
            [],
            {"block_type": "furnace"},
        )
        self._record_action("placeHere", [], place_result)
        place_ok = _action_succeeded(place_result)
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
        await self._send_chat(
            f"I'll get {request.quantity} {request.item_name} and bring it to you."
        )

        have = _inventory_count_from_state(bot_state, request.item_name)
        self._active_task_add_event(
            "inventory_check",
            f"Have {have} {request.item_name}; need {request.quantity}.",
            ok=have >= request.quantity,
        )
        if have < request.quantity:
            missing = request.quantity - have
            if request.source_block is None:
                await self._send_chat(
                    f"I don't know a direct block source for {request.item_name} yet."
                )
                await self._finish_active_task(
                    "failed",
                    reason=f"No direct source known for {request.item_name}.",
                )
                return True

            for command, kwargs in [
                *self._gather_steps_for_item_request(request.source_block, missing),
            ]:
                result = await self._execute_agent_command(command, [], kwargs)
                self._record_action(command, [], result)
                ok = _action_succeeded(result)
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
                    await self._finish_active_task("failed", reason=str(result or ""))
                    return True

        final_ok = True
        for command, kwargs in [
            ("goToPlayer", {"player_name": self._last_sender, "closeness": 3}),
            (
                "givePlayer",
                {
                    "player_name": self._last_sender,
                    "item_name": request.item_name,
                    "num": request.quantity,
                },
            ),
        ]:
            result = await self._execute_agent_command(command, [], kwargs)
            self._record_action(command, [], result)
            ok = (
                _delivery_succeeded(result)
                if command == "givePlayer" else _action_succeeded(result)
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
        match = _NEARBY_MISS_RE.search(result)
        if match is None:
            return False

        target = self._target_from_failed_search(command_name, args, kwargs)
        if not target:
            target = match.group("target")
        key = (command_name, target)
        if key in seen:
            return False
        seen.add(key)

        if command_name in {"collectBlocks", "searchForBlock"}:
            search_command = "searchForBlock"
            search_kwargs = {"block_type": target, "search_range": 64}
        elif command_name == "searchForEntity":
            search_command = "searchForEntity"
            search_kwargs = {"entity_type": target, "search_range": 64}
        else:
            return False

        logger.info(
            "Nearby miss recovery: command=%s target=%s player=%s",
            command_name,
            target,
            self._last_sender,
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

        recovery_steps = [
            ("goToPlayer", [], {"player_name": self._last_sender, "closeness": 3}),
            (search_command, [], search_kwargs),
        ]
        for recover_command, recover_args, recover_kwargs in recovery_steps:
            action_desc = _describe_action(
                recover_command,
                recover_args if recover_args else list(recover_kwargs.values()),
            )
            await self._send_chat(action_desc)
            recover_result = await self._execute_agent_command(
                recover_command,
                recover_args,
                recover_kwargs,
            )
            self._record_action(recover_command, recover_args, recover_result)
            recover_ok = _action_succeeded(recover_result)
            self._active_task_add_event(
                "action_result",
                f"{recover_command} kwargs={recover_kwargs} -> {recover_result}",
                ok=recover_ok,
            )
            await self._record_event("action_result", {
                "command": recover_command,
                "args": recover_args,
                "kwargs": recover_kwargs,
                "result": recover_result,
                "recovery": "nearby_miss_return_to_player",
            })
            await self._send_chat(f"Done: {recover_result}")
            self._history.append(ConversationTurn(
                role="system",
                content=str(recover_result or ""),
            ))
            if not recover_ok:
                break
        return True

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
        plan = await self._execute_agent_command(
            "getCraftingPlan",
            [],
            {"target_item": target, "quantity": 1},
        )
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
            result = await self._execute_agent_command(command, args, kwargs)
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
            result = await self._execute_agent_command(step.command, step.args, step.kwargs)
            self._record_action(step.command, step.args, result)
            await self._record_event("action_result", {
                "command": step.command,
                "args": step.args,
                "kwargs": step.kwargs,
                "result": result,
                "skill": skill.name,
            })
            if not _action_succeeded(result):
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
        if name == "getCraftingPlan":
            target = str((kwargs or {}).get("target_item", values[0] if values else ""))
            quantity = int((kwargs or {}).get("quantity", values[1] if len(values) > 1 else 1) or 1)
            kg_plan = await self._get_knowledge_crafting_plan(target, quantity)
            if kg_plan is not None:
                return kg_plan
        preflight_result = await self._preflight_action_with_observation(name, args, kwargs)
        if preflight_result is not None:
            return preflight_result
        if name in {"goToCoordinates", "moveTo"}:
            coord_error = self._validate_coordinates_before_move(args, kwargs)
            if coord_error:
                return f"FAILED: {coord_error}"
        if name == "craftRecipe":
            dye_hint = await self._bed_dye_misconception_hint(args, kwargs)
            if dye_hint:
                return dye_hint
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
        if command_name not in _PRE_ACTION_OBSERVE_COMMANDS:
            return None

        was_running = self._running
        state = await self.observe()
        if was_running and not self._running:
            return "FAILED: observe() failed before action."

        nearby = set(state.nearby_block_types)
        if command_name == "placeHere":
            block_type = str((kwargs or {}).get("block_type", args[0] if args else "")).strip()
            if block_type in _REUSABLE_WORKSTATIONS and block_type in nearby:
                return (
                    f"SKIPPED: {block_type} is already nearby. "
                    f"Use the existing {block_type} instead of placing another one."
                )

        if command_name == "craftRecipe":
            recipe_name = str((kwargs or {}).get("recipe_name", args[0] if args else "")).strip()
            if recipe_name == "furnace" and "furnace" in nearby:
                return (
                    "SKIPPED: furnace is already nearby. "
                    "Use the existing furnace instead of crafting another one."
                )

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

    async def _replace_active_task(self, requester: str, message: str) -> None:
        """Start a new lightweight task, cancelling any unfinished one."""
        await self._finish_active_task("cancelled", reason="Replaced by a newer player command.")
        task_id = f"{int(time.time())}_{_slugify_task_name(message)}"
        self._active_task = ActiveTask(
            id=task_id,
            requester=requester,
            original_message=message,
            requires_ctable=_looks_like_crafting_request(message),
            timeout_s=300.0,  # 5 min default, mirrors Minecollab benchmark
            started_at=time.time(),
        )
        self._active_task.events.append(ActiveTaskEvent(
            kind="start",
            message=f"{requester}: {message}",
        ))
        logger.info("ActiveTask start: id=%s requester=%s message=%r", task_id, requester, message)
        await self._record_event("task_start", {
            "task_id": task_id,
            "requester": requester,
            "message": message,
        })

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

    async def _finish_active_task(self, status: str, *, reason: str = "") -> None:
        """Summarise, write memory, then clear the current active task."""
        task = self._active_task
        if task is None or task.status != "running":
            return
        if status == "succeeded" and not self._active_task_has_delivery(task):
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
        })
        await self._remember_active_task(task)
        self._active_task = None

    @staticmethod
    def _active_task_has_delivery(task: ActiveTask) -> bool:
        return any(
            event.kind == "action_result"
            and ("givePlayer" in event.message or "give_to_player" in event.message)
            and event.ok is not False
            for event in (task.events or [])
        )

    @staticmethod
    def _active_task_expects_delivery(task: ActiveTask) -> bool:
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
        return (
            "ACTIVE TASK\n"
            f"id: {task.id}\n"
            f"requester: {task.requester}\n"
            f"request: {task.original_message}\n"
            f"attempts: {task.attempts}\n"
            f"failures: {task.failures}\n"
            "recent task events:\n"
            f"{self._active_task_event_lines(task, limit=6)}\n"
            "Task boundary: complete only this request from the requester. "
            "Do not resume older memory tasks or adjacent goals unless they are "
            "strictly required by this request. Failed memories are lessons, not "
            "pending objectives. Use recent task events to avoid repeating "
            "completed or failed steps."
        )

    async def _remember_active_task(self, task: ActiveTask) -> None:
        if self._memory is None:
            return
        record = MemoryRecord(
            name=f"task_{task.id}",
            description=task.summary[:150],
            type=MemoryType.EPISODIC,
            scope=Scope.WORLD,
            confidence=0.75 if task.status == "succeeded" else 0.65,
            source="llm_reflected",
            tags=[
                "active_task",
                f"task_{task.status}",
                f"requester_{_slugify_task_name(task.requester, limit=24)}",
            ],
            body=(
                f"**Request:** {task.requester}: {task.original_message}\n\n"
                f"**Status:** {task.status}\n\n"
                f"**Attempts:** {task.attempts}\n\n"
                f"**Failures:** {task.failures}\n\n"
                f"**Summary:** {task.summary}\n\n"
                f"**Events:**\n{self._active_task_event_lines(task, limit=20)}\n"
            ),
        )
        try:
            await self._memory.remember(record)
        except Exception:
            logger.exception("ActiveTask memory write failed")

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

        # Let the Mineflayer side decide whether a crafting table in inventory
        # or nearby is usable. The cheap nearby-block snapshot is not reliable
        # enough to block otherwise valid 3x3 crafts here.
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
        "countEntities": CommandDef(
            name="countEntities",
            description="Count nearby entities of a given type within a range.",
            params={
                "entity_type": {"type": "string", "description": "Entity type, e.g. chicken"},
                "range": {"type": "int", "description": "Search range in blocks, max 64"},
            },
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
            description=(
                "Attack and kill the nearest entity of the given type. "
                "Works on hostile mobs (zombie, skeleton, creeper) AND on "
                "passive mobs you need to harvest (sheep for wool, cow for "
                "leather/beef, chicken for feathers/meat, pig for pork). "
                "Use this whenever you need a resource that drops from a "
                "mob — do NOT keep calling searchForEntity if the target is "
                "already nearby. Default search radius is 64 blocks."
            ),
            params={
                "entity_type": {
                    "type": "string",
                    "description": (
                        "Entity type, e.g. zombie, sheep, cow, chicken, pig"
                    ),
                },
                "range": {
                    "type": "float",
                    "description": "Search radius in blocks. Default: 64.",
                    "default": 64,
                },
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
                "num": {"type": "int", "description": "Number to give (default 8)"},
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
                "count": {"type": "int", "description": "Number to collect (default 8)"},
            },
        ),
        "breakNearbyBlocks": CommandDef(
            name="breakNearbyBlocks",
            description=(
                "Directly break nearby reachable fragile blocks without long pathfinding, "
                "then pick up drops. Prefer this over collectBlocks for tall_grass, "
                "short_grass, flowers, and other thin nearby blocks."
            ),
            params={
                "block_type": {"type": "string", "description": "Block type, e.g. tall_grass"},
                "count": {"type": "int", "description": "Number to break (default 1)"},
                "range": {
                    "type": "float",
                    "description": "Local search radius, max 16 (default 6)",
                },
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
