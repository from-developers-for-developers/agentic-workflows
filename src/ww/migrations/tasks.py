# SPDX-License-Identifier: GPL-3.0-or-later
"""Stored task state rewritten in the current format: ``ww migrate --tasks``.

ww never needs this: a task an older ww wrote is migrated as it is read
(:mod:`ww.migrations.data`), an open one is written in the current format on
its next commit, and a closed one is migrated on every read. Rewriting them
eagerly puts every task on disk in the current format at once: before an
older format stops being readable, or to stop migrating closed tasks on
every read.

A task is rewritten by reading it through the storage adapter, which
migrates it, and committing it again, which writes the current format,
under the task's lock like every other writer. Only the run documents whose
encoding changes are written anew; the index always is. A task already in
the current format is left alone, and one that does not read is reported and
left alone too.

Before a task is rewritten, its state files (the index and the run documents
it names) are copied to ``.ww/backups/tasks-<UTC time>/<task ID>/``, at the
same paths below the task's directory. Copying them back over
``.ww/tasks/`` restores the tasks; the run documents the rewrite added are
then named by no index and the task's next commit removes them.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ww.errors import WwError
from ww.storage_adapters.filesystem import FileTaskStorageAdapter

BACKUP_DIRECTORY = Path(".ww") / "backups"


@dataclass(frozen=True)
class TaskRewrite:
    """One stored task: the state files in an older format, or why it does not read.

    ``files`` are relative to the project root.
    """

    task_id: str
    files: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class TaskRewrites:
    """The tasks a rewrite concerns; ``backup`` is set once one was written."""

    tasks: tuple[TaskRewrite, ...]
    applied: bool = False
    backup: Path | None = None

    @property
    def outdated(self) -> tuple[TaskRewrite, ...]:
        return tuple(task for task in self.tasks if task.error is None)

    @property
    def unreadable(self) -> tuple[TaskRewrite, ...]:
        return tuple(task for task in self.tasks if task.error is not None)


def stored_tasks(adapter: FileTaskStorageAdapter) -> tuple[str, ...]:
    """Every stored task and child task, each parent before its children."""
    return tuple(
        task
        for task_id in adapter.task_ids()
        for task in (task_id, *adapter.child_task_ids(task_id))
    )


def outdated_tasks(root: Path) -> TaskRewrites:
    """The tasks whose state an older format holds, without changing any."""
    adapter = FileTaskStorageAdapter(root)
    found: list[TaskRewrite] = []
    for task_id in stored_tasks(adapter):
        rewrite = _inspect(adapter, task_id)
        if rewrite.files or rewrite.error is not None:
            found.append(rewrite)
    return TaskRewrites(tuple(found))


def rewrite_tasks(root: Path) -> TaskRewrites:
    """Rewrite every task an older format holds, after backing its state up."""
    adapter = FileTaskStorageAdapter(root)
    backup = _backup_directory(root)
    found: list[TaskRewrite] = []
    written = False
    for task_id in stored_tasks(adapter):
        with adapter.lock_task(task_id):
            rewrite = _inspect(adapter, task_id)
            if rewrite.files:
                try:
                    _back_up(adapter, task_id, backup)
                    written = True
                    runs, handoff, revision = adapter.read_task_record(task_id)
                    adapter.commit_task_aggregate(task_id, runs, handoff, revision)
                except (WwError, ValueError, OSError) as error:
                    rewrite = TaskRewrite(task_id, rewrite.files, str(error))
        if rewrite.files or rewrite.error is not None:
            found.append(rewrite)
    return TaskRewrites(tuple(found), applied=True, backup=backup if written else None)


def _inspect(adapter: FileTaskStorageAdapter, task_id: str) -> TaskRewrite:
    try:
        outdated = adapter.outdated_task_files(task_id)
    except (WwError, ValueError, OSError) as error:
        return TaskRewrite(task_id, error=str(error))
    return TaskRewrite(
        task_id, tuple(str(path.relative_to(adapter.root)) for path in outdated)
    )


def _back_up(adapter: FileTaskStorageAdapter, task_id: str, backup: Path) -> None:
    """Copy the task's state files below ``backup``, at their task paths."""
    for path in adapter.task_state_files(task_id):
        target = backup / path.relative_to(adapter.tasks_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _backup_directory(root: Path) -> Path:
    """A backup directory for this rewrite, named by the time, not yet there."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root / BACKUP_DIRECTORY / f"tasks-{stamp}"
    suffix = 1
    while directory.exists():
        suffix += 1
        directory = root / BACKUP_DIRECTORY / f"tasks-{stamp}-{suffix}"
    return directory
