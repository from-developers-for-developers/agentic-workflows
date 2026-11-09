# SPDX-License-Identifier: GPL-3.0-or-later
"""Every task in the store, one row each, as ``ww tasks`` lists them.

A row names the task, its codename when it has one, its one-line
description, the status of its latest run, when it last changed and the
tokens its agents spent, as far as ww has collected them. Children
follow their parent. A task this build cannot read in full, such as one an
older ww wrote, is listed from its envelope (``ww.task_envelope``), with why
it could not be read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from ww.direct_work import DirectWork
from ww.errors import StateError
from ww.execution_models import TaskRunAggregate
from ww.instructions.policy import operator_reason
from ww.storage import Storage
from ww.task_descriptions import task_description
from ww.task_envelope import read_task_envelope
from ww.usage import UsageStore, format_tokens, task_total

# The status of a task registered only through direct work.
DIRECT_STATUS = "direct"
# The status of an unreadable task whose envelope names no run either.
UNREADABLE_STATUS = "unreadable"


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    description: str | None
    status: str
    workflows: tuple[str, ...]
    updated_at: str | None
    # Why only the task's envelope could be read; ``None`` when it was read.
    unreadable: str | None = None
    codename: str | None = None
    # Every token recorded for the task (``ww usage``).
    tokens: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "codename": self.codename,
            "description": self.description or "",
            "status": self.status,
            "workflows": list(self.workflows),
            "updated_at": self.updated_at,
            "unreadable": self.unreadable,
            "tokens": self.tokens,
        }


def task_rows(storage: Storage) -> tuple[TaskRow, ...]:
    """Every task, in natural task ID order, children after their parent."""
    return tuple(task_row(storage, task_id) for task_id in task_ids_in_order(storage))


def task_ids_in_order(storage: Storage) -> tuple[str, ...]:
    """Every task ID in natural order, children after their parent."""
    tasks = storage.task_persistence
    return tuple(
        candidate
        for task_id in sorted(tasks.task_ids(), key=_natural)
        for candidate in (task_id, *sorted(tasks.child_task_ids(task_id), key=_natural))
    )


def task_row(storage: Storage, task_id: str) -> TaskRow:
    return replace(
        _row(storage, task_id),
        codename=_codename(storage, task_id),
        tokens=task_total(UsageStore(storage), task_id),
    )


def _row(storage: Storage, task_id: str) -> TaskRow:
    tasks = storage.task_persistence
    direct = _direct_work(storage, task_id)
    try:
        runs, _, _ = tasks.read_task_record(task_id)
    except StateError as error:
        return _envelope_row(storage, task_id, str(error), direct)
    times = [run.state.updated_at for run in runs] + [
        entry.recorded_at for entry in direct
    ]
    return TaskRow(
        task_id,
        task_description(run.state.description for run in runs),
        run_status(runs[-1]) if runs else DIRECT_STATUS,
        tuple(run.workflow for run in runs),
        max(times, default=None),
    )


def run_status(run: TaskRunAggregate) -> str:
    """The run's status, with a stop for the operator called that."""
    if operator_reason(run.state, run.snapshot.plan) is not None:
        return "awaiting_operator"
    return run.state.status


def _envelope_row(
    storage: Storage, task_id: str, reason: str, direct: tuple[DirectWork, ...]
) -> TaskRow:
    envelope = read_task_envelope(storage.runtime_path / "tasks" / task_id, task_id)
    times = [
        moment
        for run in envelope.runs
        if (moment := run.updated_at or run.created_at) is not None
    ] + [entry.recorded_at for entry in direct]
    if envelope.runs:
        status = envelope.runs[-1].status or UNREADABLE_STATUS
    else:
        status = DIRECT_STATUS if direct else UNREADABLE_STATUS
    return TaskRow(
        task_id,
        task_description(run.description for run in envelope.runs),
        status,
        tuple(run.workflow or "" for run in envelope.runs),
        max(times, default=None),
        reason,
    )


def _codename(storage: Storage, task_id: str) -> str | None:
    try:
        return storage.task_persistence.read_codename(task_id)
    except StateError:
        return None


def _direct_work(storage: Storage, task_id: str) -> tuple[DirectWork, ...]:
    try:
        return storage.task_persistence.read_direct_work(task_id)
    except StateError:
        return ()


def _natural(task_id: str) -> tuple[tuple[int, int | str], ...]:
    """Order ``TASK-2`` before ``TASK-10``: numbers by value, the rest by text."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", task_id)
        if part
    )


def render_task_rows(rows: tuple[TaskRow, ...]) -> str:
    """The rows as aligned plain text: ID, codename, status, last change,
    tokens, description.

    The codename column is left out while no task has one, and the tokens
    column while no task has any recorded.
    """
    if not rows:
        return "No tasks."
    named = any(row.codename for row in rows)
    counted = any(row.tokens for row in rows)
    cells = [
        (
            row.task_id,
            *((row.codename or "",) if named else ()),
            row.status,
            _minute(row.updated_at),
            *((format_tokens(row.tokens) if row.tokens else "-",) if counted else ()),
            row.description or "",
        )
        for row in rows
    ]
    aligned = len(cells[0]) - 1
    widths = [max(len(cell[column]) for cell in cells) for column in range(aligned)]
    lines = [
        "  ".join(cell[column].ljust(widths[column]) for column in range(aligned))
        + ("  " + cell[-1] if cell[-1] else "")
        for cell in cells
    ]
    unreadable = [row.task_id for row in rows if row.unreadable]
    if unreadable:
        lines += [
            "",
            "Read only in part, by an older or newer ww: "
            + ", ".join(unreadable)
            + "; `ww tasks --json` says why.",
        ]
    return "\n".join(line.rstrip() for line in lines)


def _minute(moment: str | None) -> str:
    """``2026-11-05T15:06:38Z`` as ``2026-11-05 15:06``; unknown as ``-``."""
    if not moment:
        return "-"
    return moment.replace("T", " ")[:16]
