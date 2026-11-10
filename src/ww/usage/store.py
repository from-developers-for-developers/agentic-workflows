# SPDX-License-Identifier: GPL-3.0-or-later
"""Where token usage is kept.

Everything lives under ``.ww/usage/``, apart from the task state: resetting
a task leaves its usage, and switching usage off or wiping it touches no
task.

- ``tasks/<task>.jsonl``: one record per stretch of a log file's tokens that
  belong to the task, appended by each collection.
- ``outside.jsonl``: the same records for tokens that belong to no task,
  such as a conversation between tasks.
- ``runs.jsonl``: one line per task run, written when ww first sees the run:
  its workflow, when it started, the ww version, and how many feature maps
  the project had then.
- ``files.json``: what ww knows about each log file, keyed by the
  file's IDs alone, never its path: how far it has read it, the ww commands
  and subagent starts it found there, and tokens still waiting for the
  session's next ww command to say where they belong.

Records hold counts, models, IDs and times; never a log's text.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ww.agents import TokenCounts
from ww.migrations.data import UnsupportedSchemaError, header, upgrade
from ww.storage import Storage

# Where a record's tokens came from.
TRANSCRIPT = "transcript"
HOOK = "hook"


@dataclass(frozen=True)
class Stretch:
    """Tokens of one log file that share a task, step, role and model."""

    task: str | None
    run: str | None
    step: str | None
    role: str | None
    agent: str
    session: str
    file: str
    model: str | None
    source: str
    first: str
    last: str
    counts: TokenCounts
    # The version of the ww command that placed the tokens; ``None`` in
    # records written before ww recorded it.
    ww_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **({"ww_version": self.ww_version} if self.ww_version else {}),
            "task": self.task,
            "run": self.run,
            "step": self.step,
            "role": self.role,
            "agent": self.agent,
            "session": self.session,
            "file": self.file,
            "model": self.model,
            "source": self.source,
            "first": self.first,
            "last": self.last,
            **self.counts.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Stretch:
        def text(name: str) -> str | None:
            value = data.get(name)
            return value if isinstance(value, str) else None

        return cls(
            task=text("task"),
            run=text("run"),
            step=text("step"),
            role=text("role"),
            agent=text("agent") or "",
            session=text("session") or "",
            file=text("file") or "",
            model=text("model"),
            source=text("source") or TRANSCRIPT,
            first=text("first") or "",
            last=text("last") or "",
            counts=TokenCounts.from_dict(data),
            ww_version=text("ww_version"),
        )


@dataclass(frozen=True)
class RunFacts:
    """What was true when a task run started, for comparing runs' tokens."""

    task: str
    run: str
    workflow: str
    started: str
    ww_version: str
    # Feature maps in the project, and the tracked files they cover;
    # ``None`` where ww could not count them.
    feature_maps: int | None = None
    mapped_files: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "run": self.run,
            "workflow": self.workflow,
            "started": self.started,
            "ww_version": self.ww_version,
            "feature_maps": self.feature_maps,
            "mapped_files": self.mapped_files,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunFacts | None:
        task, run, workflow, started = (
            data.get(name) for name in ("task", "run", "workflow", "started")
        )
        if not (
            isinstance(task, str)
            and isinstance(run, str)
            and isinstance(workflow, str)
            and isinstance(started, str)
        ):
            return None

        def number(name: str) -> int | None:
            value = data.get(name)
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool)
                else None
            )

        version = data.get("ww_version")
        return cls(
            task=task,
            run=run,
            workflow=workflow,
            started=started,
            ww_version=version if isinstance(version, str) else "",
            feature_maps=number("feature_maps"),
            mapped_files=number("mapped_files"),
        )


@dataclass
class FileState:
    """What ww has read of one log file, and what is still unattributed."""

    agent: str
    session: str
    path: str | None = None
    offset: int = 0
    carry: dict[str, Any] = field(default_factory=dict)
    # Each ww command found, in order: [time, task, role, verb].
    calls: list[list[str]] = field(default_factory=list)
    # Subagent starts found: tool call ID to time.
    spawns: dict[str, str] = field(default_factory=dict)
    # Tokens waiting for the next ww command, by model ("" for unknown):
    # the counts plus the first and last time.
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The working directory the log names.
    directory: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "session": self.session,
            "path": self.path,
            "offset": self.offset,
            "carry": self.carry,
            "calls": self.calls,
            "spawns": self.spawns,
            "pending": self.pending,
            "directory": self.directory,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileState:
        return cls(
            agent=str(data.get("agent", "")),
            session=str(data.get("session", "")),
            path=data.get("path") if isinstance(data.get("path"), str) else None,
            offset=data["offset"] if isinstance(data.get("offset"), int) else 0,
            carry=dict(data.get("carry") or {}),
            calls=[list(call) for call in data.get("calls") or []],
            spawns=dict(data.get("spawns") or {}),
            pending=dict(data.get("pending") or {}),
            directory=data.get("directory")
            if isinstance(data.get("directory"), str)
            else None,
        )


