# SPDX-License-Identifier: GPL-3.0-or-later
"""Tasks whose persisted record this build of ww cannot read.

``discover`` is an operator surface: besides the project's workflows it
names the tasks ww cannot read, so the operator sees state that would
otherwise stay hidden. A task whose record cannot be read is reported beside
the others, never raised: one broken task must not hide the rest.
"""

from __future__ import annotations

from dataclasses import dataclass

from ww.errors import StateError
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
