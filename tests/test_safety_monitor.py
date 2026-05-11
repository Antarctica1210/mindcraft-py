"""Unit tests for :mod:`agent.safety_monitor`.

Covers three danger triggers (low health, hostile proximity, lava
nearby), the cancellable-future API, and the start/stop lifecycle.
The bridge is a :class:`FakeBridge` from ``conftest`` — no Mineflayer
process is started.
"""
from __future__ import annotations

import asyncio

import pytest

from agent.safety_monitor import SafetyMonitor, SafetyThresholds
from conftest import FakeBridge

# Polling at 50 ms keeps the suite fast while still allowing the loop
# to fire reliably within ``await asyncio.sleep(0.15)``.
FAST_POLL = SafetyThresholds(
    min_health=10.0,
    hostile_distance=3.0,
    poll_interval_s=0.05,
)


# ── Lifecycle ───────────────────────────────────────────────────────


async def test_start_stop_is_idempotent() -> None:
    monitor = SafetyMonitor(FakeBridge(), FAST_POLL)
    await monitor.start()
    await monitor.start()  # second start should be a no-op
    await monitor.stop()
    await monitor.stop()   # second stop should be a no-op


async def test_stop_drops_pending_futures() -> None:
    monitor = SafetyMonitor(FakeBridge(), FAST_POLL)
    await monitor.start()
    fut: asyncio.Future[None] = asyncio.Future()
    monitor.register_cancellable(fut)
    assert monitor.state.cancel_futures
    await monitor.stop()
    assert not monitor.state.cancel_futures
    assert not fut.cancelled()  # stop() should not cancel registered futures


# ── Triggers ────────────────────────────────────────────────────────


async def test_low_health_triggers_interrupt() -> None:
    bridge = FakeBridge()
    bridge.health = 3.0
    monitor = SafetyMonitor(bridge, FAST_POLL)
    await monitor.start()
    try:
        await asyncio.sleep(0.15)
        assert monitor.state.interrupted
        assert "health" in monitor.state.reason
    finally:
        await monitor.stop()


async def test_dead_bot_does_not_trigger() -> None:
    """Health == 0 means already dead — no need to interrupt."""
    bridge = FakeBridge()
    bridge.health = 0.0
    monitor = SafetyMonitor(bridge, FAST_POLL)
    await monitor.start()
    try:
        await asyncio.sleep(0.15)
        assert not monitor.state.interrupted
    finally:
        await monitor.stop()


async def test_hostile_in_range_triggers_interrupt() -> None:
    bridge = FakeBridge()
    bridge.entities_nearby = "  zombie (2m) [id:1]\n  cow (5m) [id:2]"
    monitor = SafetyMonitor(bridge, FAST_POLL)
    await monitor.start()
    try:
        await asyncio.sleep(0.15)
        assert monitor.state.interrupted
        assert "hostile" in monitor.state.reason
        assert "zombie" in monitor.state.reason
    finally:
        await monitor.stop()


async def test_hostile_out_of_range_does_not_trigger() -> None:
    bridge = FakeBridge()
    bridge.entities_nearby = "  zombie (12m) [id:1]"
    monitor = SafetyMonitor(bridge, FAST_POLL)
    await monitor.start()
    try:
        await asyncio.sleep(0.15)
        assert not monitor.state.interrupted
    finally:
        await monitor.stop()


async def test_lava_nearby_triggers_interrupt() -> None:
    bridge = FakeBridge()
    bridge.blocks_nearby = "standing on: lava"
    monitor = SafetyMonitor(bridge, FAST_POLL)
    await monitor.start()
    try:
        await asyncio.sleep(0.15)
        assert monitor.state.interrupted
        assert "lava" in monitor.state.reason
    finally:
        await monitor.stop()


# ── Cancellable API ────────────────────────────────────────────────


async def test_registered_future_is_cancelled_on_trigger() -> None:
    bridge = FakeBridge()
    monitor = SafetyMonitor(bridge, FAST_POLL)
    await monitor.start()
    try:
        fut: asyncio.Future[None] = asyncio.Future()
        monitor.register_cancellable(fut)
        bridge.health = 1.0
        await asyncio.sleep(0.15)
        assert monitor.state.interrupted
        assert fut.cancelled()
    finally:
        await monitor.stop()


async def test_clear_cancellables_drops_tracked_futures() -> None:
    monitor = SafetyMonitor(FakeBridge(), FAST_POLL)
    await monitor.start()
    try:
        fut: asyncio.Future[None] = asyncio.Future()
        monitor.register_cancellable(fut)
        assert len(monitor.state.cancel_futures) == 1
        monitor.clear_cancellables()
        assert not monitor.state.cancel_futures
    finally:
        await monitor.stop()


async def test_done_future_is_not_registered() -> None:
    monitor = SafetyMonitor(FakeBridge(), FAST_POLL)
    fut: asyncio.Future[None] = asyncio.Future()
    fut.set_result(None)
    monitor.register_cancellable(fut)
    assert not monitor.state.cancel_futures


# ── Idempotence ────────────────────────────────────────────────────


async def test_interrupt_does_not_double_fire() -> None:
    """Once interrupted, subsequent polls don't overwrite the reason."""
    bridge = FakeBridge()
    bridge.health = 2.0
    monitor = SafetyMonitor(bridge, FAST_POLL)
    await monitor.start()
    try:
        await asyncio.sleep(0.15)
        first_reason = monitor.state.reason
        first_ts = monitor.state.triggered_ts
        await asyncio.sleep(0.15)
        assert monitor.state.reason == first_reason
        assert monitor.state.triggered_ts == first_ts
    finally:
        await monitor.stop()


async def test_clear_interrupt_allows_retrigger() -> None:
    bridge = FakeBridge()
    bridge.health = 2.0
    monitor = SafetyMonitor(bridge, FAST_POLL)
    await monitor.start()
    try:
        await asyncio.sleep(0.15)
        assert monitor.state.interrupted
        monitor.clear_interrupt()
        assert not monitor.state.interrupted
        await asyncio.sleep(0.15)
        # Still dangerous → should re-trigger
        assert monitor.state.interrupted
    finally:
        await monitor.stop()


# ── Robustness ─────────────────────────────────────────────────────


async def test_bridge_failure_does_not_crash_loop() -> None:
    """If ``observe()`` raises, the loop continues without interrupting."""

    class FlakyBridge(FakeBridge):
        async def observe(self) -> dict:  # type: ignore[override]
            raise RuntimeError("network glitch")

    monitor = SafetyMonitor(FlakyBridge(), FAST_POLL)
    await monitor.start()
    try:
        await asyncio.sleep(0.15)
        # No interrupt because we never see state; the task must still be alive
        assert not monitor.state.interrupted
        assert monitor._task is not None and not monitor._task.done()
    finally:
        await monitor.stop()


@pytest.mark.parametrize("hostile", [
    "zombie", "skeleton", "creeper", "spider", "witch", "warden",
])
async def test_recognises_common_hostiles(hostile: str) -> None:
    bridge = FakeBridge()
    bridge.entities_nearby = f"  {hostile} (1.5m) [id:1]"
    monitor = SafetyMonitor(bridge, FAST_POLL)
    await monitor.start()
    try:
        await asyncio.sleep(0.15)
        assert monitor.state.interrupted
        assert hostile in monitor.state.reason
    finally:
        await monitor.stop()
