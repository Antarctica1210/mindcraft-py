"""
Phase 0 — Mineflayer JSPyBridge wrapper.

Provides a clean Python API for creating and controlling Mineflayer bots
via JSPyBridge. All Mineflayer APIs are accessible directly from Python.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Lazy-imported JS modules — only loaded when first needed
_js_modules: dict[str, Any] = {}


def _require(module_name: str) -> Any:
    """Import a Node.js module via JSPyBridge, with caching."""
    if module_name not in _js_modules:
        from javascript import require as _js_require

        _js_modules[module_name] = _js_require(module_name)
    return _js_modules[module_name]


# Default plugins commonly needed by mindcraft bots
DEFAULT_PLUGINS = [
    "mineflayer-pathfinder",
    "mineflayer-pvp",
    "mineflayer-collectblock",
    "mineflayer-armor-manager",
    "mineflayer-auto-eat",
]


def create_bot(
    host: str = "127.0.0.1",
    port: int = 25565,
    username: str = "PythonBot",
    *,
    auth: str = "offline",
    version: str | None = None,
    plugins: list[str] | None = None,
    **kwargs: Any,
) -> Any:
    """Create and return a Mineflayer bot instance.

    Args:
        host: Minecraft server address.
        port: Server port.
        username: Bot display name.
        auth: Authentication mode — 'offline' or 'microsoft'.
        version: Minecraft version string, e.g. '1.21.4'. None for auto-detect.
        plugins: List of npm plugin names to load. None loads defaults.
        **kwargs: Passed directly to mineflayer.createBot().

    Returns:
        A Mineflayer bot object with all requested plugins loaded.

    Example:
        bot = create_bot(host='localhost', port=55916, username='TestBot')
        bot.chat('Hello from Python!')
    """
    mineflayer = _require("mineflayer")

    bot_options: dict[str, Any] = {
        "host": host,
        "port": port,
        "username": username,
        "auth": auth,
        **kwargs,
    }
    if version is not None:
        bot_options["version"] = version

    bot = mineflayer.createBot(bot_options)

    # Load plugins
    plugin_names = plugins if plugins is not None else DEFAULT_PLUGINS
    for plugin_name in plugin_names:
        try:
            plugin_mod = _require(plugin_name)
            # Most mineflayer plugins export a single plugin function
            plugin_func = plugin_mod.plugin or plugin_mod.pathfinder or plugin_mod
            bot.loadPlugin(plugin_func)
            logger.info("Loaded plugin: %s", plugin_name)
        except Exception:
            logger.warning("Failed to load plugin: %s", plugin_name, exc_info=True)

    return bot


def load_plugin(bot: Any, plugin_name: str) -> None:
    """Load a Mineflayer plugin onto an existing bot.

    Args:
        bot: Mineflayer bot instance.
        plugin_name: npm package name of the plugin.
    """
    plugin_mod = _require(plugin_name)
    plugin_func = plugin_mod.plugin or plugin_mod.pathfinder or plugin_mod
    bot.loadPlugin(plugin_func)
    logger.info("Loaded plugin on bot %s: %s", bot.username, plugin_name)


def get_mineflayer() -> Any:
    """Return the mineflayer module for direct API access."""
    return _require("mineflayer")


def get_vec3() -> Any:
    """Return the vec3 module for position/vector math."""
    return _require("vec3")


def get_pathfinder() -> Any:
    """Return the mineflayer-pathfinder module."""
    return _require("mineflayer-pathfinder")
