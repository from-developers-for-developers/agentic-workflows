# SPDX-License-Identifier: GPL-3.0-or-later
"""The tasks that changed a feature: ``.ww/features/<name>.tasks``.

One line per task, its ID and its one-line description, oldest first:
``TASK-65  Collect token usage per task, step, role and model``. ww writes the
file, never an agent. Parallel branches each append a line, and Git keeps both
when merging (``init`` marks the files ``merge=union``), so a reader keeps the
first line of each task and ignores any repeat.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .maps import FEATURES_DIRECTORY

SUFFIX = ".tasks"
_SEPARATOR = "  "


@dataclass(frozen=True)
class TaskEntry:
    task_id: str
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"id": self.task_id, "description": self.description}

    def line(self) -> str:
        return f"{self.task_id}{_SEPARATOR}{self.description}".rstrip()


def task_list_path(root: Path, name: str) -> Path:
    return root / FEATURES_DIRECTORY / f"{name}{SUFFIX}"


def read_task_list(root: Path, name: str) -> tuple[TaskEntry, ...]:
    """The tasks recorded for map ``name``, oldest first, each once."""
    path = task_list_path(root, name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    entries: dict[str, TaskEntry] = {}
    for line in text.splitlines():
        task_id, _, description = line.strip().partition(" ")
        if task_id and task_id not in entries:
            entries[task_id] = TaskEntry(task_id, description.strip())
    return tuple(entries.values())


def record_task(root: Path, name: str, task_id: str, description: str) -> bool:
    """Add ``task_id`` to map ``name``'s list unless it is there already.

    The description is kept to one line. Returns whether the file changed.
    """
    if any(entry.task_id == task_id for entry in read_task_list(root, name)):
        return False
    path = task_list_path(root, name)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    entry = TaskEntry(task_id, " ".join(description.split()))
    path.write_text(existing + separator + entry.line() + "\n", encoding="utf-8")
    return True
