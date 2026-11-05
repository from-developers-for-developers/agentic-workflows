# SPDX-License-Identifier: GPL-3.0-or-later
"""The envelope of a stored task: what every ww can read of any task's files.

A task's state documents change shape between schema versions; their
envelope does not. These names are a promise, never renamed in any schema:

- the task index ``state.json``: ``format``, ``schema_version``,
  ``written_by`` (the ww version that wrote it), ``task_id`` and ``runs``;
- each run, either inline in ``runs`` or in the run document an entry names
  (``runs/<id>/state.<revision>.json``, under ``run``): ``run_id``,
  ``workflow``, and in its ``state`` ``status``, ``created_at``,
  ``updated_at`` and the optional ``description``; a run document records
  its own ``written_by``;
- each step's artifact, a Markdown file under ``runs/<run_id>/steps/``.

This module reads only that, with plain JSON and none of the schema-specific
decoders, so a task this build cannot decode still shows what it was. A
missing or odd value reads as ``None``; the reader never raises for what a
file holds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASK_STATE_FILE = "state.json"
RUNS_DIRECTORY = "runs"
STEPS_DIRECTORY = "steps"


@dataclass(frozen=True)
class RunEnvelope:
    run_id: str
    workflow: str | None = None
    status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    written_by: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class TaskEnvelope:
    """A task's envelope; ``problem`` says why its index could not be read."""

    task_id: str
    format: str | None = None
    schema_version: int | None = None
    written_by: str | None = None
    runs: tuple[RunEnvelope, ...] = ()
    problem: str | None = None


def read_task_envelope(task_directory: Path, task_id: str) -> TaskEnvelope:
    """The envelope of the task stored in ``task_directory``."""
    try:
        index = _load(task_directory / TASK_STATE_FILE)
    except (OSError, ValueError, RecursionError) as error:
        return TaskEnvelope(task_id, problem=f"{TASK_STATE_FILE}: {error}")
    if not isinstance(index, dict):
        return TaskEnvelope(task_id, problem=f"{TASK_STATE_FILE} is not a mapping")
    entries = index.get("runs")
    runs = tuple(
        run
        for entry in (entries if isinstance(entries, list) else ())
        if (run := _run(task_directory, entry)) is not None
    )
    return TaskEnvelope(
        _text(index.get("task_id")) or task_id,
        _text(index.get("format")),
        _version(index.get("schema_version")),
        _text(index.get("written_by")),
        runs,
    )


def run_directories(task_directory: Path) -> tuple[str, ...]:
    """The run directories under the task's ``runs``, by name."""
    runs = task_directory / RUNS_DIRECTORY
    try:
        return tuple(
            sorted(
                entry.name
                for entry in runs.iterdir()
                if entry.is_dir() and not entry.is_symlink()
            )
        )
    except OSError:
        return ()


def step_artifacts(task_directory: Path, run_id: str) -> tuple[Path, ...]:
    """The run's step artifacts, relative to its ``steps`` directory, sorted.

    Only Markdown files that resolve inside that directory are listed, so a
    link cannot make a file elsewhere look like an artifact.
    """
    if not _plain_name(run_id):
        return ()
    steps = task_directory / RUNS_DIRECTORY / run_id / STEPS_DIRECTORY
    try:
        if steps.is_symlink() or not steps.is_dir():
            return ()
        inside = steps.resolve(strict=True)
        candidates = sorted(steps.rglob("*.md"))
    except OSError:
        return ()
    found = []
    for path in candidates:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_file() and resolved.is_relative_to(inside):
            found.append(path.relative_to(steps))
    return tuple(sorted(found, key=Path.as_posix))


def _run(task_directory: Path, entry: object) -> RunEnvelope | None:
    """One ``runs`` entry: the run inline, or the run document it names."""
    if not isinstance(entry, dict):
        return None
    run_id = _text(entry.get("run_id")) or _text(entry.get("id"))
    if run_id is None:
        return None
    body: dict[str, Any] = entry
    written_by = None
    revision = entry.get("revision")
    if "state" not in entry and _version(revision) is not None and _plain_name(run_id):
        path = task_directory / RUNS_DIRECTORY / run_id / f"state.{revision}.json"
        try:
            document = _load(path)
        except (OSError, ValueError, RecursionError):
            document = None
        if isinstance(document, dict):
            written_by = _text(document.get("written_by"))
            run = document.get("run")
            body = run if isinstance(run, dict) else {}
        else:
            body = {}
    state = body.get("state")
    state = state if isinstance(state, dict) else {}
    return RunEnvelope(
        run_id,
        _text(body.get("workflow")) or _text(state.get("workflow")),
        _text(state.get("status")),
        _text(state.get("created_at")),
        _text(state.get("updated_at")),
        written_by,
        _text(state.get("description")),
    )


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _version(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _plain_name(name: str) -> bool:
    """Whether ``name`` is one directory entry, not a path out of its parent."""
    return name not in {"", ".", ".."} and not {"/", "\\"} & set(name)
