# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only view of the work still open in a project root.

Agent hooks ask two questions of ww's state many times a session: which
tasks are unfinished, and whether an agent-owned step is being worked on
right now.  Both are answered here from the persisted runs alone, without
rendering instructions, compiling plans, or loading extensions, so a hook
stays fast and its answer is exactly what ``instruction`` would report.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ww.contracts import OperatorReason, run_is_open
from ww.instructions.policy import operator_reason
from ww.storage_adapters import TaskStorageAdapter
from ww.workspace import resolve_workspace


@dataclass(frozen=True)
class OpenTask:
    """One task whose selected run is neither completed nor abandoned."""

    task_id: str
    run_id: str | None
    workflow: str
    # The plan item at the cursor, when the run has not run past its end.
    item_id: str | None
    item_name: str | None
    step: str | None
    owner: str | None
    item_status: str | None
    attempt: int
    run_status: str
    # Why the task waits for the operator, or ``None`` when it does not.
    operator_reason: OperatorReason | None
    # The directory the task works in: its worktree, project, or the root.
    workspace: Path
    updated_at: str

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


def open_work(tasks: TaskStorageAdapter, root: Path) -> tuple[OpenTask, ...]:
    """Every unfinished task in ``root``, children included, newest first."""
    found: list[OpenTask] = []
    for task_id in tasks.task_ids():
        for candidate in (task_id, *tasks.child_task_ids(task_id)):
            task = _open_task(tasks, root, candidate)
            if task is not None:
                found.append(task)
    return tuple(sorted(found, key=lambda task: task.updated_at, reverse=True))


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
        item_id=item.id if item is not None else None,
        item_name=item.name if item is not None else None,
        step=item.step if item is not None else None,
        owner=item.owner if item is not None else None,
        item_status=(record.status if record is not None and active else None),
        attempt=record.attempts if record is not None else 0,
        run_status=state.status,
        operator_reason=operator_reason(state, plan),
        workspace=resolve_workspace(root, state.working_directory) or root.resolve(),
        updated_at=state.updated_at,
    )


def tasks_for_directory(
    open_tasks: tuple[OpenTask, ...], directory: Path | None
) -> tuple[OpenTask, ...]:
    """The tasks a session in ``directory`` is working on.

    A session inside a task's workspace works on that task; the most specific
    workspace wins, since task worktrees usually sit inside the root. A
    session that matches no task, or that runs in no known directory, may be
    working on any of them.
    """
    if directory is None:
        return open_tasks
    resolved = directory.resolve()
    matching = [task for task in open_tasks if resolved.is_relative_to(task.workspace)]
    if not matching:
        return open_tasks
    deepest = max(len(task.workspace.parts) for task in matching)
    return tuple(task for task in matching if len(task.workspace.parts) == deepest)
