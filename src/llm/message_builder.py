"""
Phase 1 — Prompt assembly.

Builds the system prompt and message array for LLM calls.
Replaces `models/prompter.js` prompt construction logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Default system prompt template (mirrors JS _default.json "conversing" field)
DEFAULT_CONVERSING_TEMPLATE = (
    "You are an AI Minecraft bot named $NAME that can converse with players, "
    "see, move, mine, build, and interact with the world by using commands.\n"
    "$SELF_PROMPT "
    "Be a friendly, casual, effective, and efficient robot. "
    "Be very brief in your responses, don't apologize constantly, "
    "don't give instructions or make lists unless asked, and don't refuse requests. "
    "Don't pretend to act, use commands immediately when requested. "
    "Do NOT say this: 'Sure, I've stopped. *stops*', "
    "instead say this: 'Sure, I'll stop. !stop'. "
    "Respond only as $NAME, never output '(FROM OTHER BOT)' or pretend to be someone else. "
    "If you have nothing to say or do, respond with just a tab '\\t'.\n"
    "IMPORTANT — Crafting and multi-step tasks:\n"
    "  - Before crafting any complex item, use getCraftingPlan to see the "
    "full ingredient tree and required tools (e.g. crafting table, furnace).\n"
    "  - Some recipes (beds, tools, armor) require a placed crafting table nearby. "
    "Check $STATS for nearby blocks — if a crafting table is already in the "
    "environment, use it instead of making a new one.\n"
    "  - If no crafting table is nearby, collect logs and craft one. "
    "The craftRecipe command automatically places a table if needed, "
    "and automatically picks it up after crafting.\n"
    "  - Check your inventory first. If you lack materials, search for and "
    "collect them before crafting.\n"
    "  - Break complex requests into sequential steps: check requirements → "
    "search → collect → craft intermediates → craft final item.\n"
    "  - After each step, check the result and decide the next action. "
    "Do not skip intermediate steps.\n"
    "Summarized memory:'$MEMORY'\n"
    "$STATS\n$INVENTORY\n$COMMAND_DOCS\n$EXAMPLES\n"
    "Conversation Begin:"
)

DEFAULT_CODING_TEMPLATE = (
    "You are an intelligent mineflayer bot $NAME that plays minecraft by writing "
    "javascript codeblocks. Given the conversation, use the provided skills and world "
    "functions to write a js codeblock that controls the mineflayer bot "
    "``` // using this syntax ```. The code will be executed and you will receive "
    "its output. If an error occurs, write another codeblock and try to fix the problem. "
    "Available runtime values: bot (mineflayer bot), api (safe helper methods like "
    "api.moveTo, api.collectBlocks, api.searchForBlock, api.attack, api.chat, "
    "api.observe, api.inventory, api.waitForAction, api.stop), Vec3, and log(...args). "
    "Be maximally efficient, creative, and correct. Be mindful of previous actions. "
    "Do not use commands !likeThis, only use codeblocks. "
    "$SELF_PROMPT\n"
    "Summarized memory:'$MEMORY'\n$STATS\n$INVENTORY\n$CODE_DOCS\n$EXAMPLES\n"
    "Conversation:"
)


@dataclass
class CommandDef:
    """Definition of a bot command for LLM documentation."""

    name: str
    description: str = ""
    params: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ConversationTurn:
    """A single turn in the conversation history."""

    role: str  # "user", "assistant", "system", or "tool"
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    """Raw OpenAI-format tool calls (for models that use the tool-call protocol)."""
    tool_call_id: str | None = None
    """Matching tool_call id when role='tool' (OpenAI tool-call protocol)."""


class MessageBuilder:
    """Assembles prompts by replacing placeholders with dynamic content.

    Usage:
        builder = MessageBuilder(bot_name="andy", commands=[...])
        system_prompt = builder.build_system_prompt(
            stats="...", inventory="...", examples="...", memory="..."
        )
        messages = builder.build_messages(history=turns, system_prompt=system_prompt)
    """

    def __init__(
        self,
        bot_name: str = "",
        *,
        commands: list[CommandDef] | None = None,
        conversing_template: str | None = None,
        coding_template: str | None = None,
        self_prompt: str = "",
        skill_registry: Any = None,
    ):
        self.bot_name = bot_name
        self.commands = commands or []
        self.conversing_template = conversing_template or DEFAULT_CONVERSING_TEMPLATE
        self.coding_template = coding_template or DEFAULT_CODING_TEMPLATE
        self.self_prompt = self_prompt
        self._skill_registry = skill_registry

    # ── Placeholder substitution ──────────────────────────────────

    def build_system_prompt(
        self,
        *,
        stats: str = "",
        inventory: str = "",
        command_docs: str = "",
        code_docs: str = "",
        examples: str = "",
        memory: str = "",
        for_coding: bool = False,
    ) -> str:
        """Build a complete system prompt by filling all placeholders.

        Args:
            stats: Current bot stats (health, position, etc.)
            inventory: Current inventory summary.
            command_docs: Documentation string for available commands.
            code_docs: Documentation string for code-generation skills.
            examples: Relevant conversation examples.
            memory: Summarized long-term memory.
            for_coding: If True, use the coding template instead of conversing.

        Returns:
            Complete system prompt string.
        """
        template = self.coding_template if for_coding else self.conversing_template

        prompt = template
        prompt = prompt.replace("$NAME", self.bot_name)
        prompt = prompt.replace("$SELF_PROMPT", self.self_prompt)
        prompt = prompt.replace("$MEMORY", memory)
        prompt = prompt.replace("$STATS", stats)
        prompt = prompt.replace("$INVENTORY", inventory)
        prompt = prompt.replace("$COMMAND_DOCS", command_docs)
        prompt = prompt.replace("$CODE_DOCS", code_docs)
        prompt = prompt.replace("$EXAMPLES", examples)

        return prompt

    # ── Command documentation generation ──────────────────────────

    def build_command_docs(self, *, blocked_actions: set[str] | None = None) -> str:
        """Generate the $COMMAND_DOCS string from registered commands.

        Mirrors the JS `getCommandDocs()` function.
        """
        blocked = blocked_actions or set()
        lines = [
            "\n*COMMAND DOCS\n You can use the following commands to perform actions"
            " and queries. Use exactly one command per response, with the syntax"
            " !commandName(params).\n",
        ]
        for cmd in self.commands:
            if cmd.name in blocked:
                continue
            lines.append(f"{cmd.name}: {cmd.description}")
            if cmd.params:
                lines.append("  Params:")
                for pname, pinfo in cmd.params.items():
                    ptype = pinfo.get("type", "string")
                    pdesc = pinfo.get("description", "")
                    lines.append(f"    {pname}: ({ptype}) {pdesc}")
            lines.append("")

        lines.append("*")
        return "\n".join(lines)

    # ── Message array construction ────────────────────────────────

    @staticmethod
    def build_messages(
        history: list[ConversationTurn],
        system_prompt: str,
        *,
        stop_sequence: str = "***",
    ) -> list[dict[str, str]]:
        """Build OpenAI-format messages array from history.

        Args:
            history: Ordered conversation turns.
            system_prompt: Pre-built system prompt (use build_system_prompt first).
            stop_sequence: Appended to each message (JS compatibility).

        Returns:
            List of {"role": ..., "content": ...} dicts.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        for turn in history:
            content = turn.content
            if turn.role in ("user", "assistant"):
                content = f"{content}{stop_sequence}"
            msg: dict[str, Any] = {"role": turn.role, "content": content}
            # Propagate tool_calls for models using the OpenAI tool-call protocol
            if turn.tool_calls:
                msg["tool_calls"] = turn.tool_calls
            if turn.tool_call_id:
                msg["tool_call_id"] = turn.tool_call_id
            messages.append(msg)
        return messages

    # ── Skill / code docs ────────────────────────────────────────

    def build_code_docs(self, *, query: str = "", limit: int = 5) -> str:
        """Generate the $CODE_DOCS string from the skill registry.

        If a query is given, returns the most relevant skills.  Otherwise
        returns all loaded skills.
        """
        if self._skill_registry is None:
            return ""
        return self._skill_registry.get_relevant(query, limit=limit)

    # ── Stats / inventory formatting ──────────────────────────────

    @staticmethod
    def format_stats(
        *,
        health: float = 20,
        food: float = 20,
        position: tuple[float, float, float] = (0, 0, 0),
        gamemode: str = "survival",
        time_of_day: str = "",
        biome: str = "",
        entities_nearby: str = "",
        blocks_nearby: str = "",
    ) -> str:
        """Format bot stats into the $STATS string."""
        x, y, z = position
        parts = [
            f"HEALTH: {health:.0f}/20",
            f"FOOD: {food:.0f}/20",
            f"POS: ({x:.0f}, {y:.0f}, {z:.0f})",
            f"MODE: {gamemode}",
        ]
        if time_of_day:
            parts.append(f"TIME: {time_of_day}")
        if biome:
            parts.append(f"BIOME: {biome}")
        if entities_nearby:
            parts.append(f"\nNEARBY_ENTITIES\n{entities_nearby}")
        if blocks_nearby:
            parts.append(f"\nNEARBY_BLOCKS\n{blocks_nearby}")

        return "\n".join(parts)

    @staticmethod
    def format_inventory(items: list[dict[str, Any]]) -> str:
        """Format inventory items into the $INVENTORY string."""
        if not items:
            return "INVENTORY: empty"
        lines = ["INVENTORY:"]
        for item in items:
            name = item.get("name", "unknown")
            count = item.get("count", 0)
            lines.append(f"  {name} x{count}")
        return "\n".join(lines)

    @staticmethod
    def format_examples(examples: list[list[ConversationTurn]]) -> str:
        """Format conversation examples into the $EXAMPLES string."""
        if not examples:
            return ""

        parts = ["\n*EXAMPLES\n"]
        for i, example in enumerate(examples):
            parts.append(f"--- Example {i + 1} ---")
            for turn in example:
                role_label = turn.role.upper()
                parts.append(f"{role_label}: {turn.content}")
        parts.append("*")
        return "\n".join(parts)
