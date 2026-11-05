# SPDX-License-Identifier: GPL-3.0-or-later
"""Tasks whose persisted record this build of ww, or another, cannot read.

``discover`` is an operator surface: besides the project's workflows it
names the tasks ww cannot read, so the operator sees state that would
otherwise stay hidden. A task whose record cannot be read is reported beside
the others, never raised: one broken task must not hide the rest.

:func:`runs_refused_by` looks ahead instead: the runs of tasks in progress
that a given ww version would refuse because their saved plan holds a
construct it removed, which ``ww upgrade`` lists before installing it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ww.contracts import run_is_open
from ww.errors import StateError
from ww.migrations.removals import (
    RemovedConstructError,
    RemovedFinding,
    removed_in_plan,
)
from ww.storage_adapters import TaskStorageAdapter


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


def unreadable_tasks(tasks: TaskStorageAdapter) -> tuple[UnreadableTask, ...]:
    """Every task in the root, children included, whose record cannot be read."""
    found: list[UnreadableTask] = []
    for task_id in tasks.task_ids():
        for candidate in (task_id, *tasks.child_task_ids(task_id)):
            try:
                tasks.read_task_record(candidate)
            except StateError as error:
                found.append(UnreadableTask(candidate, str(error)))
    return tuple(found)


@dataclass(frozen=True)
class RefusedRun:
    """A run of a task in progress that a ww version refuses, and why.

    ``open`` tells the run in progress from a finished one beside it, which
    is refused too, since a task is read whole.
    """

    task_id: str
    run_id: str
    open: bool
    found: tuple[RemovedFinding, ...]

    def render(self) -> str:
        state = "open" if self.open else "finished"
        return f"{self.task_id} ({state} run {self.run_id}): " + "; ".join(
            finding.render() for finding in self.found
        )


def runs_refused_by(tasks: TaskStorageAdapter, version: str) -> tuple[RefusedRun, ...]:
    """The runs of tasks in progress whose saved plan ``version`` refuses.

    It is judged by this build's list of removed constructs, which names the
    removals of the next major version ahead of it. An open run this build
    already refuses is listed as well; finished tasks are not, since nothing
    runs them again.
    """
    refused: list[RefusedRun] = []
    for task_id in tasks.task_ids():
        for candidate in (task_id, *tasks.child_task_ids(task_id)):
            try:
                runs, _, _ = tasks.read_task_record(candidate)
            except RemovedConstructError as error:
                if error.open:
                    refused.append(
                        RefusedRun(candidate, error.run_id, True, error.found)
                    )
                continue
            except StateError:
                continue
            if not any(run_is_open(run.state.status) for run in runs):
                continue
            for run in runs:
                found = removed_in_plan(run.snapshot.to_dict(), version)
                if found:
                    refused.append(
                        RefusedRun(
                            candidate,
                            run.run_id,
                            run_is_open(run.state.status),
                            found,
                        )
                    )
    return tuple(refused)
