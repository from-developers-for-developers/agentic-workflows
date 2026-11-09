# SPDX-License-Identifier: GPL-3.0-or-later
"""What ww knows about each headless run: one JSON record per launch.

A launch's record is its single authority.  ww writes it before starting the
supervisor, the supervisor (:mod:`ww.headless.supervisor`) updates it while
the agent runs and when it ends, and ``ww wait`` and the pages only read it.
Records live beside the run's other files, under
``.ww/tasks/<task>/runs/<run>/headless/``, with the agent's output and
error streams next to each record.  The environment the agent ran with is
never written down: it may hold secrets.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal

from ww.agents import TokenCounts

LaunchStatus = Literal[
    # Recorded, supervisor not yet reporting.
    "starting",
    "running",
    # The agent's program ended by itself, with ``exit_code``.
    "exited",
    # The supervisor stopped it: it ran longer than its timeout, or printed
    # nothing for longer than its idle timeout.
    "timed_out",
    "idle",
    # The program could not be started at all.
    "could_not_start",
    # Someone stopped it, such as ``reset``.
    "stopped",
]
FINISHED: frozenset[LaunchStatus] = frozenset(
    {"exited", "timed_out", "idle", "could_not_start", "stopped"}
)
DIRECTORY = "headless"


@dataclass(frozen=True)
class LaunchRecord:
    """One start of an agent's program for one step of one run."""

    id: str
    task_id: str
    run_id: str
    item_id: str
    step: str
    agent: str
    argv: tuple[str, ...]
    workspace: str
    started_at: str
    assignment: str | None = None
    # The parallel branch it works on, when the step is a parallel group.
    branch: str | None = None
    # The launch this one resumes, continuing its agent session.
    resumes: str | None = None
    timeout_minutes: float | None = None
    idle_timeout_minutes: float | None = None
    status: LaunchStatus = "starting"
    supervisor_pid: int | None = None
    agent_pid: int | None = None
    session_id: str | None = None
    exit_code: int | None = None
    ended_at: str | None = None
    # Why it could not start, or how it was stopped.
    error: str | None = None
    usage: TokenCounts = field(default_factory=TokenCounts)
    # The manager gave the step back to a session: ww no longer waits on it.
    released: bool = False

    @property
    def finished(self) -> bool:
        return self.status in FINISHED

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["argv"] = list(self.argv)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LaunchRecord:
        known = {each.name for each in fields(cls)}
        values = {key: value for key, value in data.items() if key in known}
        values["argv"] = tuple(values.get("argv", ()))
        usage = values.get("usage")
        values["usage"] = (
            TokenCounts(**usage) if isinstance(usage, dict) else TokenCounts()
        )
        return cls(**values)


class LaunchStore:
    """The launch records of one task's runs, under ``root/.ww/tasks``."""

    def __init__(self, root: Path) -> None:
        self.tasks = root / ".ww" / "tasks"

    def directory(self, task_id: str, run_id: str) -> Path:
        return self.tasks / task_id / "runs" / run_id / DIRECTORY

    def path(self, record: LaunchRecord) -> Path:
        return self.directory(record.task_id, record.run_id) / f"{record.id}.json"

    def output_path(self, record: LaunchRecord) -> Path:
        return self.directory(record.task_id, record.run_id) / f"{record.id}.out.jsonl"

    def error_path(self, record: LaunchRecord) -> Path:
        return self.directory(record.task_id, record.run_id) / f"{record.id}.err.log"

    def write(self, record: LaunchRecord) -> None:
        write_record(self.path(record), record)

    def runs(self, task_id: str, run_id: str) -> tuple[LaunchRecord, ...]:
        """The run's launches, oldest first."""
        directory = self.directory(task_id, run_id)
        records = []
        for path in sorted(directory.glob("*.json")):
            record = read_record(path)
            if record is not None:
                records.append(record)
        return tuple(sorted(records, key=lambda each: (each.started_at, each.id)))

    def latest(
        self, task_id: str, run_id: str, item_id: str, branch: str | None = None
    ) -> LaunchRecord | None:
        """The newest launch for ``item_id`` (and ``branch``) in the run."""
        found = [
            each
            for each in self.runs(task_id, run_id)
            if each.item_id == item_id and each.branch == branch
        ]
        return found[-1] if found else None

    def running(self, agent: str) -> int:
        """How many of ``agent``'s launches are going, in every task."""
        return sum(
            1
            for path in self.tasks.glob(f"**/{DIRECTORY}/*.json")
            if (record := read_record(path)) is not None
            and record.agent == agent
            and not record.finished
        )

    def release(self, record: LaunchRecord) -> LaunchRecord:
        released = replace(record, released=True)
        self.write(released)
        return released


def read_record(path: Path) -> LaunchRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return LaunchRecord.from_dict(data)
    except TypeError:
        return None


def write_record(path: Path, record: LaunchRecord) -> None:
    """Replace the record at ``path`` in one step, so a reader never sees half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record.to_dict(), handle, indent=2)
            handle.write("\n")
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
