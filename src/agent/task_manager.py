"""Active task data structures and lifecycle helpers."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from .completion import CompletionSpec, inventory_counts_from_state, position_tuple_from_state
from .effects import ActionEffect
from .models import BotState
from .task_goal_parser import completion_spec_from_message


@dataclass
class PendingTask:
    """Tracks a failed command so the bot can resume with player guidance."""

    command_name: str
    command_args: list[Any]
    command_kwargs: dict[str, Any] | None
    goal: str
    player_name: str
    failed_reason: str


@dataclass
class ActiveTaskEvent:
    """One state transition or command result inside an active task."""

    kind: str
    message: str
    ok: bool | None = None


@dataclass
class ActiveTaskSnapshot:
    """State evidence captured around task boundaries and actions."""

    seq: int
    note: str
    inventory: dict[str, int]
    position: tuple[float, float, float] | None = None


@dataclass
class ActiveTask:
    """Lightweight single-task tracker for one player request."""

    id: str
    requester: str
    original_message: str
    task_type: str = "player_task"
    parent_id: str | None = None
    reason: str = ""
    resume_command: str | None = None
    resume_args: list[Any] | None = None
    resume_kwargs: dict[str, Any] | None = None
    status: str = "running"
    attempts: int = 0
    failures: int = 0
    events: list[ActiveTaskEvent] | None = None
    summary: str = ""
    requires_ctable: bool = False
    planning_strategy: Any = None
    timeout_s: float | None = None
    started_at: float = 0.0
    history_start_index: int = 0
    completion_spec: CompletionSpec | None = None
    recovery_attempts: int = 0
    baseline_inventory: dict[str, int] | None = None
    baseline_position: tuple[float, float, float] | None = None
    snapshots: list[ActiveTaskSnapshot] | None = None
    effects: list[ActionEffect] | None = None

    def __post_init__(self) -> None:
        if self.events is None:
            self.events = []
        if self.snapshots is None:
            self.snapshots = []
        if self.baseline_inventory is None:
            self.baseline_inventory = {}
        if self.effects is None:
            self.effects = []


class TaskManager:
    """Creates and stores the current FILO active task stack."""

    def __init__(self) -> None:
        self.stack: list[ActiveTask] = []

    @property
    def active(self) -> ActiveTask | None:
        return self.stack[-1] if self.stack else None

    def start(
        self,
        *,
        requester: str,
        message: str,
        baseline_state: BotState,
        history_len: int,
        planning_strategy: Any,
        timeout_s: float = 300.0,
    ) -> ActiveTask:
        task = ActiveTask(
            id=f"{int(time.time())}_{slugify_task_name(message)}",
            requester=requester,
            original_message=message,
            requires_ctable=looks_like_crafting_request(message),
            planning_strategy=planning_strategy,
            timeout_s=timeout_s,
            started_at=time.time(),
            history_start_index=max(0, history_len - 1),
            completion_spec=completion_spec_from_message(message),
            baseline_inventory=inventory_counts_from_state(baseline_state),
            baseline_position=position_tuple_from_state(baseline_state),
        )
        task.events.append(ActiveTaskEvent(kind="start", message=f"{requester}: {message}"))
        task.snapshots.append(ActiveTaskSnapshot(
            seq=0,
            note="baseline",
            inventory=inventory_counts_from_state(baseline_state),
            position=position_tuple_from_state(baseline_state),
        ))
        self.stack = [task]
        return task

    @property
    def root(self) -> ActiveTask | None:
        return self.stack[0] if self.stack else None

    def push_subgoal(
        self,
        *,
        requester: str,
        message: str,
        baseline_state: BotState,
        history_len: int,
        planning_strategy: Any,
        parent_id: str,
        reason: str,
        resume_command: str | None = None,
        resume_args: list[Any] | None = None,
        resume_kwargs: dict[str, Any] | None = None,
        timeout_s: float = 120.0,
    ) -> ActiveTask:
        task = ActiveTask(
            id=f"{int(time.time())}_{slugify_task_name(message)}",
            requester=requester,
            original_message=message,
            task_type="subgoal",
            parent_id=parent_id,
            reason=reason,
            resume_command=resume_command,
            resume_args=list(resume_args or []),
            resume_kwargs=dict(resume_kwargs or {}),
            requires_ctable=looks_like_crafting_request(message),
            planning_strategy=planning_strategy,
            timeout_s=timeout_s,
            started_at=time.time(),
            history_start_index=max(0, history_len - 1),
            completion_spec=completion_spec_from_message(message),
            baseline_inventory=inventory_counts_from_state(baseline_state),
            baseline_position=position_tuple_from_state(baseline_state),
        )
        task.events.append(ActiveTaskEvent(
            kind="start",
            message=f"internal subgoal: {message}; reason={reason}",
        ))
        task.snapshots.append(ActiveTaskSnapshot(
            seq=0,
            note="subgoal_baseline",
            inventory=inventory_counts_from_state(baseline_state),
            position=position_tuple_from_state(baseline_state),
        ))
        self.stack.append(task)
        return task

    def pop_active(self, *, status: str, summary: str = "") -> ActiveTask | None:
        if not self.stack:
            return None
        task = self.stack.pop()
        task.status = status
        task.summary = summary
        return task

    def clear(self) -> None:
        self.stack.clear()


def looks_like_crafting_request(message: str) -> bool:
    text = message.strip().lower()
    return bool(re.search(r"\b(craft|make|build|制作|合成|建造|做)\b", text))


def slugify_task_name(text: str, *, limit: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fff]+", "_", text.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return (slug or "task")[:limit]
