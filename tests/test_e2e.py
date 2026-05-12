#!/usr/bin/env python3
"""
End-to-end test: Minecraft bot with LLM-driven behaviour.

Starts the Mineflayer socket server as a subprocess, connects a
SocketBridge, and wires up the full Phase 1+2 pipeline (LiteLLM →
parse → execute) via TCP JSON-line protocol.

Usage:
    uv run python tests/test_e2e.py                  # full pipeline
    uv run python tests/test_e2e.py --no-llm          # skip LLM (commands only)
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
from typing import Any

# Ensure src/ is on the path when running from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from llm.message_builder import ConversationTurn  # noqa: E402
from logging_config import setup_logging  # noqa: E402
from settings import settings  # noqa: E402

setup_logging(level=logging.INFO)
logger = logging.getLogger("e2e")

# ── Socket server launcher ────────────────────────────────────────────

_SERVER_PROC: subprocess.Popen[str] | None = None


def start_socket_server(port: int = 9090) -> None:
    """Launch the Mineflayer socket server as a subprocess."""
    global _SERVER_PROC
    server_js = os.path.join(os.path.dirname(__file__), "..", "src", "mc", "socket_server.js")
    _SERVER_PROC = subprocess.Popen(
        ["node", server_js, "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Wait for server to be ready
    deadline = time.time() + 30
    while time.time() < deadline:
        line = _SERVER_PROC.stdout.readline()  # type: ignore[union-attr]
        if not line:
            if _SERVER_PROC.poll() is not None:
                raise RuntimeError(f"Socket server exited early: rc={_SERVER_PROC.returncode}")
            time.sleep(0.1)
            continue
        print(f"[server] {line.rstrip()}")
        if "Listening on" in line:
            logger.info("Socket server ready")
            return
    raise RuntimeError("Socket server failed to start within 30s")


def stop_socket_server() -> None:
    """Shut down the socket server subprocess."""
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


# ── Agent harness ───────────────────────────────────────────────────


class E2EHarness:
    """Wires the full Phase 1+2 pipeline around a socket bridge."""

    def __init__(self, bridge: Any, llm_enabled: bool = True):
        from agent.conversation import ConversationManager
        from agent.core import _build_tool_defs
        from agent.core import _command_defs as get_cmd_defs
        from agent.executor import ActionExecutor
        from agent.modes import ModeEvaluator
        from llm.message_builder import MessageBuilder  # noqa: I001

        self._bridge = bridge
        self._llm_enabled = llm_enabled

        self._executor = ActionExecutor(bridge=bridge)
        self._executor._register_builtins()
        self._convo_mgr = ConversationManager()
        self._modes = ModeEvaluator()
        self._history: list[ConversationTurn] = []

        self._msg_builder = MessageBuilder(
            bot_name=settings.bot.name,
            commands=list(get_cmd_defs().values()),
            self_prompt="",
        )

        self._prompter: Any = None
        if llm_enabled:
            from llm import Prompter

            self._prompter = Prompter(settings.llm, embedding=settings.embedding)
            tools = _build_tool_defs(self._executor._handlers)
            if tools:
                self._prompter.register_tools(tools)

    # ── Observation ─────────────────────────────────────────────────

    async def observe(self) -> Any:
        from agent.models import BotState, Position

        s = await self._bridge.observe()
        p = s.get("position", {})
        return BotState(
            health=s.get("health", 20),
            food=s.get("food", 20),
            position=Position(x=p.get("x", 0), y=p.get("y", 0), z=p.get("z", 0)),
            gamemode=s.get("gamemode", "survival"),
            time_of_day=s.get("time_of_day", ""),
            biome="",
            entities_nearby=s.get("entities_nearby", ""),
            blocks_nearby=s.get("blocks_nearby", ""),
        )

    async def format_inventory(self) -> str:
        try:
            items = await self._bridge.get_inventory()
            if not items:
                return "INVENTORY: empty"
            lines = ["INVENTORY:"]
            for item in items[:20]:
                lines.append(f"  {item['name']} x{item['count']}")
            return "\n".join(lines)
        except Exception:
            return "INVENTORY: unavailable"

    # ── Pipeline ────────────────────────────────────────────────────

    async def handle_message(self, sender: str, message: str) -> None:
        """Full observe → plan → execute cycle."""
        print(f"\n{'─' * 60}")
        print(f"[CHAT] {sender}: {message}")
        print(f"{'─' * 60}")

        # 1. Observe
        state = await self.observe()
        print(f"[OBSERVE] health={state.health:.0f} food={state.food:.0f} "
              f"pos=({state.position.x:.1f}, {state.position.y:.1f}, {state.position.z:.1f}) "
              f"time={state.time_of_day}")
        if state.entities_nearby:
            print(f"[OBSERVE] nearby:\n{state.entities_nearby}")

        # 2. Build prompt
        stats = self._msg_builder.format_stats(
            health=state.health,
            food=state.food,
            position=(state.position.x, state.position.y, state.position.z),
            gamemode=state.gamemode,
            time_of_day=state.time_of_day,
            biome=state.biome,
            entities_nearby=state.entities_nearby,
            blocks_nearby=state.blocks_nearby,
        )
        inventory = await self.format_inventory()
        command_docs = self._msg_builder.build_command_docs()

        system_prompt = self._msg_builder.build_system_prompt(
            stats=stats,
            inventory=inventory,
            command_docs=command_docs,
            examples="",
            memory="",
        )

        self._history.append(ConversationTurn(role="user", content=message))

        if not self._prompter:
            print("[LLM] DISABLED — manual command mode")
            await self._check_manual_command(sender, message)
            return

        # 3. Call LLM
        print(f"[LLM] Sending to {settings.llm.model}...")
        messages = self._msg_builder.build_messages(
            history=self._history,
            system_prompt=system_prompt,
        )

        try:
            from llm.prompter import LLMInput

            response = await self._prompter.send_request(
                LLMInput(
                    history=messages[1:],
                    system_message=system_prompt,
                )
            )
        except Exception as exc:
            logger.exception("LLM call failed")
            await self._bridge.chat(f"Sorry, my brain failed: {exc}")
            return

        print(f"[LLM] model={response.model} tokens={response.usage} "
              f"finish={response.finish_reason}")
        print(f"[LLM] content ({len(response.content)} chars):")
        print(f"  {response.content[:500]}")

        self._history.append(ConversationTurn(role="assistant", content=response.content))

        # 4. Parse
        from llm.tool_parser import parse_response

        parsed = parse_response(response.content, tool_calls=response.tool_calls or None)
        print(f"[PARSE] has_command={parsed.has_command} commands={len(parsed.commands)}")
        if parsed.has_command:
            print(f"[PARSE] cleaned text: {parsed.text[:200]}")

        if not parsed.has_command:
            await self._bridge.chat(parsed.text)
            return

        # 5. Execute commands
        for i, cmd in enumerate(parsed.commands):
            display_args = cmd.args if cmd.args else list((cmd.kwargs or {}).values())
            print(f"[EXEC #{i}] !{cmd.name}({', '.join(str(a) for a in display_args)})")
            try:
                result = await self._executor.execute(cmd.name, cmd.args, cmd.kwargs)
                print(f"[RESULT #{i}] {result}")
                if result:
                    self._history.append(ConversationTurn(role="system", content=result))
            except Exception as exc:
                logger.exception("Command execution failed")
                print(f"[ERROR #{i}] {exc}")

        # Speak the non-command portion (if any)
        if parsed.text.strip():
            await self._bridge.chat(parsed.text)

        # Compress history
        self._history = self._convo_mgr.compress_history(self._history)
        print(f"[HISTORY] {len(self._history)} turns after compression")

    async def _check_manual_command(self, sender: str, message: str) -> None:
        """When LLM is disabled, check for manual !command syntax."""
        from llm.tool_parser import has_command, parse_response

        if has_command(message):
            parsed = parse_response(message)
            for cmd in parsed.commands:
                result = await self._executor.execute(cmd.name, cmd.args)
                print(f"[CMD] !{cmd.name} → {result}")
                if result:
                    await self._bridge.chat(str(result))


# ── Main ────────────────────────────────────────────────────────────


async def async_main(args: argparse.Namespace) -> None:
    """Async entry point."""
    from mc import SocketBridge

    socket_port = 9090

    print("=" * 60)
    print("  E2E Test — LLM-driven Minecraft Bot (Socket Bridge)")
    print(f"  LLM: {settings.llm.model}")
    print(f"  MC Server: {args.host}:{args.port}")
    print(f"  Socket Bridge: tcp://127.0.0.1:{socket_port}")
    print(f"  LLM enabled: {not args.no_llm}")
    print("=" * 60)

    # 1. Start socket server
    print("\n[SETUP] Starting Mineflayer socket server...")
    start_socket_server(socket_port)

    # 2. Connect bridge
    print("[SETUP] Connecting socket bridge...")
    bridge = SocketBridge(port=socket_port)
    await bridge.connect()

    # 3. Create bot
    print(f"[SETUP] Creating bot: {args.username}")
    result = await bridge.create_bot(host=args.host, port=args.port, username=args.username)
    print(f"[BOT] Connected as {result['username']}")

    # 4. Set up agent harness
    harness = E2EHarness(bridge, llm_enabled=not args.no_llm)

    # 5. Wire chat event — when a player sends chat, trigger pipeline
    async def on_chat(sender: str, message: str) -> None:
        if sender == args.username:
            return
        await harness.handle_message(sender, message)

    bridge.on("chat")(on_chat)

    # 6. Dispatch test message
    print(f"\n[SEND] Dispatching test message: \"{args.message}\"")
    await asyncio.sleep(1)
    await harness.handle_message("system", args.message)

    # 7. Keep running
    print("\n[WAIT] Listening for chat messages (Ctrl+C to stop)...")
    print("[WAIT] Or type in Minecraft chat to interact with the bot.\n")

    try:
        while True:
            await asyncio.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[DONE] Shutting down...")
    finally:
        await bridge.close()
        stop_socket_server()


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E test — LLM-driven Minecraft bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=55916)
    parser.add_argument("--username", default=settings.bot.name)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument(
        "--message",
        default="Walk 5 blocks north and tell me your new position.",
    )
    args = parser.parse_args()

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\n[DONE]")
    finally:
        stop_socket_server()


if __name__ == "__main__":
    main()
