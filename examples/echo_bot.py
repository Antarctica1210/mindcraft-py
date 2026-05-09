#!/usr/bin/env python3
"""
Phase 0 — Echo bot: verify JSPyBridge + Mineflayer works from Python.

Run after starting a Minecraft LAN world:
    python examples/echo_bot.py --host 127.0.0.1 --port <LAN_PORT>

The bot will:
  1. Connect and announce itself on spawn.
  2. Echo back any chat message prefixed with 'echo'.
  3. Respond to 'come' by navigating to the sender.
"""
import argparse
import logging
import time

from mc import EventRegistry, create_bot, get_pathfinder
from src.logging_config import setup_logging

setup_logging(level=logging.INFO)
logger = logging.getLogger("echo_bot")


def main():
    parser = argparse.ArgumentParser(description="Phase 0 echo bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--username", default="EchoBot")
    args = parser.parse_args()

    logger.info("Creating bot: %s @ %s:%s", args.username, args.host, args.port)
    bot = create_bot(
        host=args.host,
        port=args.port,
        username=args.username,
    )
    events = EventRegistry(bot)
    pathfinder = get_pathfinder()

    # Signal for clean shutdown
    running = True

    @events.on("spawn")
    def on_spawn():
        logger.info("Bot spawned at %s", bot.entity.position)
        bot.chat("EchoBot online — say 'echo <msg>' or 'come'")

    @events.on("chat")
    def on_chat(sender, message, *args):
        if sender == bot.username:
            return  # ignore own messages

        logger.info("[chat] %s: %s", sender, message)

        if message.lower().startswith("echo "):
            # Echo back the message
            reply = message[5:]
            bot.chat(reply)
            logger.info("Echoed: %s", reply)

        elif message.lower() == "come":
            # Navigate to the player
            try:
                player_entity = bot.players[sender].entity
                target = pathfinder.goals.GoalNear(
                    player_entity.position.x,
                    player_entity.position.y,
                    player_entity.position.z,
                    1,
                )
                bot.pathfinder.setGoal(target)
                bot.chat(f"Coming to you, {sender}!")
                logger.info("Navigating to %s", sender)
            except Exception:
                bot.chat(f"I can't see you, {sender}")
                logger.exception("Failed to find player %s", sender)

        elif message.lower() == "quit":
            bot.chat("Goodbye!")
            nonlocal running
            running = False

    @events.on("end")
    def on_end(reason):
        logger.info("Bot disconnected: %s", reason)
        nonlocal running
        running = False

    @events.on("error")
    def on_error(err):
        logger.error("Bot error: %s", err)

    # Block until quit or disconnect
    try:
        while running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        events.remove_all()
        bot.quit()
        logger.info("Bot shut down.")


if __name__ == "__main__":
    main()
