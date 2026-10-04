# SPDX-License-Identifier: GPL-3.0-or-later
"""The few lines agent hooks and task commands show about open work.

Whatever a session-start hook prints stays in the agent's context for the
rest of the session, so every text here is short and plain: no headings,
one line per task, and a cap on how many tasks are listed.
"""

from __future__ import annotations

from pathlib import Path

from ww.executable import ww_command
from ww.open_work import OpenTask, UnreadableTask

from .records import Interruption

SESSION_TASK_LIMIT = 5
STOP_TASK_LIMIT = 3

_OPERATOR_REASONS = {
    "handler_failed": "an automatic handler failed",
    "work_failed": "the work failed",
    "child_failed": "a child task failed",
    "handler_interrupted": "a command was interrupted",
    "loop_limit": "a loop reached its limit",
    "fix_limit": "a step's checks kept failing",
    "check_disputed": "a worker disputed a check",
    "value_unavailable": "a step's template value is not available yet",
    "pass_incomplete": "an items pass left items without their recorded outcome",
}


def session_context(
    open_tasks: tuple[OpenTask, ...],
    interruptions: dict[str, Interruption],
    root: Path,
    *,
    compacted: bool,
    unreadable: tuple[UnreadableTask, ...] = (),
    on_request: bool = False,
    skipped: int = 0,
) -> str:
    """What a session learns about ww when it starts, resumes, or compacts.

    ``open_tasks`` are the recent unfinished tasks; ``skipped`` counts the
    tasks the scan left unread as older, which ``discover`` still covers.
    A step in progress without an interruption marker ended with no hook
    run, so it gets a notice of its own, except after a compaction: the
    session that holds it is the one carrying on.
    """
    ww = ww_command()
    lines = []
    if compacted:
        lines.append("Context was compacted; ww's task state is authoritative.")
    lines.append(
        "This project has ww available on request only: use it only when the "
        "user explicitly asks for ww; otherwise work without it and do not "
        f"ask. `{ww} discover` lists its workflows."
        if on_request
        else f"This project coordinates work through ww: `{ww} discover` lists "
        "its workflows."
    )
    if open_tasks:
        lines.append("Unfinished ww tasks, newest first:")
        for task in open_tasks[:SESSION_TASK_LIMIT]:
            lines.append(task_line(task, root, ww))
            interruption = interruptions.get(task.task_id)
            if interruption is not None:
                lines.append("  " + interruption_notice(interruption, task.task_id))
            elif task.agent_step_in_progress and not compacted:
                lines.append("  " + abrupt_end_notice(task))
        if len(open_tasks) > SESSION_TASK_LIMIT:
            lines.append(
                f"… and {len(open_tasks) - SESSION_TASK_LIMIT} more; "
                f"`{ww} status <task-id>` shows one."
            )
    if open_tasks or skipped:
        lines.append(f"`{ww} discover` lists every unfinished task.")
    if unreadable:
        lines.append(unreadable_notice(unreadable))
    return "\n".join(lines) + "\n"


def unreadable_notice(unreadable: tuple[UnreadableTask, ...]) -> str:
    """One line naming the tasks ww cannot read, without their errors."""
    names = ", ".join(task.task_id for task in unreadable)
    return (
        f"ww cannot read the state of {names}; other tasks and new work are "
        f"unaffected. `{ww_command()} discover` shows why; ask the operator "
        "before touching them."
    )


def task_line(task: OpenTask, root: Path, ww: str) -> str:
    """One unfinished task with its step, state, workspace and resume commands."""
    step = task.label
    if task.operator_reason is not None:
        state = "awaiting the operator: " + _OPERATOR_REASONS.get(
            task.operator_reason, task.operator_reason
        )
    else:
        state = (task.item_status or task.run_status).replace("_", " ")
    parts = [f"- {task.task_id} ({task.workflow}, {task.agent}) {step}: {state}"]
    parts.append(f"in {display_workspace(task.workspace, root)}")
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
    names = "; ".join(f"{task.task_id} step `{task.label}`" for task in listed)
    example = listed[0].task_id
    return (
        f"ww: {names} is still in progress. If the work is done, record it with "
        f"`{ww} complete {example} --role worker ...` as its instruction shows; if "
        f"it cannot finish, run `{ww} fail {example} --role worker "
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
    if interruption.in_conversation:
        lead = (
            f"Interrupted: the previous session ({who}) stopped at {interruption.at} "
            f"while `{step}` (attempt {interruption.attempt}) was talking with the "
            "operator. "
        )
        if interruption.recovered_entries:
            return lead + (
                f"{interruption.recovered_entries} entries of the conversation "
                "were recovered from the session transcript; read them on the "
                "step's page and continue from the last unanswered point."
            )
        return lead + (
            "The conversation was not recorded; ask the operator where you were."
        )
    return (
        f"Interrupted: the previous session ({who}) stopped at {interruption.at} "
        f"during `{step}` (attempt {interruption.attempt}). Before continuing, check "
        "`git status` in the task workspace, review the diff and ww's recorded "
        f"commits (`{ww} extension ww/git commits {task_id}`), and compare them "
        "with the step's requirements; then complete, continue, or fail the step."
    )


def abrupt_end_notice(task: OpenTask) -> str:
    """Tell whoever picks the task up that its last session left no trace.

    The ``interrupt`` hook never runs when a tab is closed, the agent crashes
    or is killed, so the step is still in progress with no marker.
    """
    lead = (
        f"Left in progress at {task.updated_at} by {task.agent} with no recorded "
        "end, probably a closed session: "
    )
    if task.in_conversation:
        return (
            lead + "the step's page shows the conversation so far; pick it up at "
            "the last unanswered question."
        )
    return (
        lead + f"check its page (`{ww_command()} instruction {task.task_id} "
        "--role manager`) before continuing."
    )


def display_workspace(path: Path, root: Path) -> str:
    """A workspace relative to the root, or "the root" itself."""
    try:
        relative = path.relative_to(root.resolve())
    except ValueError:
        return str(path)
    return str(relative) if relative.parts else "the root"


def recent_interruptions_pointer(count: int, days: int) -> str | None:
    """One line for ``discover`` and ``lookup``, only when there is news.

    Those entry commands are what an agent without hooks runs first, so the
    line is the whole of their "first start" check; the details live in
    ``ww interrupted`` and on each task's own commands.
    """
    if not count:
        return None
    tasks = "task was" if count == 1 else "tasks were"
    window = "day" if days == 1 else f"{days} days"
    return (
        f"{count} {tasks} interrupted in the last {window}; "
        f"run `{ww_command()} interrupted` before starting new work."
    )
