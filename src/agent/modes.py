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
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class BehaviorMode(enum.StrEnum):
    SELF_PRESERVATION = "self_preservation"
    UNSTUCK = "unstuck"
    HUNTING = "hunting"
    IDLE = "idle"
    CONVERSING = "conversing"
    CODING = "coding"


# Priority order — lower index = higher priority
MODE_PRIORITY: list[BehaviorMode] = [
    BehaviorMode.SELF_PRESERVATION,
    BehaviorMode.UNSTUCK,
    BehaviorMode.HUNTING,
    BehaviorMode.IDLE,
    BehaviorMode.CONVERSING,
    BehaviorMode.CODING,
]


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

    def frame(self, position: tuple[float, float, float]) -> None:
        """Called once per tick to update mode-specific state."""
        if position != self._prev_position:
            self._stuck_since = 0.0
        self._prev_position = position


class ModeEvaluator:
    """Evaluates behavior modes in priority order each tick."""

    def __init__(self) -> None:
        self._states: dict[BehaviorMode, ModeState] = {
            m: ModeState(mode=m) for m in BehaviorMode
        }
        self._active_mode: BehaviorMode | None = None
        self._frame_count: int = 0

    def active_mode(self) -> BehaviorMode | None:
        return self._active_mode

    def is_active(self, mode: BehaviorMode) -> bool:
        return self._active_mode == mode

    def activate(self, mode: BehaviorMode) -> None:
        """Activate a mode, pausing lower-priority ones."""
        self._active_mode = mode
        state = self._states[mode]
        state.active = True
        state.paused = False
        priority_idx = MODE_PRIORITY.index(mode)
        for i, m in enumerate(MODE_PRIORITY):
            if i > priority_idx:
                self._states[m].paused = True

    def release(self, mode: BehaviorMode) -> None:
        """Release a mode so lower-priority ones can resume."""
        self._states[mode].active = False
        if self._active_mode == mode:
            self._active_mode = None
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
            return await self._eval_unstuck(state)
        elif mode == BehaviorMode.HUNTING:
            return False
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
            return True
        if bot_state.health < 5:
            logger.warning("Bot is dying — self_preservation triggered")
            return True
        return False

    async def _eval_unstuck(self, state: ModeState) -> bool:
        if state._stuck_since == 0.0:
            state._stuck_since = time.time()
            return False
        stuck_duration = time.time() - state._stuck_since
        if stuck_duration > 20:
            logger.warning("Bot stuck for %.0fs — unstuck triggered", stuck_duration)
            return True
        return False
