# SPDX-License-Identifier: GPL-3.0-or-later
"""What a task was doing at a given time: its run, step and runtime.

Tokens are placed on the step that was under way when the model answered,
read from the run's own step times; a task whose run has finished stops
taking tokens shortly after, so a session that moves on to talk about
something else does not keep charging it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ww.errors import WwError
from ww.execution_models.records import StepProgress
from ww.storage_adapters.base import TaskStorageAdapter
from ww.task_envelope import read_task_envelope

# How long after its run finished a task still takes a session's tokens:
# the agent's closing reply to the operator belongs to the task.
FINISH_GRACE = timedelta(minutes=5)
_FINISHED = frozenset({"completed", "failed", "abandoned"})


@dataclass(frozen=True)
class _Step:
    path: str
    started_at: str


@dataclass(frozen=True)
class _Run:
    run_id: str
    created_at: str
    finished_at: str | None
    runtime: str
    steps: tuple[_Step, ...]


class TaskTimes:
    """Each known task's runs and steps, read once per collection.

    A task this ww cannot read in full, such as one an older ww wrote, still
    takes its tokens: its runs come from its envelope (``ww.task_envelope``),
    which knows when each ran and ended but not its steps.
    """

    def __init__(
        self, tasks: TaskStorageAdapter, directory: Path | None = None
    ) -> None:
        self.tasks = tasks
        # The task directories, where an unreadable task's envelope is read.
        self.directory = directory
        self._runs: dict[str, tuple[_Run, ...] | None] = {}

    def exists(self, task_id: str) -> bool:
        return self._read(task_id) is not None

    def _read(self, task_id: str) -> tuple[_Run, ...] | None:
        if task_id not in self._runs:
            try:
                runs, _, _ = self.tasks.read_task_record(task_id)
            except (WwError, OSError, ValueError):
                self._runs[task_id] = self._envelope(task_id)
            else:
                # A task with no stored state reads as one without runs.
                if not runs and not self._stored(task_id):
                    self._runs[task_id] = None
                    return None
                self._runs[task_id] = tuple(
                    _Run(
                        run.run_id,
                        instant(run.state.created_at),
                        instant(run.state.updated_at)
                        if run.state.status in _FINISHED
                        else None,
                        run.state.workflow_runtime,
                        tuple(
                            sorted(
                                _leaves(run.state.steps),
                                key=lambda step: step.started_at,
                            )
                        ),
                    )
                    for run in runs
                )
        return self._runs[task_id]

    def _stored(self, task_id: str) -> bool:
        return self.directory is None or (self.directory / task_id).is_dir()

    def _envelope(self, task_id: str) -> tuple[_Run, ...] | None:
        """An unreadable task's runs without steps; ``None`` if it is not stored."""
        if self.directory is None or not self._stored(task_id):
            return None
        envelope = read_task_envelope(self.directory / task_id, task_id)
        return tuple(
            _Run(
                run.run_id,
                instant(run.created_at or ""),
                instant(run.updated_at)
                if run.updated_at and run.status in _FINISHED
                else None,
                "",
                (),
            )
            for run in envelope.runs
        )

    def _run_at(self, task_id: str, at: str) -> _Run | None:
        runs = self._read(task_id) or ()
        started = [run for run in runs if run.created_at <= at]
        return started[-1] if started else (runs[0] if runs else None)

    def active(self, task_id: str, at: str) -> bool:
        """Whether the task still takes a session's tokens at ``at``."""
        if self._read(task_id) is None:
            return False
        run = self._run_at(task_id, at)
        if run is None or run.finished_at is None:
            return True
        return _parse(at) <= _parse(run.finished_at) + FINISH_GRACE

    def place(self, task_id: str, at: str) -> tuple[str | None, str | None]:
        """The run and step under way at ``at``; no step before the first."""
        run = self._run_at(task_id, at)
        if run is None:
            return None, None
        begun = [step for step in run.steps if step.started_at <= at]
        return run.run_id, begun[-1].path if begun else None

    def runtime(self, task_id: str, at: str) -> str | None:
        run = self._run_at(task_id, at)
        return run.runtime if run else None


def _leaves(steps: tuple[StepProgress, ...]) -> list[_Step]:
    """The steps that hold work, a group's children in place of the group."""
    found = []
    for step in steps:
        if step.children:
            found.extend(_leaves(step.children))
        elif step.started_at:
            found.append(_Step(step.path, instant(step.started_at)))
    return found


def instant(moment: str) -> str:
    """A time in one fixed UTC form, so times from any source compare as text.

    ww writes whole seconds and agents write milliseconds; an unreadable
    time is kept as it is.
    """
    try:
        parsed = _parse(moment)
    except ValueError:
        return moment
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))
