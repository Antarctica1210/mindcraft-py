"""
Phase 2 — Behavior mode state machine.

Reactive modes that run every tick: self-preservation, unstuck,
hunting, idle.  Modes are evaluated in priority order and may
interrupt lower-priority actions.

Mirrors `src/agent/modes.js`.
"""
from __future__ import annotations

import enum
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class BehaviorMode(enum.StrEnum):
    SELF_PRESERVATION = "self_preservation"
    UNSTUCK = "unstuck"
    COWARDICE = "cowardice"
    SELF_DEFENSE = "self_defense"
    HUNTING = "hunting"
    ITEM_COLLECTING = "item_collecting"
    TORCH_PLACING = "torch_placing"
    ELBOW_ROOM = "elbow_room"
    IDLE_STARING = "idle_staring"
    IDLE = "idle"
    CONVERSING = "conversing"
    CODING = "coding"


# Priority order — lower index = higher priority
MODE_PRIORITY: list[BehaviorMode] = [
    BehaviorMode.SELF_PRESERVATION,
    BehaviorMode.UNSTUCK,
    BehaviorMode.COWARDICE,
    BehaviorMode.SELF_DEFENSE,
    BehaviorMode.HUNTING,
    BehaviorMode.ITEM_COLLECTING,
    BehaviorMode.TORCH_PLACING,
    BehaviorMode.ELBOW_ROOM,
    BehaviorMode.IDLE_STARING,
    BehaviorMode.IDLE,
    BehaviorMode.CONVERSING,
    BehaviorMode.CODING,
]

# Modes that are safe to run without an explicit player command.  Everything
# else is opt-in via setMode so an idle bot does not wander, hunt, stare,
# collect random drops, or interrupt navigation.
DEFAULT_ENABLED_MODES: frozenset[BehaviorMode] = frozenset({
    BehaviorMode.SELF_PRESERVATION,
    BehaviorMode.UNSTUCK,
    BehaviorMode.COWARDICE,
    BehaviorMode.SELF_DEFENSE,
})


@dataclass
class ModeState:
    """Runtime state for a single behavior mode."""

    mode: BehaviorMode
    on: bool = True          # whether update() is called every tick
    active: bool = False     # whether an action has been triggered and hasn't finished
    paused: bool = False     # paused by another mode

    # Per-mode metrics
    _stuck_since: float = 0.0
    _prev_position: tuple[float, float, float] = (0, 0, 0)
    _logged_stuck: bool = False  # only log once per stuck episode
    _last_triggered: float = 0.0

    def frame(self, position: tuple[float, float, float]) -> None:
        """Called once per tick to update mode-specific state."""
        if position != self._prev_position:
            self._stuck_since = 0.0
            self._logged_stuck = False
        self._prev_position = position

    def on_cooldown(self, seconds: float) -> bool:
        return time.time() - self._last_triggered < seconds

    def mark_triggered(self) -> None:
        self._last_triggered = time.time()


