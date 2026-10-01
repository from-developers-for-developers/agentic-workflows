# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only view of the work still open in a project root.

Agent hooks ask two questions of ww's state many times a session: which
tasks are unfinished, and whether an agent-owned step is being worked on
right now.  Both are answered here from the persisted runs alone, without
rendering instructions, compiling plans, or loading extensions, so a hook
stays fast and its answer is exactly what ``instruction`` would report.

A task whose record cannot be read is reported beside the others, never
raised: one broken task must not hide every other task from a scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ww.contracts import OperatorReason, run_is_open
from ww.errors import StateError
from ww.instructions.policy import operator_reason
from ww.storage_adapters import TaskStorageAdapter
from ww.workspace import resolve_workspace


@dataclass(frozen=True)
class OpenTask:
    """One task whose selected run is neither completed nor abandoned."""

    task_id: str
    run_id: str | None
    workflow: str
    # The agent integration the run was started with, such as ``codex``.
    agent: str
    # The plan item at the cursor, when the run has not run past its end.
    item_id: str | None
    item_name: str | None
    step: str | None
    # ``step`` for the step itself; a hook phase such as
    # ``before_complete_workflow`` for work attached to that step.
    phase: str | None
    owner: str | None
    item_status: str | None
    attempt: int
    run_status: str
    # Why the task waits for the operator, or ``None`` when it does not.
    operator_reason: OperatorReason | None
    # The directory the task works in: its worktree, project, or the root.
    workspace: Path
    updated_at: str
    # The step is one the manager hands to a worker (``--runtime auto``), so
    # the session that started the run waits on it rather than holds it.
    delegated: bool = False
    # The step is interactive and its conversation has not ended: the session
    # stops to let the operator speak, so a stop is not a step left open.
    in_conversation: bool = False

    @property
    def label(self) -> str:
        """How messages name the work: a hook by its own name and its step.

        A hook item records the step it is attached to, so naming only the
        step would point at a step whose own work may be long finished.
        """
        if self.phase in (None, "step") or not self.item_name:
            return self.step or self.item_name or "no active step"
        if not self.step:
            return self.item_name
        return f"{self.item_name} (a hook of {self.step})"

    @property
    def agent_step_in_progress(self) -> bool:
        """An agent-owned step was dispatched and is neither done nor waiting.

        Waiting for input, or for the operator, is not work in progress: the
        agent is right to stop and hand the conversation back.
        """
        return (
            self.run_status == "in_progress"
            and self.owner == "agent"
            and self.item_status == "in_progress"
            and self.operator_reason is None
        )


@dataclass(frozen=True)
class UnreadableTask:
    """A task whose persisted record cannot be read by this build of ww.

    Commands addressing the task keep failing with ``reason``; scans across
    tasks skip it and name it, so every other task stays usable.
    """

    task_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"task_id": self.task_id, "reason": self.reason}


@dataclass(frozen=True)
class OpenWork:
    """The unfinished tasks of a root, and the tasks that could not be read."""

    tasks: tuple[OpenTask, ...]
    unreadable: tuple[UnreadableTask, ...] = ()
    # Tasks left unread because they were last written before the scan's
    # ``since``, finished or not.
    skipped: int = 0


def open_work(
    tasks: TaskStorageAdapter, root: Path, since: datetime | None = None
) -> OpenWork:
    """Every unfinished task in ``root``, children included, newest first.

    With ``since``, a task last written before it is skipped unread, which
    keeps a scan of a long history cheap.
    """
    found: list[OpenTask] = []
    unreadable: list[UnreadableTask] = []
    skipped = 0
    for task_id in tasks.task_ids():
        for candidate in (task_id, *tasks.child_task_ids(task_id)):
            if since is not None:
                written = tasks.task_written_at(candidate)
                if written is not None and written < since:
                    skipped += 1
                    continue
            try:
                task = _open_task(tasks, root, candidate)
            except StateError as error:
                unreadable.append(UnreadableTask(candidate, str(error)))
                continue
            if task is not None:
                found.append(task)
    return OpenWork(
        tuple(sorted(found, key=lambda task: task.updated_at, reverse=True)),
        tuple(unreadable),
        skipped,
    )


def _open_task(tasks: TaskStorageAdapter, root: Path, task_id: str) -> OpenTask | None:
    runs, _, _ = tasks.read_task_record(task_id)
    run = next((run for run in reversed(runs) if run_is_open(run.state.status)), None)
    if run is None:
        return None
    state, plan = run.state, run.snapshot.plan
    item = plan.items[state.cursor] if state.cursor < len(plan.items) else None
    record = (
        state.item_executions[state.cursor]
        if state.cursor < len(state.item_executions)
        else None
    )
    active = state.active_item_id is not None and item is not None
    return OpenTask(
        task_id=task_id,
        run_id=state.run_id,
        workflow=state.workflow,
        agent=state.agent,
        item_id=item.id if item is not None else None,
        item_name=item.name if item is not None else None,
        step=item.step if item is not None else None,
        phase=item.phase if item is not None else None,
        owner=item.owner if item is not None else None,
        item_status=(record.status if record is not None and active else None),
        attempt=record.attempts if record is not None else 0,
        run_status=state.status,
        operator_reason=operator_reason(state, plan),
        workspace=resolve_workspace(root, state.working_directory) or root.resolve(),
        updated_at=state.updated_at,
        delegated=(
            state.workflow_runtime == "auto"
            and item is not None
            and item.role == "worker"
        ),
        in_conversation=(
            item is not None
            and item.interactive
            and record is not None
            and not record.interaction_ended
        ),
    )


def tasks_for_session(
    open_tasks: tuple[OpenTask, ...],
    root: Path,
    directory: Path | None,
    agent: str,
) -> tuple[OpenTask, ...]:
    """The tasks a session of ``agent`` working in ``directory`` concerns.

    A session inside a task's own workspace, a worktree or a project
    checkout, works on that task whichever agent started it; the most
    specific workspace wins, since worktrees usually sit inside the root.
    The root itself is shared by every session and every task without a
    worktree, so it selects nothing: a session there, anywhere else, or in no
    known directory concerns only the tasks its own agent started. Another
    agent's open step is that agent's to close.
    """
    shared = root.resolve()
    if directory is not None:
        resolved = directory.resolve()
        inside = [
            task
            for task in open_tasks
            if task.workspace != shared and resolved.is_relative_to(task.workspace)
        ]
        if inside:
            deepest = max(len(task.workspace.parts) for task in inside)
            return tuple(
                task for task in inside if len(task.workspace.parts) == deepest
            )
    return tuple(task for task in open_tasks if task.agent == agent)
