# SPDX-License-Identifier: GPL-3.0-or-later
"""The few lines agent hooks and task commands show about open work.

Whatever a session-start hook prints stays in the agent's context for the
rest of the session, so every text here is short and plain: no headings,
one line per task, and a cap on how many tasks are listed.
"""

from __future__ import annotations

from pathlib import Path

from ww.executable import ww_command
from ww.open_work import OpenTask

from .records import Interruption

SESSION_TASK_LIMIT = 5
# How far back the entry commands look for interrupted tasks.
RECENT_INTERRUPTION_DAYS = 3
STOP_TASK_LIMIT = 3

_OPERATOR_REASONS = {
    "handler_failed": "an automatic handler failed",
    "work_failed": "the work failed",
    "child_failed": "a child task failed",
    "interrupted_command": "a command was interrupted",
    "loop_limit": "a loop reached its limit",
}


def session_context(
    open_tasks: tuple[OpenTask, ...],
    interruptions: dict[str, Interruption],
    root: Path,
    *,
    compacted: bool,
) -> str:
    """What a session learns about ww when it starts, resumes, or compacts."""
    ww = ww_command()
    lines = []
    if compacted:
        lines.append("Context was compacted; ww's task state is authoritative.")
    lines.append(
        f"This project coordinates work through ww: `{ww} discover` lists its "
        "workflows."
    )
    if open_tasks:
        lines.append("Unfinished ww tasks, newest first:")
        for task in open_tasks[:SESSION_TASK_LIMIT]:
            lines.append(_task_line(task, root, ww))
            interruption = interruptions.get(task.task_id)
            if interruption is not None:
                lines.append("  " + interruption_notice(interruption, task.task_id))
        if len(open_tasks) > SESSION_TASK_LIMIT:
            lines.append(
                f"… and {len(open_tasks) - SESSION_TASK_LIMIT} more; "
                f"`{ww} status <task-id>` shows one."
            )
    return "\n".join(lines) + "\n"


def _task_line(task: OpenTask, root: Path, ww: str) -> str:
    step = task.label
    if task.operator_reason is not None:
        state = "awaiting the operator: " + _OPERATOR_REASONS.get(
            task.operator_reason, task.operator_reason
        )
    else:
        state = (task.item_status or task.run_status).replace("_", " ")
    parts = [f"- {task.task_id} ({task.workflow}, {task.agent}) {step}: {state}"]
    parts.append(f"in {_display(task.workspace, root)}")
    parts.append(f"resume: `{ww} instruction {task.task_id} --role manager`")
    if task.agent_step_in_progress and task.run_id:
        parts.append(
            f"worker: `{ww} instruction {task.task_id} --run {task.run_id} "
            "--role worker`"
        )
    return " · ".join(parts)


def stop_reminder(tasks: tuple[OpenTask, ...]) -> str:
    """Ask, once, that the open step be closed before the agent stops."""
    ww = ww_command()
    listed = tasks[:STOP_TASK_LIMIT]
    names = "; ".join(
        f"{task.task_id} step `{task.label}`" for task in listed
    )
    example = listed[0].task_id
    return (
        f"ww: {names} is still in progress. If the work is done, record it with "
        f"`{ww} complete {example} --role worker ...` as its instruction shows; if "
        f'it cannot finish, run `{ww} fail {example} --role worker '
        f'--error "<reason>"`. '
        "A manager waiting on a worker, or a deliberate pause, may simply stop "
        "again: ww reminds only once."
    )


def interruption_notice(interruption: Interruption, task_id: str) -> str:
    """Tell whoever picks the task up that its last session stopped short."""
    ww = ww_command()
    who = interruption.agent + (
        f", {interruption.reason}" if interruption.reason else ""
    )
    step = interruption.step or interruption.item_name or "its step"
    return (
        f"Interrupted: the previous session ({who}) stopped at {interruption.at} "
        f"during `{step}` (attempt {interruption.attempt}). Before continuing, check "
        "`git status` in the task workspace, review the diff and ww's recorded "
        f"commits (`{ww} extension ww/git commits {task_id}`), and compare them "
        "with the step's requirements; then complete, continue, or fail the step."
    )


def _display(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root.resolve())
    except ValueError:
        return str(path)
    return str(relative) if relative.parts else "the root"


def recent_interruptions_pointer(count: int) -> str | None:
    """One line for ``discover`` and ``lookup``, only when there is news.

    Those entry commands are what an agent without hooks runs first, so the
    line is the whole of their "first start" check; the details live in
    ``ww interrupted`` and on each task's own commands.
    """
    if not count:
        return None
    tasks = "task was" if count == 1 else "tasks were"
    return (
        f"{count} {tasks} interrupted in the last {RECENT_INTERRUPTION_DAYS} days; "
        f"run `{ww_command()} interrupted` before starting new work."
    )