class ModeEvaluator:
    """Evaluates behavior modes in priority order each tick."""

    def __init__(self) -> None:
        self._states: dict[BehaviorMode, ModeState] = {
            m: ModeState(mode=m, on=m in DEFAULT_ENABLED_MODES) for m in BehaviorMode
        }
        self._active_mode: BehaviorMode | None = None
        self._frame_count: int = 0
        self._behavior_log: list[str] = []

    def active_mode(self) -> BehaviorMode | None:
        return self._active_mode

    def set_enabled(self, mode_name: str, on: bool) -> str:
        """Enable or disable a behavior mode by JS-compatible name."""
        try:
            mode = BehaviorMode(mode_name)
        except ValueError:
            known = ", ".join(m.value for m in BehaviorMode)
            return f"FAILED: Unknown mode '{mode_name}'. Known modes: {known}"
        state = self._states[mode]
        state.on = on
        if not on and state.active:
            self.release(mode)
        status = "enabled" if on else "disabled"
        self.log_behavior(f"{mode.value} {status}")
        return f"Mode '{mode.value}' {status}."

    def status_text(self) -> str:
        """Return a concise status list for the !modes command."""
        parts = []
        for mode in MODE_PRIORITY:
            state = self._states[mode]
            marker = "active" if state.active else "on" if state.on else "off"
            parts.append(f"{mode.value}:{marker}")
        return "Modes: " + ", ".join(parts)

    def is_active(self, mode: BehaviorMode) -> bool:
        return self._active_mode == mode

    def log_behavior(self, message: str) -> None:
        """Record a behavior event for the next LLM call."""
        self._behavior_log.append(message)

    def flush_behavior_log(self) -> str:
        """Return and clear the behavior log buffer (mirrors JS flushBehaviorLog)."""
        if not self._behavior_log:
            return ""
        log = "\n".join(self._behavior_log)
        self._behavior_log.clear()
        return log

    def activate(self, mode: BehaviorMode) -> None:
        """Activate a mode, pausing lower-priority ones."""
        self._active_mode = mode
        state = self._states[mode]
        state.active = True
        state.paused = False
        self.log_behavior(f"{mode.value} activated")
        priority_idx = MODE_PRIORITY.index(mode)
        for i, m in enumerate(MODE_PRIORITY):
            if i > priority_idx:
                self._states[m].paused = True

    def release(self, mode: BehaviorMode) -> None:
        """Release a mode so lower-priority ones can resume."""
        self._states[mode].active = False
        if self._active_mode == mode:
            self._active_mode = None
        self.log_behavior(f"{mode.value} released")
        priority_idx = MODE_PRIORITY.index(mode)
        for i, m in enumerate(MODE_PRIORITY):
            if i > priority_idx:
                self._states[m].paused = False

    async def tick(
        self, bot_state: Any, bridge: Any
    ) -> BehaviorMode | None:
        """Evaluate all enabled modes in priority order."""
        self._frame_count += 1
        for mode in MODE_PRIORITY:
            state = self._states[mode]
            if not state.on or state.paused:
                continue
            if await self._evaluate(mode, bot_state, bridge):
                state.mark_triggered()
                self.log_behavior(f"{mode.value} triggered on tick {self._frame_count}")
                if state.active:
                    self.release(mode)
                return mode
        return None

    async def _evaluate(
        self, mode: BehaviorMode, bot_state: Any, bridge: Any
    ) -> bool:
        state = self._states[mode]
        state.frame((bot_state.position.x, bot_state.position.y, bot_state.position.z))
        if mode == BehaviorMode.SELF_PRESERVATION:
            return await self._eval_self_preservation(bot_state, bridge)
        elif mode == BehaviorMode.UNSTUCK:
            return await self._eval_unstuck(state, bot_state, bridge)
        elif mode == BehaviorMode.COWARDICE:
            return await self._eval_cowardice(state, bot_state, bridge)
        elif mode == BehaviorMode.SELF_DEFENSE:
            return await self._eval_self_defense(state, bot_state, bridge)
        elif mode == BehaviorMode.HUNTING:
            return await self._eval_hunting(state, bot_state, bridge)
        elif mode == BehaviorMode.ITEM_COLLECTING:
            return await self._eval_item_collecting(state, bot_state, bridge)
        elif mode == BehaviorMode.TORCH_PLACING:
            return await self._eval_torch_placing(state, bridge)
        elif mode == BehaviorMode.ELBOW_ROOM:
            return await self._eval_elbow_room(state, bot_state, bridge)
        elif mode == BehaviorMode.IDLE_STARING:
            return await self._eval_idle_staring(state, bot_state, bridge)
        elif mode in (BehaviorMode.IDLE, BehaviorMode.CONVERSING, BehaviorMode.CODING):
            return False
        return False

    async def _eval_self_preservation(self, bot_state: Any, bridge: Any) -> bool:
        # Use bridge.observe() to get current block info
        blocks = getattr(bot_state, "blocks_nearby", "")
        if "standing on: water" in blocks and bot_state.position.y > 0:
            try:
                await bridge.set_control_state("jump", True)
            except Exception:
                pass
            return True
        if "standing on: lava" in blocks or "standing on: fire" in blocks:
            logger.warning("Bot in lava/fire — self_preservation triggered")
            try:
                await bridge.move_away(8)
            except Exception:
                pass
            return True
        if bot_state.health < 5:
            logger.warning("Bot is dying — self_preservation triggered")
            try:
                await bridge.move_away(12)
            except Exception:
                pass
            return True
        return False

    async def _eval_unstuck(self, state: ModeState, bot_state: Any, bridge: Any) -> bool:
        current_goal = getattr(bot_state, "current_goal", None)
        if not current_goal or "No current" in str(current_goal):
            state._stuck_since = 0.0
            return False
        if state.on_cooldown(8):
            return False
        if state._stuck_since == 0.0:
            state._stuck_since = time.time()
            return False
        stuck_duration = time.time() - state._stuck_since
        if stuck_duration > 20:
            if not state._logged_stuck:
                state._logged_stuck = True
                logger.warning("Bot stuck for %.0fs — unstuck triggered", stuck_duration)
            try:
                await bridge.set_control_state("jump", True)
                await bridge.move_away(5)
            except Exception:
                pass
            self.activate(BehaviorMode.UNSTUCK)
            return True
        return False

    async def _eval_cowardice(self, state: ModeState, bot_state: Any, bridge: Any) -> bool:
        if bot_state.health >= 10 or state.on_cooldown(5):
            return False
        hostile = _nearest_named(bot_state, HOSTILE_NAMES)
        if hostile is None:
            return False
        try:
            await bridge.move_away(16)
        except Exception:
            pass
        self.activate(BehaviorMode.COWARDICE)
        self.log_behavior(f"cowardice: fleeing from {hostile}")
        return True

    async def _eval_self_defense(self, state: ModeState, bot_state: Any, bridge: Any) -> bool:
        if state.on_cooldown(3):
            return False
        hostile = _nearest_named(bot_state, HOSTILE_NAMES, max_distance=4)
        if hostile is None:
            return False
        try:
            await bridge.attack(hostile)
        except Exception:
            return False
        self.activate(BehaviorMode.SELF_DEFENSE)
        self.log_behavior(f"self_defense: attacking {hostile}")
        return True

    async def _eval_hunting(self, state: ModeState, bot_state: Any, bridge: Any) -> bool:
        if bot_state.food > 12 or state.on_cooldown(8):
            return False
        target = _nearest_named(bot_state, PASSIVE_FOOD_NAMES, max_distance=16)
        if target is None:
            return False
        try:
            await bridge.attack(target)
        except Exception:
            return False
        self.activate(BehaviorMode.HUNTING)
        self.log_behavior(f"hunting: attacking {target} for food")
        return True

    async def _eval_item_collecting(self, state: ModeState, bot_state: Any, bridge: Any) -> bool:
        if state.on_cooldown(10):
            return False
        if " item " not in f" {bot_state.entities_nearby.lower()} ":
            return False
        try:
            await bridge.pickup_nearby_items(8)
        except Exception:
            return False
        self.activate(BehaviorMode.ITEM_COLLECTING)
        self.log_behavior("item_collecting: picking up nearby dropped items")
        return True

    async def _eval_torch_placing(self, state: ModeState, bridge: Any) -> bool:
        if state.on_cooldown(20):
            return False
        try:
            state.mark_triggered()
            decision = await bridge.should_place_torch()
            if not isinstance(decision, dict) or not decision.get("should_place"):
                return False
            await bridge.place_torch()
        except Exception:
            return False
        self.activate(BehaviorMode.TORCH_PLACING)
        self.log_behavior("torch_placing: placed a torch nearby")
        return True

    async def _eval_elbow_room(self, state: ModeState, bot_state: Any, bridge: Any) -> bool:
        if state.on_cooldown(8):
            return False
        if not _player_too_close(bot_state.players_nearby):
            return False
        try:
            await bridge.move_away(3)
        except Exception:
            return False
        self.activate(BehaviorMode.ELBOW_ROOM)
        self.log_behavior("elbow_room: moving away from a nearby player")
        return True

    async def _eval_idle_staring(self, state: ModeState, bot_state: Any, bridge: Any) -> bool:
        if bot_state.current_goal and "No current" not in bot_state.current_goal:
            return False
        if state.on_cooldown(15):
            return False
        if random.random() > 0.02:
            return False
        pos = bot_state.position
        try:
            await bridge.look_at_position(
                pos.x + random.uniform(-8, 8),
                pos.y + random.uniform(0, 3),
                pos.z + random.uniform(-8, 8),
            )
        except Exception:
            return False
        self.activate(BehaviorMode.IDLE_STARING)
        self.release(BehaviorMode.IDLE_STARING)
        return True