@dataclass
class SessionState:
    """A session ww was told about, and whether its logs were found here."""

    agent: str
    host: str | None = None
    readable: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "host": self.host, "readable": self.readable}


@dataclass
class UsageState:
    files: dict[str, FileState] = field(default_factory=dict)
    sessions: dict[str, SessionState] = field(default_factory=dict)


class UsageStore:
    """The checkout's usage records and read positions."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.directory = storage.runtime_path / "usage"
        self.state_path = self.directory / "files.json"
        self.outside_path = self.directory / "outside.jsonl"
        self.runs_path = self.directory / "runs.jsonl"

    def task_path(self, task_id: str) -> Path:
        # A child's ID, ``parent/child``, nests like its task directory.
        return self.directory / "tasks" / f"{task_id}.jsonl"

    def lock(self) -> Any:
        return self.storage.locks.lock(self.state_path, purpose="token usage")

    def load(self) -> UsageState:
        """The read positions; a file that cannot be read starts over empty.

        A later ww's file raises, so its positions are never overwritten.
        """
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("token usage state must be a mapping")
            data = upgrade("usage state", data)
        except UnsupportedSchemaError as error:
            if error.newer:
                raise
            return UsageState()
        except (OSError, ValueError):
            return UsageState()
        files = data.get("files")
        sessions = data.get("sessions")
        return UsageState(
            files={
                key: FileState.from_dict(value)
                for key, value in (files.items() if isinstance(files, dict) else ())
                if isinstance(value, dict)
            },
            sessions={
                key: SessionState(
                    str(value.get("agent", "")),
                    value.get("host") if isinstance(value.get("host"), str) else None,
                    value.get("readable")
                    if isinstance(value.get("readable"), bool)
                    else None,
                )
                for key, value in (
                    sessions.items() if isinstance(sessions, dict) else ()
                )
                if isinstance(value, dict)
            },
        )

    def save(self, state: UsageState) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.storage.locks.atomic_write(
            self.state_path,
            json.dumps(
                {
                    **header("usage state"),
                    "files": {key: file.to_dict() for key, file in state.files.items()},
                    "sessions": {
                        key: session.to_dict()
                        for key, session in state.sessions.items()
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    def append(self, stretches: Iterable[Stretch]) -> None:
        """Append each stretch to its task's file, or to the outside file."""
        lines: dict[Path, list[str]] = {}
        for stretch in stretches:
            path = self.task_path(stretch.task) if stretch.task else self.outside_path
            lines.setdefault(path, []).append(
                json.dumps(stretch.to_dict(), sort_keys=True) + "\n"
            )
        for path, written in lines.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.writelines(written)

    def records(self, path: Path) -> Iterator[Stretch]:
        """The stretches in one usage file; unreadable lines are skipped."""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                yield Stretch.from_dict(data)

    def run_facts(self) -> dict[tuple[str, str], RunFacts]:
        """Each recorded run's facts, by task and run; the first line wins."""
        found: dict[tuple[str, str], RunFacts] = {}
        try:
            lines = self.runs_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return found
        for line in lines:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            facts = RunFacts.from_dict(data) if isinstance(data, dict) else None
            if facts is not None:
                found.setdefault((facts.task, facts.run), facts)
        return found

    def note_run(self, facts: RunFacts) -> bool:
        """Record ``facts`` unless the run already has a line; whether it did."""
        with self.lock():
            if (facts.task, facts.run) in self.run_facts():
                return False
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.runs_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(facts.to_dict(), sort_keys=True) + "\n")
        return True

    def task_records(self, task_id: str) -> list[Stretch]:
        return list(self.records(self.task_path(task_id)))

    def outside_records(self) -> list[Stretch]:
        return list(self.records(self.outside_path))

    def usage_files(self) -> list[Path]:
        """Every usage file of the checkout: each task's and the outside one."""
        tasks = self.directory / "tasks"
        found = sorted(tasks.rglob("*.jsonl")) if tasks.is_dir() else []
        return [*found, self.outside_path]

    def forget_sessions(self, sessions: set[str]) -> None:
        """Drop every stretch read from ``sessions``' logs, for a fresh read.

        A session is ``<agent>:<session>``, as the stretches name it.
        """
        if not sessions:
            return
        for path in self.usage_files():
            if not path.exists():
                continue
            kept = [
                json.dumps(stretch.to_dict(), sort_keys=True) + "\n"
                for stretch in self.records(path)
                if f"{stretch.agent}:{stretch.session}" not in sessions
            ]
            self.storage.locks.atomic_write(path, "".join(kept))
