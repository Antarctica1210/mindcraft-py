#!/usr/bin/env python3
"""
Echo bot using the Socket Bridge (no JSPyBridge required).

Start the Mineflayer socket server first:
    node src/mc/socket_server.js --port 9090

Then run the echo bot:
    python examples/echo_bot.py --host 127.0.0.1 --port <LAN_PORT>

The bot will:
  1. Connect and announce itself on spawn.
  2. Echo back any chat message prefixed with 'echo'.
  3. Respond to 'come' by navigating to the sender.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from logging_config import setup_logging  # noqa: E402
from mc import SocketBridge  # noqa: E402

setup_logging(level=logging.INFO)
logger = logging.getLogger("echo_bot")


async def async_main(args: argparse.Namespace) -> None:
    bridge = SocketBridge(port=args.socket_port)

    if args.start_server:
        _start_server(args.socket_port)

    try:
        await bridge.connect()

        # Wait for spawn event before proceeding
        spawned = asyncio.Event()

        @bridge.on("spawn")
        async def on_spawn() -> None:
            await bridge.chat("EchoBot online — say 'echo <msg>' or 'come'")
            spawned.set()

        @bridge.on("chat")
        async def on_chat(sender: str, message: str) -> None:
            if sender == args.username:
                return
            logger.info("[chat] %s: %s", sender, message)

            if message.lower().startswith("echo "):
                await bridge.chat(message[5:])
                logger.info("Echoed: %s", message[5:])

            elif message.lower() == "come":
                state = await bridge.observe()
                players = state.get("players_nearby", "")
                # Parse player position from players_nearby string
                target = None
                for line in players.split("\n"):
                    if f"player:{sender}" in line:
                        parts = line.replace("player:", "").split(" at ")
                        if len(parts) == 2:
                            coords = parts[1].strip("()").split(", ")
                            if len(coords) == 3:
                                target = (float(coords[0]), float(coords[1]), float(coords[2]))
                                break

                if target:
                    await bridge.move_to(target[0], target[1], target[2])
                    await bridge.chat(f"Coming to you, {sender}!")
                    logger.info("Navigating to %s", sender)
                else:
                    await bridge.chat(f"I can't see you, {sender}")

            elif message.lower() == "quit":
                await bridge.chat("Goodbye!")

        @bridge.on("end")
        async def on_end(reason: str) -> None:
            logger.info("Bot disconnected: %s", reason)

        @bridge.on("error")
        async def on_error(message: str) -> None:
            logger.error("Bot error: %s", message)

        # Create bot — this resolves after spawn
        result = await bridge.create_bot(
            host=args.host, port=args.port, username=args.username
        )
        logger.info("Bot created: %s", result)

        # Wait for spawn event
        await asyncio.wait_for(spawned.wait(), timeout=30)
        logger.info("Bot ready")

        # Keep running until Ctrl+C
        while True:
            await asyncio.sleep(0.5)

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        try:
            await bridge.close()
        except Exception:
            pass
        if args.start_server:
            _stop_server()
        logger.info("Bot shut down.")


# ── Subprocess helpers ──────────────────────────────────────────────

_server_proc: subprocess.Popen[str] | None = None


def _start_server(port: int) -> None:
    global _server_proc
    server_js = os.path.join(os.path.dirname(__file__), "..", "src", "mc", "socket_server.js")
    _server_proc = subprocess.Popen(
        ["node", server_js, "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        line = _server_proc.stdout.readline()  # type: ignore[union-attr]
        if not line:
            if _server_proc.poll() is not None:
                raise RuntimeError(f"Socket server exited: rc={_server_proc.returncode}")
            time.sleep(0.1)
            continue
        if "Listening on" in line:
            return
    raise RuntimeError("Socket server start timeout")


def _stop_server() -> None:
    global _server_proc
    if _server_proc is None:
        return
    try:
        _server_proc.send_signal(signal.SIGTERM)
        _server_proc.wait(timeout=5)
    except Exception:
        _server_proc.kill()
        _server_proc.wait()
    _server_proc = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Socket bridge echo bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=55916)
    parser.add_argument("--username", default="EchoBot")
    parser.add_argument("--socket-port", type=int, default=9090)
    parser.add_argument("--start-server", action="store_true",
                       help="Launch the socket server as a subprocess")
    args = parser.parse_args()

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass
    finally:
        _stop_server()


if __name__ == "__main__":
    main()
