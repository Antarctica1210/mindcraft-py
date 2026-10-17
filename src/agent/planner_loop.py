"""Observe-plan-act-check loop for active agent turns."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from llm import PlanningStrategy
from llm.dispatch_gate import DispatchDecision
from llm.message_builder import ConversationTurn
from llm.tool_parser import parse_response

from .command_registry import (
    NAVIGATION_ONLY_ALLOWED_COMMANDS as _NAVIGATION_ONLY_ALLOWED_COMMANDS,
)
from .command_registry import QUERY_COMMANDS as _QUERY_COMMANDS
from .command_registry import command_defs
from .failures import classify_failure as _classify_failure
from .failures import is_hard_failure as _is_hard_failure
from .task_goal_parser import is_navigation_only_request as _is_navigation_only_request
from .task_manager import ActiveTask, PendingTask

logger = logging.getLogger(__name__)

_MAX_REPEATED_QUERY_PER_NAME = 3
_MAX_CONSECUTIVE_QUERY_CALLS = 4
_MAX_TOOL_CALLS_PER_TURN = 25


def _describe_action(name: str, args: list[Any]) -> str:
    cmd_name = name.lstrip("!")
    defs = command_defs()
    cmd = defs.get(cmd_name)
    param_names = list(cmd.params.keys()) if cmd else []

    if not args:
        if cmd:
            desc = cmd.description.rstrip(".")
            return desc[0].lower() + desc[1:] if desc else cmd_name
        return name

    parts: list[str] = []
    for i, arg in enumerate(args):
        label = param_names[i] if i < len(param_names) else str(i)
        parts.append(f"{label}={arg}")

    return f"{cmd_name}({', '.join(parts)})"


def _tool_call_required_reminder() -> str:
    return (
        "You must use a command to respond to this request — either emit "
        "a tool call/function call OR use the !commandName(args) syntax in your "
        "text reply (e.g. !craftRecipe(\"oak_planks\", 1) or "
        "!searchForEntity(\"sheep\", 64)). Do NOT respond with plain text "
        "alone. If you are unsure which command fits, call !help or !stats "
        "first."
    )


def _task_boundary_block_reason(task: ActiveTask | None, command_name: str) -> str | None:
    if task is None or task.status != "running":
        return None
    name = command_name.lstrip("!")
    if _is_navigation_only_request(task.original_message):
        if name in _NAVIGATION_ONLY_ALLOWED_COMMANDS:
            return None
        return (
            f"Ignored {name} because the current task is only "
            f"{task.original_message!r}. Use a movement/follow command instead."
        )
    return None


def _command_call_key(
    name: str,
    args: list[Any],
    kwargs: dict[str, Any] | None = None,
) -> str:
    payload = {
        "name": name.lstrip("!"),
        "args": args,
        "kwargs": kwargs or {},
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _query_repeat_reminder(name: str, result: str) -> str:
    return (
        f"You already called {name} with the same arguments and got this result:\n"
        f"{result[:1200]}\n\n"
        "Do NOT call the same query again. Use this information to take the next "
        "concrete action toward the player's request. For crafting tasks, choose "
        "an action such as craftRecipe for craftable items, collectBlocks/search "
        "for missing materials, or ask the player if the materials cannot be obtained."
    )


def _log_preview(value: Any, *, limit: int = 500) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "..."


class PlannerLoop:
    """Run the observe-plan-act-check loop for an Agent host."""

    def __init__(self, host: Any) -> None:
        self._host = host

    async def run(
        self, *, max_responses: int, use_tools: bool, turn_id: int | None = None,
    ) -> bool:
        """Drive plan → parse → execute repeats for tool-capable intents.

        For ``use_tools=False`` (question/chitchat) the LLM is called with
        ``tool_choice="none"`` and the reply is spoken once, no commands.

        For ``use_tools=True`` (action_command) we use ``tool_choice="auto"``
        so the model can both reason in plain text AND emit tool calls in the
        same response — this matches the JS original's "I need wool. !attack
        ..." pattern, which is what carried weaker models through the bed-craft
        trace. The F2 retry path catches the case where a model produces text
        only without a tool call, re-prompting once with a reminder.
        """
        host = self._host
        used_command = False
        if turn_id is None:
            turn_id = host._turn_id
        bot_state = await host.observe()
        if not host._running:
            return False
        if turn_id != host._turn_id:
            logger.info(
                "Plan loop superseded before start: turn=%s current=%s",
                turn_id,
                host._turn_id,
            )
            return False
        dispatch_decision: DispatchDecision | None = None
        if use_tools and host._dispatch_gate is not None:
            dispatch_decision = await host._dispatch_gate.classify(host._last_message, bot_state)
            host._apply_delivery_joint_judgement(dispatch_decision)
        if use_tools:
            if await host._maybe_complete_satisfied_craft(bot_state):
                return True
            if host._dispatch_allows_shortcut(dispatch_decision, "direct_action"):
                direct_result = await host._try_direct_action_command()
                if direct_result is not None:
                    return direct_result
            if host._dispatch_allows_shortcut(dispatch_decision, "direct_cook"):
                cook_result = await host._try_direct_cook_request(bot_state)
                if cook_result is not None:
                    return cook_result
            if host._direct_shortcut_retry_reason is None:
                if host._dispatch_allows_shortcut(dispatch_decision, "direct_item"):
                    block_reason = host._direct_shortcut_block_reason(
                        "direct_item",
                        bot_state,
                    )
                    if block_reason is None:
                        item_result = await host._try_direct_item_request(bot_state)
                        if item_result is not None:
                            return item_result
                    else:
                        logger.info(
                            "Direct shortcut skipped route=direct_item reason=%s",
                            block_reason,
                        )
            if host._direct_shortcut_retry_reason is None:
                if host._dispatch_allows_shortcut(dispatch_decision, "skill"):
                    skill_used = await host._try_phase5a_skill(host._last_message, bot_state)
                    if skill_used:
                        return True
            if host._direct_shortcut_retry_reason is not None:
                logger.info(
                    "Limiting LLM recovery after direct shortcut failure to one response: %s",
                    host._direct_shortcut_retry_reason,
                )
                max_responses = min(max_responses, 1)
                host._direct_shortcut_retry_reason = None
        tool_choice = "auto" if use_tools else "none"
        consecutive_failures = 0
        last_failed: tuple[str, str] | None = None  # (command, args_str)
        no_tool_retries = 0  # F2: track re-prompts when LLM omits tool calls
        seen_query_results: dict[str, str] = {}
        warned_repeated_queries: set[str] = set()
        # F1: count calls to each query command by name, regardless of args
        # — catches the paraphrased-query loop case (searchWiki "bed recipe"
        # → "crafting bed" → "minecraft bed") that the exact-arg check above
        # misses.
        query_command_counts: dict[str, int] = {}
        warned_query_command_loops: set[str] = set()
        nearby_miss_recoveries: set[tuple[str, str]] = set()
        # F3a: consecutive read-only query calls — the *primary* loop guard.
        # Resets on any non-query (action) command, so legitimate chains
        # like searchForEntity → attack → searchForEntity → attack are
        # never penalised.  Targets the actual failure mode (query spam).
        consecutive_query_calls = 0
        # F3b: absolute backstop on total tool-call attempts per user turn.
        # Set high (matches JS empirical iteration count + headroom) so
        # genuine multi-step crafting (~19 iterations on bed-craft) is not
        # cut off; only truly runaway models hit this.
        total_tool_calls = 0

        for _ in range(max_responses):
            if turn_id != host._turn_id:
                logger.info("Plan loop superseded: turn=%s current=%s", turn_id, host._turn_id)
                return used_command
            if host._shut_up or not host._running:
                break
            if host._safety and host._safety.state.interrupted:
                logger.warning("Plan loop halted: safety interrupt active")
                break
            if host._active_task and host._active_task.timeout_s is not None:
                elapsed = time.time() - host._active_task.started_at
                if elapsed > host._active_task.timeout_s:
                    logger.warning(
                        "Task %s timed out after %.0fs (limit %.0fs)",
                        host._active_task.id,
                        elapsed,
                        host._active_task.timeout_s,
                    )
                    await host._send_chat(
                        f"I've been working on this for {int(elapsed)}s — "
                        "that's too long. What should I do differently?"
                    )
                    await host._finish_active_task(
                        "failed",
                        reason=f"Task timed out after {elapsed:.0f}s",
                    )
                    return False
            if use_tools and await host._maybe_complete_satisfied_craft(bot_state):
                return True

            # Flush behavior log so the LLM sees what modes have been doing
            # (mirrors JS behavior_log flush before each promptConvo call).
            b_log = host._modes.flush_behavior_log()
            if b_log:
                host._history.append(
                    ConversationTurn(role="system", content=f"Recent behaviors:\n{b_log}"),
                )

            llm_response = await host.plan(bot_state, tool_choice=tool_choice)
            if turn_id != host._turn_id:
                logger.info(
                    "Dropping stale LLM response: turn=%s current=%s",
                    turn_id,
                    host._turn_id,
                )
                return used_command

            # Concise action summary: tool calls first, then truncated reply
            if llm_response.tool_calls:
                tc_names = [tc.function.name for tc in llm_response.tool_calls]
                logger.info("<<< tools=%s", ", ".join(tc_names))
            elif llm_response.content.strip():
                preview = llm_response.content.strip()[:80]
                logger.info("<<< %s", preview)
            else:
                logger.info("<<< (empty)")

            # Action path + tool_choice="required": some reasoning models can
            # spend tokens internally, return finish_reason="stop", and expose
            # neither content nor tool calls. Give them the same one-shot
            # correction we use for plain text without tool calls.
            if use_tools and not llm_response.tool_calls and not llm_response.content.strip():
                if no_tool_retries < 1:
                    no_tool_retries += 1
                    logger.warning(
                        "LLM produced empty response on action_command intent — "
                        "injecting tool-call reminder (retry %d/1)", no_tool_retries,
                    )
                    host._history.append(ConversationTurn(
                        role="system",
                        content=_tool_call_required_reminder(),
                    ))
                    continue
                logger.debug("Empty LLM response with no tool calls after retry — ending turn")
                break

            parsed = parse_response(
                llm_response.content,
                tool_calls=llm_response.tool_calls,
            )

            # Question / chitchat path: just remember and speak the reply,
            # no commands.
            if not use_tools:
                host._history.append(
                    ConversationTurn(
                        role="assistant",
                        content=llm_response.content,
                        tool_calls=llm_response.tool_calls,
                    ),
                )
                await host._send_chat(llm_response.content.strip())
                break

            # Non-action paths can still produce neither tool calls nor text.
            if not parsed.has_command and not llm_response.content.strip():
                logger.debug("Empty LLM response with no tool calls — ending turn")
                break

            if not parsed.has_command:
                # F2: Action path but model produced plain text with no tool
                # calls.  If we haven't retried yet, inject a reminder and
                # re-prompt once — the LLM may have forgotten it must produce
                # a tool call.  If it still refuses, speak the text so the
                # player at least sees the model's reasoning.
                if use_tools and no_tool_retries < 1:
                    no_tool_retries += 1
                    logger.warning(
                        "LLM produced no tool call on action_command intent — "
                        "injecting reminder (retry %d/1)", no_tool_retries,
                    )
                    host._history.append(ConversationTurn(
                        role="system",
                        content=_tool_call_required_reminder(),
                    ))
                    continue  # re-call plan() with tool_choice="required"

                # Speak plain text (question/chitchat path, or retry exhausted)
                await host._send_chat(llm_response.content.strip())
                break

            if (
                use_tools
                and host._active_task is not None
                and host._active_task.planning_strategy == PlanningStrategy.ROLLING_PLAN
                and len(parsed.commands) > 1
            ):
                dropped = [cmd.name for cmd in parsed.commands[1:]]
                logger.info(
                    "Rolling plan produced multiple commands; executing only first=%s "
                    "dropped=%s",
                    parsed.commands[0].name,
                    dropped,
                )
                host._history.append(ConversationTurn(
                    role="system",
                    content=(
                        "Rolling-plan mode executes only one command per turn. "
                        f"Ignored later commands from the previous response: {', '.join(dropped)}. "
                        "Keep future plan steps as plain text with no !command syntax."
                    ),
                ))
                if llm_response.tool_calls:
                    llm_response = llm_response.model_copy(
                        update={"tool_calls": llm_response.tool_calls[:1]}
                    )
                parsed.commands = parsed.commands[:1]
                parsed.has_command = True

            repeated_query: tuple[str, str] | None = None
            if use_tools:
                for cmd in parsed.commands:
                    cmd_key = _command_call_key(
                        cmd.name,
                        cmd.args,
                        getattr(cmd, "kwargs", None) or None,
                    )
                    normalized_name = cmd.name.lstrip("!")
                    if normalized_name in _QUERY_COMMANDS and cmd_key in seen_query_results:
                        repeated_query = (normalized_name, cmd_key)
                        break

            if repeated_query is not None:
                normalized_name, cmd_key = repeated_query
                if cmd_key not in warned_repeated_queries:
                    warned_repeated_queries.add(cmd_key)
                    logger.warning(
                        "LLM repeated query command %s with same arguments — "
                        "injecting next-step reminder",
                        normalized_name,
                    )
                    host._history.append(ConversationTurn(
                        role="system",
                        content=_query_repeat_reminder(
                            normalized_name,
                            seen_query_results[cmd_key],
                        ),
                    ))
                    continue
                logger.warning(
                    "LLM repeated query command %s after reminder — attempting fallback",
                    normalized_name,
                )
                fallback_result = await host._fallback_after_repeated_query(
                    normalized_name,
                    bot_state,
                    seen_query_results.get(cmd_key, ""),
                )
                if fallback_result is not None:
                    return fallback_result or used_command
                await host._send_chat(
                    "I'm stuck repeating the same check instead of taking the next step."
                )
                return used_command

            # F1: count by command name only — catches paraphrased-arg loops
            # that the exact-arg check above doesn't catch.
            query_loop_name: str | None = None
            if use_tools:
                for cmd in parsed.commands:
                    normalized = cmd.name.lstrip("!")
                    if normalized in _QUERY_COMMANDS:
                        query_command_counts[normalized] = (
                            query_command_counts.get(normalized, 0) + 1
                        )
                        if (
                            query_command_counts[normalized]
                            >= _MAX_REPEATED_QUERY_PER_NAME
                        ):
                            query_loop_name = normalized
                            break

            if query_loop_name is not None:
                count = query_command_counts[query_loop_name]
                if query_loop_name not in warned_query_command_loops:
                    warned_query_command_loops.add(query_loop_name)
                    logger.warning(
                        "LLM called %s %d times in this turn (different args) — "
                        "forcing pivot to concrete action",
                        query_loop_name, count,
                    )
                    host._history.append(ConversationTurn(
                        role="system",
                        content=(
                            f"You have already called {query_loop_name} {count} "
                            "times in this turn with different arguments. You "
                            "have enough information. Choose a concrete next "
                            "action NOW: craftRecipe, collectBlocks, "
                            "searchForBlock, searchForEntity, or chat back to "
                            "the player to ask for help. Do NOT call any "
                            "read-only query command again this turn."
                        ),
                    ))
                    continue
                logger.warning(
                    "LLM stuck on %s after pivot reminder — ending turn",
                    query_loop_name,
                )
                await host._send_chat(
                    f"I keep looking up '{query_loop_name}' instead of acting. "
                    "Can you tell me what to do next?"
                )
                return used_command

            # Append assistant turn — include tool_calls if present so
            # models that expect the OpenAI tool-call protocol see them.
            host._history.append(
                ConversationTurn(
                    role="assistant",
                    content=llm_response.content,
                    tool_calls=llm_response.tool_calls,
                ),
            )

            # Speak the pre-command text so the player knows what the bot
            # is doing (mirrors JS routeResponse before executeCommand).
            if parsed.text.strip():
                await host._send_chat(parsed.text.strip())

            any_blocked = False
            for cmd in parsed.commands:
                if turn_id != host._turn_id:
                    logger.info(
                        "Command chain superseded: turn=%s current=%s",
                        turn_id,
                        host._turn_id,
                    )
                    return used_command
                if host._shut_up:
                    break
                if host._safety and host._safety.state.interrupted:
                    logger.warning("Command chain halted: safety interrupt")
                    break

                # F3a: track consecutive read-only queries. Resets to 0 on
                # any non-query command, so the JS bed-craft pattern
                # (search → attack → search → attack, ~11 iterations) is
                # never penalised. Catches the real failure mode: pure
                # query spam without any concrete action in between.
                cmd_name_normalized = cmd.name.lstrip("!")
                if cmd_name_normalized in _QUERY_COMMANDS:
                    consecutive_query_calls += 1
                    if consecutive_query_calls > _MAX_CONSECUTIVE_QUERY_CALLS:
                        logger.warning(
                            "Consecutive-query cap (%d) hit — bot is stuck "
                            "in read-only loop; ending turn",
                            _MAX_CONSECUTIVE_QUERY_CALLS,
                        )
                        await host._send_chat(
                            f"I've looked things up {consecutive_query_calls} "
                            "times in a row without doing anything. What "
                            "should I actually do?"
                        )
                        return used_command
                else:
                    consecutive_query_calls = 0

                # F3b: absolute total backstop. Counts everything; only
                # fires against runaway models. Set above JS's 19-iter
                # bed-craft floor so genuine long tasks complete.
                total_tool_calls += 1
                if total_tool_calls > _MAX_TOOL_CALLS_PER_TURN:
                    logger.warning(
                        "Total tool-call backstop (%d) reached — ending turn",
                        _MAX_TOOL_CALLS_PER_TURN,
                    )
                    recent_names = [a["name"] for a in host._recent_actions]
                    await host._send_chat(
                        f"I've tried {_MAX_TOOL_CALLS_PER_TURN} steps without "
                        f"finishing — most recently {', '.join(recent_names[-3:]) or cmd.name}. "
                        "Could you tell me what to try next?"
                    )
                    return used_command

                # Announce each step before executing
                cmd_args = cmd.args
                cmd_kwargs = getattr(cmd, "kwargs", None) or None
                cmd_key = _command_call_key(cmd.name, cmd_args, cmd_kwargs)
                normalized_name = cmd.name.lstrip("!")
                blocked_noop = host._blocked_model_control_command(normalized_name)
                if blocked_noop is not None:
                    logger.warning(
                        "Blocked model-generated control command: %s result=%s",
                        normalized_name,
                        blocked_noop,
                    )
                    host._history.append(ConversationTurn(role="system", content=blocked_noop))
                    host._active_task_add_event(
                        "blocked_command",
                        f"{normalized_name}: {blocked_noop}",
                        ok=False,
                    )
                    any_blocked = True
                    continue
                task_boundary_noop = _task_boundary_block_reason(
                    host._active_task,
                    normalized_name,
                )
                if task_boundary_noop is not None:
                    logger.warning(
                        "Blocked command outside active task boundary: %s result=%s",
                        normalized_name,
                        task_boundary_noop,
                    )
                    host._history.append(
                        ConversationTurn(role="system", content=task_boundary_noop)
                    )
                    host._active_task_add_event(
                        "blocked_command",
                        f"{normalized_name}: {task_boundary_noop}",
                        ok=False,
                    )
                    any_blocked = True
                    continue
                action_desc = _describe_action(
                    cmd.name,
                    cmd_args if cmd_args else list((cmd_kwargs or {}).values()),
                )
                logger.info(
                    "Bot action step start: command=%s args=%s kwargs=%s desc=%s",
                    cmd.name,
                    cmd_args,
                    cmd_kwargs or {},
                    action_desc,
                )
                await host._send_chat(action_desc)

                command_run = await host._run_agent_command(cmd.name, cmd_args, cmd_kwargs)
                result = command_run.raw
                action_result = command_run.result
                logger.info(
                    "Bot action step result: command=%s elapsed_ms=%.1f ok=%s summary=%s result=%s",
                    cmd.name,
                    command_run.elapsed_ms,
                    action_result.ok,
                    action_result.summary,
                    _log_preview(result),
                )
                used_command = True
                host._record_action(cmd.name, cmd_args, result)
                host._active_task_add_event(
                    "action_result",
                    f"{cmd.name} args={cmd_args} kwargs={cmd_kwargs or {}} -> {result}",
                    ok=action_result.ok,
                )
                host._active_task_add_effects(normalized_name, action_result)
                await host._record_event("action_result", {
                    "command": cmd.name,
                    "args": cmd_args,
                    "kwargs": cmd_kwargs,
                    "result": result,
                    "summary": action_result.summary,
                    "effects": [
                        {
                            "type": effect.type,
                            "name": effect.name,
                            "count": effect.count,
                            "item": effect.item,
                        }
                        for effect in action_result.effects
                    ],
                    "failure": (
                        {
                            "kind": action_result.failure.kind,
                            "target": action_result.failure.target,
                            "required": action_result.failure.required,
                            "missing": action_result.failure.missing,
                        }
                        if action_result.failure is not None else None
                    ),
                })

                if result is None:
                    await host._send_chat(f"{action_desc} — no response")
                    break  # no feedback = stop chaining

                # Use role="tool" with tool_call_id when responding to an
                # OpenAI-format tool call; the provider rejects role="system"
                # after an assistant message that contains tool_calls.
                result_role: str = "system"
                result_tc_id: str | None = None
                for tc in llm_response.tool_calls:
                    tc_name = tc.function.name
                    if tc_name.startswith("action:"):
                        tc_name = tc_name[7:]
                    if tc_name == cmd.name:
                        result_role = "tool"
                        result_tc_id = tc.id
                        break

                host._history.append(
                    ConversationTurn(
                        role=result_role,
                        content=action_result.history_content,
                        tool_call_id=result_tc_id,
                    ),
                )

                # ── Failure classification ───────────────────────────
                is_fail = result and "FAILED:" in result
                args_str = ",".join(str(a) for a in cmd_args)
                if not args_str and cmd_kwargs:
                    args_str = ",".join(f"{k}={v}" for k, v in cmd_kwargs.items())
                this_fail_key = (cmd.name, args_str) if is_fail else None
                failure_cause = action_result.failure
                if failure_cause is None and is_fail:
                    failure_cause = _classify_failure(result)

                if (
                    failure_cause is not None
                    and failure_cause.kind in {"bridge_failure", "hard_failure"}
                ) or _is_hard_failure(result):
                    failure_kind = (
                        failure_cause.kind
                        if failure_cause is not None else "hard_failure"
                    )
                    logger.warning(
                        "Hard command failure — asking player: kind=%s result=%s",
                        failure_kind,
                        result,
                    )
                    await host._send_chat(f"Failed: {result}")
                    host._pending_task = PendingTask(
                        command_name=cmd.name,
                        command_args=list(cmd_args),
                        command_kwargs=dict(cmd_kwargs or {}),
                        goal=host._last_message,
                        player_name=host._last_sender or "player",
                        failed_reason=result,
                    )
                    await host._finish_active_task("failed", reason=result)
                    return used_command

                if is_fail:
                    if await host._recover_missing_dependency(
                        normalized_name,
                        cmd_args,
                        cmd_kwargs,
                        result,
                    ):
                        consecutive_failures = 0
                        last_failed = None
                        bot_state = await host.observe()
                        if not host._running:
                            return used_command
                        if host._active_task is None:
                            return True
                        continue

                    if await host._recover_nearby_miss(
                        normalized_name,
                        cmd_args,
                        cmd_kwargs,
                        result,
                        nearby_miss_recoveries,
                    ):
                        consecutive_failures = 0
                        last_failed = None
                        bot_state = await host.observe()
                        if not host._running:
                            return used_command
                        continue

                    consecutive_failures += 1
                    # Same command + args failed twice → inject hint
                    if (
                        last_failed is not None
                        and this_fail_key is not None
                        and this_fail_key == last_failed
                    ):
                        host._history.append(ConversationTurn(
                            role="system",
                            content=(
                                f"You just tried {cmd.name}({args_str}) and it failed "
                                f"again with the same error. Do NOT retry the same command "
                                f"with the same arguments — try a completely different "
                                f"approach instead. Check what went wrong and replan."
                            ),
                        ))
                    last_failed = this_fail_key

                    # 3 consecutive failures (any command) → stop and ask
                    if consecutive_failures >= 3:
                        logger.warning("3 consecutive failures — asking player to guide")
                        await host._send_chat(
                            "I'm stuck — I've tried several things but they're not working. "
                            "What should I try next?"
                        )
                        await host._finish_active_task(
                            "failed",
                            reason="3 consecutive action failures.",
                        )
                        return used_command
                else:
                    consecutive_failures = 0
                    last_failed = None
                    if normalized_name in _QUERY_COMMANDS:
                        seen_query_results[cmd_key] = result

                # Report success to the player
                await host._send_chat(action_result.summary)

                # Refresh bot state so the next LLM call sees updated
                # inventory, position, etc. (mirrors JS in-line !stats/!inventory).
                bot_state = await host.observe()
                if not host._running:
                    return used_command
                if await host._maybe_finish_after_command_completion(
                    normalized_name,
                    result,
                    bot_state,
                    effects_already_recorded=True,
                ):
                    return True
                if use_tools and await host._maybe_complete_satisfied_craft(bot_state):
                    return True

            # If the only command(s) this turn were blocked (e.g. model
            # proposed !stop without the player asking), give the LLM another
            # chance instead of immediately failing the task.
            if any_blocked and not used_command:
                continue

        host._history = host._convo_mgr.compress_history(host._history)
        if use_tools and host._active_task is not None:
            recovered_after_clear = await host._maybe_retry_cook_after_furnace_clear()
            if recovered_after_clear:
                return True
            if host._completion_engine.contract_satisfied(host._active_task, bot_state):
                await host._finish_active_task(
                    "succeeded",
                    reason="Plan loop ended after completion spec was satisfied.",
                )
                return True
            if (
                host._active_task.completion_spec is not None
                and host._active_task.completion_spec.kind != "unknown"
            ):
                await host._finish_active_task(
                    "failed",
                    reason="Plan loop ended before completion spec was satisfied.",
                )
                return used_command
            await host._finish_active_task(
                "succeeded" if used_command else "failed",
                reason="Plan loop ended.",
            )
        return used_command