HOSTILE_NAMES: set[str] = {
    "blaze", "bogged", "breeze", "cave_spider", "creeper", "drowned",
    "elder_guardian", "enderman", "endermite", "evoker", "ghast", "guardian",
    "hoglin", "husk", "magma_cube", "phantom", "piglin", "piglin_brute",
    "pillager", "ravager", "shulker", "silverfish", "skeleton", "slime",
    "spider", "stray", "vex", "vindicator", "warden", "witch",
    "wither_skeleton", "zoglin", "zombie", "zombie_villager", "zombified_piglin",
}

PASSIVE_FOOD_NAMES: set[str] = {
    "chicken", "cow", "pig", "rabbit", "sheep", "cod", "salmon",
}


def _nearest_named(
    bot_state: Any, names: set[str], max_distance: float | None = None,
) -> str | None:
    """Pick the nearest matching entity from structured or text observations."""
    structured = getattr(bot_state, "entities", []) or []
    matches: list[tuple[float, str]] = []
    for entity in structured:
        name = getattr(entity, "name", "") or getattr(entity, "entity_type", "")
        norm = name.lower()
        dist = float(getattr(entity, "distance", 999) or 999)
        if norm in names and (max_distance is None or dist <= max_distance):
            matches.append((dist, norm))
    if matches:
        matches.sort(key=lambda item: item[0])
        return matches[0][1]

    text = getattr(bot_state, "entities_nearby", "") or ""
    for line in text.splitlines():
        m = re.search(r"^\s*([a-zA-Z0-9_]+)\s+\((\d+(?:\.\d+)?)m\)", line)
        if not m:
            continue
        name = m.group(1).lower()
        dist = float(m.group(2))
        if name in names and (max_distance is None or dist <= max_distance):
            return name
    return None


def _player_too_close(players_nearby: str) -> bool:
    return _nearest_player_distance(players_nearby) is not None


def _nearest_player_distance(players_nearby: str, max_distance: float = 3.0) -> float | None:
    text = players_nearby or ""
    for line in text.splitlines():
        # Preferred shape from observe(): "player:Wilson at (150.0, 68.0, 14.0) dist:2.5"
        dist_match = re.search(r"\bdist(?:ance)?[:=]\s*(\d+(?:\.\d+)?)", line, re.I)
        if dist_match:
            dist = float(dist_match.group(1))
            if dist <= max_distance:
                return dist
            continue

        # Fallback for summaries that already contain "Name (2m)".
        meters_match = re.search(r"\((\d+(?:\.\d+)?)m\)", line, re.I)
        if meters_match:
            dist = float(meters_match.group(1))
            if dist <= max_distance:
                return dist
    return None
