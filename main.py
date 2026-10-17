#!/usr/bin/env python3
"""
Mindcraft-Py — LLM-driven Minecraft bot.

Start the bot with:

    uv run python main.py                     # defaults from .env
    uv run python main.py --host 192.168.1.5  # remote server

The socket server (Node.js) is auto-detected:
  - If a socket server is already running on the port, it reuses it.
  - Otherwise, it starts one as a subprocess and shuts it down on exit.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Ensure src/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from logging_config import setup_logging  # noqa: E402
from settings import settings  # noqa: E402

logger = logging.getLogger("main")


# ── Socket server subprocess ────────────────────────────────────────────

_SERVER_PROC: subprocess.Popen[str] | None = None


def _check_port(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if something is listening on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def _start_socket_server(port: int = 9090) -> None:
    """Launch the Mineflayer socket server as a subprocess."""
    global _SERVER_PROC
    server_js = (
        Path(__file__).resolve().parent / "src" / "mc" / "socket_server.js"
    )
    if not server_js.exists():
        raise FileNotFoundError(f"Socket server not found: {server_js}")

    _SERVER_PROC = subprocess.Popen(
        ["node", str(server_js), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    deadline = time.time() + 30
    while time.time() < deadline:
        line = _SERVER_PROC.stdout.readline()  # type: ignore[union-attr]
        if not line:
            if _SERVER_PROC.poll() is not None:
                raise RuntimeError(
                    f"Socket server exited early (rc={_SERVER_PROC.returncode})"
                )
            time.sleep(0.1)
            continue
        logger.info("[socket-server] %s", line.rstrip())
        if "Listening on" in line:
            logger.info("Socket server ready on port %d", port)
            return

    raise RuntimeError("Socket server failed to start within 30s")


def _stop_socket_server() -> None:
    """Shut down the socket server subprocess gracefully."""
    global _SERVER_PROC
    if _SERVER_PROC is None:
        return
    try:
        _SERVER_PROC.send_signal(signal.SIGTERM)
        _SERVER_PROC.wait(timeout=5)
    except (subprocess.TimeoutExpired, Exception):
        _SERVER_PROC.kill()
        _SERVER_PROC.wait()
    _SERVER_PROC = None


# ── Bot runner ──────────────────────────────────────────────────────────


class BotRunner:
    """Wires the full pipeline: bridge → agent → chat handler → main loop."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self._bridge: Any = None  # SocketBridge
        self._agent: Any = None   # Agent
        self._loop_task: asyncio.Task[None] | None = None
        self._own_server: bool = False  # True if we started the socket server

    async def run(self) -> None:
        """Start everything and keep running until interrupted."""
        from agent.core import Agent
        from agent.models import AgentConfig
        from mc import SocketBridge

        socket_host = "127.0.0.1"
        socket_port = self._args.socket_port

        # 1. Auto-detect or start socket server
        if _check_port(socket_host, socket_port):
            logger.info(
                "Socket server already running on %s:%d — reusing.",
                socket_host,
                socket_port,
            )
        else:
            logger.info(
                "No socket server on %s:%d — starting subprocess…",
                socket_host,
                socket_port,
            )
            _start_socket_server(socket_port)
            self._own_server = True

        # 2. Connect bridge
        bridge = SocketBridge(port=socket_port)
        try:
            await bridge.connect()
        except Exception:
            if self._own_server:
                _stop_socket_server()
            raise
        self._bridge = bridge

        # 3. Create bot on Minecraft server
        logger.info(
            "Creating bot '%s' on %s:%d …",
            self._args.username,
            self._args.host,
            self._args.port,
        )
        result = await bridge.create_bot(
            host=self._args.host,
            port=self._args.port,
            username=self._args.username,
        )
        logger.info("Bot connected as %s", result.get("username", self._args.username))

        # 4. Initialize agent
        agent_config = AgentConfig(
            name=self._args.username,
            max_commands=settings.bot.max_commands,
            narrate_behavior=settings.bot.narrate_behavior,
            blocked_actions=settings.bot.blocked_actions,
        )
        agent = Agent(agent_config)
        await agent.start(bridge)
        self._agent = agent

        # 5. Wire chat event — incoming player messages trigger the agent
        async def on_chat(sender: str, message: str) -> None:
            if sender == self._args.username:
                return  # ignore own messages
            try:
                await agent.handle_message(sender, message)
            except Exception:
                logger.exception("handle_message failed")

        bridge.on("chat")(on_chat)

        # 6. Run the agent main loop (modes, unstuck check, etc.)
        self._loop_task = asyncio.create_task(agent.run_loop())

        # 7. Announce ready
        logger.info("Bot is ready. Waiting for chat messages (Ctrl+C to stop).")
        logger.info(
            "Connected to %s:%d as %s (LLM: %s)",
            self._args.host,
            self._args.port,
            self._args.username,
            settings.llm.model,
        )

    async def shutdown(self) -> None:
        """Gracefully shut down the bot, bridge, and socket server."""
        # Cancel main loop
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

        # Stop agent
        if self._agent is not None:
            try:
                await self._agent.stop()
            except Exception:
                logger.exception("Error stopping agent")

        # Close bridge
        if self._bridge is not None:
            try:
                await self._bridge.close()
            except Exception:
                logger.exception("Error closing bridge")

        # Stop socket server — only if we started it
        if self._own_server:
            _stop_socket_server()

        logger.info("Shutdown complete.")


# ── CLI ─────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mindcraft-Py — LLM-driven Minecraft bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python main.py
  uv run python main.py --host 192.168.1.5 --port 25565
  uv run python main.py --username MyBot --socket-port 9091
        """,
    )

    # Minecraft server
    mc = parser.add_argument_group("Minecraft Server")
    mc.add_argument("--host", default="127.0.0.1", help="Minecraft server IP")
    mc.add_argument("--port", type=int, default=55916, help="Minecraft server port")
    mc.add_argument(
        "--username",
        default=settings.bot.name,
        help="Bot username (default from $BOT_NAME)",
    )

    # Socket bridge
    br = parser.add_argument_group("Socket Bridge")
    br.add_argument(
        "--socket-port",
        type=int,
        default=9090,
        help="Socket bridge port (default 9090)",
    )

    # Logging
    log = parser.add_argument_group("Logging")
    log.add_argument(
        "--log-level",
        default=settings.log_level,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default from $LOG_LEVEL or INFO)",
    )

    return parser


# ── Main ────────────────────────────────────────────────────────────────


async def _async_main(args: argparse.Namespace) -> None:
    runner = BotRunner(args)
    try:
        await runner.run()
        # Keep alive until interrupted
        while True:
            await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("=" * 180)
        logger.info("Interrupted — shutting down…")
    except Exception:
        logger.exception("Fatal error")
    finally:
        await runner.shutdown()


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    setup_logging(level=getattr(logging, args.log_level))

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Mindcraft-Py — LLM-driven Minecraft Bot")
    logger.info("  LLM: %s", settings.llm.model)
    logger.info("  MC Server: %s:%d", args.host, args.port)
    logger.info("  Bot: %s", args.username)
    logger.info("  Socket Bridge: tcp://127.0.0.1:%d", args.socket_port)
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    try:
        asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        pass
    finally:
        _stop_socket_server()
    logger.info("=" * 180)
    logger.info("Goodbye.")


if __name__ == "__main__":
    main()
