"""
Phase 2 — Safety monitor (Plan A: Python-side polling).

Periodically polls ``bridge.observe()`` on a background task. When danger
thresholds trigger (low health, hostile mob nearby, lava in adjacent
blocks) it sets an interrupt flag and cancels any in-flight LLM call
registered via :py:meth:`SafetyMonitor.register_cancellable`. AgentLoop
checks ``state.interrupted`` at plan / execute boundaries and jumps to
emergency handling.

This is "Plan A" from the roadmap — pure Python polling, no changes to
``socket_server.js``. "Plan B" (server-pushed ``health_change`` /
``entity_hurt`` events) is a future optimization listed in Phase 2 of
``roadmap/05-python-refactor-plan.md``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SafetyThresholds:
    """Configurable danger thresholds."""

    min_health: float = 5.0
    hostile_distance: float = 3.0       # any hostile entity inside this radius
    lava_block_names: tuple[str, ...] = ("lava", "flowing_lava")
    poll_interval_s: float = 0.25       # 250 ms — Plan A baseline


@dataclass
class SafetyState:
    """Snapshot of current danger conditions."""

    interrupted: bool = False
    reason: str = ""
    last_health: float = 20.0
    last_check_ts: float = 0.0
    triggered_ts: float = 0.0
    cancel_futures: list[asyncio.Future[Any]] = field(default_factory=list)


# Entity-name fragments that are hostile by default. Matched
# case-insensitively against the ``entities_nearby`` summary string.
_HOSTILE_NAME_FRAGMENTS: tuple[str, ...] = (
    "zombie", "skeleton", "creeper", "spider", "witch", "drowned", "husk",
    "phantom", "pillager", "vindicator", "evoker", "warden", "blaze",
    "ghast", "magma_cube", "stray", "wither", "guardian", "shulker",
    "hoglin", "piglin_brute", "ravager", "vex", "endermite", "silverfish",
    "slime",
)


# Match e.g. "zombie (2m)" or "creeper (1.5m)" — produced by
# nearbyEntities() in socket_server.js.
_NEARBY_ENTITY_RE = re.compile(
    r"(\w+)\s*\((\d+(?:\.\d+)?)m\)", re.IGNORECASE,
)


class SafetyMonitor:
    """Background safety polling task.

    Usage::

        monitor = SafetyMonitor(bridge)
        await monitor.start()
        try:
            # ... agent loop running ...
            llm_task = asyncio.ensure_future(prompter.send_request(...))
            monitor.register_cancellable(llm_task)
            try:
                response = await llm_task
            except asyncio.CancelledError:
                # SafetyMonitor cancelled us
                handle_emergency()
            finally:
                monitor.clear_cancellables()
        finally:
            await monitor.stop()
    """

    def __init__(
        self,
        bridge: Any,
        thresholds: SafetyThresholds | None = None,
    ) -> None:
        self._bridge = bridge
        self._thresholds = thresholds or SafetyThresholds()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self.state = SafetyState()

    # ── Lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin background polling. Idempotent."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "SafetyMonitor started (poll=%.2fs, min_health=%.1f, "
            "hostile<%.1fm)",
            self._thresholds.poll_interval_s,
            self._thresholds.min_health,
            self._thresholds.hostile_distance,
        )

    async def stop(self) -> None:
        """Stop polling and drop pending cancellables."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self.state.cancel_futures.clear()
        logger.info("SafetyMonitor stopped")

    # ── Interrupt API ──────────────────────────────────────────────

    def register_cancellable(self, fut: asyncio.Future[Any]) -> None:
        """Register an in-flight future (e.g. LLM call) for danger cancellation.

        AgentLoop should call this around LLM requests and clear via
        :py:meth:`clear_cancellables` once the request completes normally.
        """
        if not fut.done():
            self.state.cancel_futures.append(fut)

    def clear_cancellables(self) -> None:
        """Drop the list of tracked futures (call after LLM call completes)."""
        self.state.cancel_futures.clear()

    def clear_interrupt(self) -> None:
        """Reset the interrupt flag (call after emergency handling)."""
        self.state.interrupted = False
        self.state.reason = ""

    # ── Polling loop ───────────────────────────────────────────────

    async def _loop(self) -> None:
        while self._running:
            try:
                state = await self._bridge.observe()
                self.state.last_check_ts = time.time()
                reason = self._evaluate(state)
                if reason:
                    self._trigger(reason)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("SafetyMonitor poll failed", exc_info=True)
            await asyncio.sleep(self._thresholds.poll_interval_s)

    def _evaluate(self, state: dict[str, Any]) -> str | None:
        """Return a reason string if danger detected, otherwise None."""
        t = self._thresholds

        health = float(state.get("health", 20))
        self.state.last_health = health
        if 0 < health < t.min_health:
            return f"health<{t.min_health} (current={health:.1f})"

        ents_str = state.get("entities_nearby", "") or ""
        lower = ents_str.lower()
        for frag in _HOSTILE_NAME_FRAGMENTS:
            if frag in lower and self._too_close(
                ents_str, frag, t.hostile_distance,
            ):
                return f"hostile<{t.hostile_distance}m ({frag})"

        blocks = (state.get("blocks_nearby", "") or "").lower()
        for lava in t.lava_block_names:
            if lava in blocks:
                return f"lava nearby ({lava})"

        return None

    @staticmethod
    def _too_close(ents_str: str, frag: str, max_dist: float) -> bool:
        """Best-effort proximity parse from the ``entities_nearby`` summary."""
        for m in _NEARBY_ENTITY_RE.finditer(ents_str):
            name = m.group(1).lower()
            dist = float(m.group(2))
            if frag in name and dist <= max_dist:
                return True
        return False

    def _trigger(self, reason: str) -> None:
        if self.state.interrupted:
            return
        self.state.interrupted = True
        self.state.reason = reason
        self.state.triggered_ts = time.time()
        logger.warning("SafetyMonitor: interrupt triggered — %s", reason)
        for fut in self.state.cancel_futures:
            if not fut.done():
                fut.cancel()
        self.state.cancel_futures.clear()
