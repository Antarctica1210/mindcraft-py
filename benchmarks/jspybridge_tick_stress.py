#!/usr/bin/env python3
"""
Phase 0 — JSPyBridge high-frequency tick stress test.

Measures Python ↔ JS bridge overhead under Minecraft-like event load
(20 TPS = 20 tick events/sec). Tests three dimensions:

    1. Tick event interval consistency (target: 50ms ± drift)
    2. API round-trip latency (bot.entity.position read via JSPyBridge)
    3. Multi-bot event scaling (optional)

Requires a running Minecraft server on localhost unless --mock is used.

Pass criteria:
    - Tick P50 interval within 5ms of 50ms target → JSPyBridge viable
    - Tick P50 within 15ms → edge case, limit high-freq handlers
    - Tick P50 > 15ms drift OR event drops → fall back to WebSocket scheme
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

# ── Test with mock (no real Minecraft server) ──


def run_mock_stress(duration_s: int = 30, events_per_sec: int = 20) -> dict:
    """Stress-test the Python ↔ JS bridge without a real Minecraft server.

    Creates a simple Node.js EventEmitter to simulate tick events.
    """
    from javascript import require

    emitter = require("events")
    ee = emitter.EventEmitter()

    tick_latencies = []
    api_latencies = []

    handler_called = 0
    last_tick = time.time()

    def on_tick():
        nonlocal handler_called, last_tick
        now = time.time()
        tick_latencies.append(now - last_tick)
        last_tick = now
        handler_called += 1
        # Simulate a typical agent operation: read a value across the bridge
        start = time.perf_counter()
        _ = ee.listenerCount("tick")
        api_latencies.append((time.perf_counter() - start) * 1000)

    # Register Python handler on JS event emitter
    from javascript import On

    On(ee, "tick")(on_tick)

    print(f"Running mock stress test for {duration_s}s at {events_per_sec} events/sec...")
    interval_s = 1.0 / events_per_sec
    deadline = time.time() + duration_s

    while time.time() < deadline:
        ee.emit("tick")
        elapsed = time.time() - (deadline - duration_s)
        # Sleep to maintain target rate
        next_tick = int(elapsed / interval_s + 1) * interval_s
        sleep_time = next_tick - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)

    return _compute_stats(
        tick_latencies, api_latencies, handler_called, duration_s, "mock"
    )


# ── Test with real Minecraft server ──


def _start_viewer(bot, viewer_port: int) -> None:
    """Start prismarine-viewer for a bot — opens a 3D view in the browser."""
    from javascript import require

    try:
        pv = require("prismarine-viewer")
        pv.mineflayer(bot, {"port": viewer_port, "firstPerson": True})
        print(f"Viewer: http://localhost:{viewer_port}")
    except Exception:
        print("WARNING: prismarine-viewer not available (npm install prismarine-viewer)")


def run_live_stress(
    host: str, port: int, duration_s: int = 30, viewer_port: int = 0
) -> dict | None:
    """Stress-test with a real Mineflayer bot connected to a Minecraft server."""
    from javascript import On

    from mc import create_bot

    print(f"Connecting to {host}:{port}...")
    bot = create_bot(host=host, port=port, username="StressBot")

    if viewer_port > 0:
        _start_viewer(bot, viewer_port)

    tick_latencies: list[float] = []
    api_latencies: list[float] = []
    handler_called = 0
    last_tick = time.time()
    spawned = False

    # Use raw JSPyBridge On — EventRegistry wrapper loses the reference
    @On(bot, "spawn")
    def on_spawn(this):
        nonlocal spawned
        spawned = True
        print("Bot spawned, starting data collection...")

    @On(bot, "physicsTick")
    def on_tick(this):
        nonlocal handler_called, last_tick
        now = time.time()
        tick_latencies.append(now - last_tick)
        last_tick = now
        handler_called += 1
        # Simulate agent read
        start = time.perf_counter()
        if bot.entity:
            _ = bot.entity.position
        api_latencies.append((time.perf_counter() - start) * 1000)

    # Wait for spawn (max 30s)
    timeout = time.time() + 30
    while not spawned and time.time() < timeout:
        time.sleep(0.1)

    if not spawned:
        print("ERROR: Bot failed to spawn within 30s. Is a Minecraft server running?")
        bot.quit()
        return None

    # Let events stabilize
    time.sleep(1)
    tick_latencies.clear()
    api_latencies.clear()
    handler_called = 0

    # Move the bot to generate additional events
    bot.setControlState("forward", True)
    time.sleep(2)
    bot.setControlState("forward", False)

    # Main measurement window
    start = time.time()
    while time.time() - start < duration_s:
        time.sleep(0.05)

    actual_duration = time.time() - start

    bot.quit()

    return _compute_stats(
        tick_latencies, api_latencies, handler_called, actual_duration, f"{host}:{port}"
    )


# ── Stats computation ──


def _compute_stats(
    tick_intervals: list[float],
    api_latencies_ms: list[float],
    handler_count: int,
    duration_s: float,
    source: str,
) -> dict:
    """Compute and print statistics, return as dict for programmatic use."""
    result = {
        "source": source,
        "duration_s": duration_s,
        "tick_handler_calls": handler_count,
        "tick_rate_per_sec": handler_count / duration_s if duration_s > 0 else 0,
    }

    if tick_intervals:
        sorted_intervals = sorted(tick_intervals)
        n = len(sorted_intervals)
        tick_p50 = sorted_intervals[int(n * 0.50)] * 1000 if n > 0 else 0
        tick_p95 = sorted_intervals[int(n * 0.95)] * 1000 if n > 1 else tick_p50
        tick_p99 = sorted_intervals[int(n * 0.99)] * 1000 if n > 1 else tick_p50
        tick_max = max(tick_intervals) * 1000
        tick_avg = statistics.mean(tick_intervals) * 1000

        result.update({
            "tick_samples": n,
            "tick_p50_ms": round(tick_p50, 2),
            "tick_p95_ms": round(tick_p95, 2),
            "tick_p99_ms": round(tick_p99, 2),
            "tick_max_ms": round(tick_max, 2),
            "tick_avg_ms": round(tick_avg, 2),
        })

    if api_latencies_ms:
        sorted_api = sorted(api_latencies_ms)
        n = len(sorted_api)
        result.update({
            "api_samples": n,
            "api_p50_ms": round(sorted_api[n // 2], 4),
            "api_p95_ms": round(sorted_api[int(n * 0.95)], 4),
            "api_p99_ms": (
                round(sorted_api[int(n * 0.99)], 4) if n > 1
                else round(sorted_api[n // 2], 4)
            ),
            "api_max_ms": round(max(api_latencies_ms), 4),
        })

    # Print report
    _print_report(result)
    return result


def _print_report(stats: dict) -> None:
    """Print human-readable stress test report."""
    print(f"\n{'=' * 60}")
    print(f"  JSPyBridge Tick Stress Test — {stats['source']}")
    dur = stats["duration_s"]
    tick_calls = stats.get("tick_handler_calls", "N/A")
    print(f"  Duration: {dur:.1f}s  |  Tick calls: {tick_calls}")
    print(f"{'=' * 60}")

    if stats.get("tick_samples"):
        target = 50.0
        drift = stats["tick_avg_ms"] - target
        print(f"\n  Tick interval:  avg={stats['tick_avg_ms']:.1f}ms  "
              f"P50={stats['tick_p50_ms']:.1f}ms  "
              f"P95={stats['tick_p95_ms']:.1f}ms  "
              f"P99={stats['tick_p99_ms']:.1f}ms")
        print(f"  Tick rate:      {stats['tick_rate_per_sec']:.1f}/sec  (target ~20/sec)")
        print(f"  Drift from 50ms: {'+' if drift > 0 else ''}{drift:.1f}ms")

        if abs(drift) < 5:
            print("  Result: ✅ PASS — JSPyBridge overhead is negligible")
        elif abs(drift) < 15:
            print("  Result: ⚠️  EDGE — limit high-frequency handlers in Python")
        else:
            print("  Result: ❌ FAIL — fall back to WebSocket/IPC bridge")
    else:
        print("\n  No tick data collected.")

    if stats.get("api_samples"):
        print(f"\n  API round-trip: P50={stats['api_p50_ms']:.4f}ms  "
              f"P95={stats['api_p95_ms']:.4f}ms  "
              f"P99={stats.get('api_p99_ms', 'N/A')}ms")

    print()


# ── CLI ──


def main():
    parser = argparse.ArgumentParser(
        description="JSPyBridge tick stress test for Phase 0 validation"
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run without a real Minecraft server (uses Node.js EventEmitter)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Minecraft server host")
    parser.add_argument("--port", type=int, default=25565, help="Minecraft server port")
    parser.add_argument(
        "--duration", type=int, default=30, help="Test duration in seconds"
    )
    parser.add_argument(
        "--viewer", type=int, default=0, metavar="PORT",
        help="Enable prismarine-viewer on the given port (e.g. --viewer 3000)"
    )
    parser.add_argument(
        "--output", type=Path, help="Save JSON results to file"
    )
    args = parser.parse_args()

    if args.mock:
        result = run_mock_stress(duration_s=args.duration)
    else:
        result = run_live_stress(
            args.host, args.port, duration_s=args.duration, viewer_port=args.viewer
        )

    if result and args.output:
        args.output.write_text(json.dumps(result, indent=2))
        print(f"Results saved to {args.output}")

    # Exit code based on result
    if result and result.get("tick_samples"):
        drift = abs(result["tick_avg_ms"] - 50)
        sys.exit(0 if drift < 15 else 1)


if __name__ == "__main__":
    main()
