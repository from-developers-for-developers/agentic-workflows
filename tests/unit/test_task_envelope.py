# SPDX-License-Identifier: GPL-3.0-or-later
"""The task envelope: what any ww reads of any task, whatever its schema."""

from __future__ import annotations

import json
from pathlib import Path

from ww.task_envelope import (
    RunEnvelope,
    TaskEnvelope,
    read_task_envelope,
    run_directories,
    step_artifacts,
)


def _write(path: Path, content: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text, encoding="utf-8")


def test_runs_inline_in_the_index_are_read(tmp_path: Path) -> None:
    _write(
        tmp_path / "state.json",
        {
            "format": "ww.task-state",
            "schema_version": 2,
            "task_id": "TASK-1",
            "runs": [
                {
                    "run_id": "01-bugfix",
                    "workflow": "bugfix",
                    "snapshot": {},
                    "state": {
                        "status": "completed",
                        "created_at": "2026-10-02T10:20:55Z",
                        "updated_at": "2026-10-02T10:29:22Z",
                    },
                }
            ],
        },
    )

    assert read_task_envelope(tmp_path, "TASK-1") == TaskEnvelope(
        "TASK-1",
        "ww.task-state",
        2,
        None,
        (
            RunEnvelope(
                "01-bugfix",
                "bugfix",
                "completed",
                "2026-10-02T10:20:55Z",
                "2026-10-02T10:29:22Z",
            ),
        ),
    )


def test_runs_the_index_names_are_read_from_their_run_documents(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "state.json",
        {
            "format": "ww.task-state",
            "schema_version": 1,
            "written_by": "1.0.0b18",
            "task_id": "TASK-1",
            "runs": [
                {"id": "01-task", "revision": 3},
                {"id": "02-merge", "revision": 4},
            ],
        },
    )
    _write(
        tmp_path / "runs/01-task/state.3.json",
        {
            "written_by": "1.0.0b17",
            "run": {
                "run_id": "01-task",
                "workflow": "task",
                "state": {"status": "completed", "updated_at": "2026-11-01T09:00:00Z"},
            },
        },
    )

    envelope = read_task_envelope(tmp_path, "TASK-1")

    assert envelope.written_by == "1.0.0b18"
    # A run whose document is missing keeps its ID; the rest is unknown.
    assert envelope.runs == (
        RunEnvelope(
            "01-task",
            "task",
            "completed",
            None,
            "2026-11-01T09:00:00Z",
            "1.0.0b17",
        ),
        RunEnvelope("02-merge"),
    )


def test_odd_values_read_as_unknown(tmp_path: Path) -> None:
    task = tmp_path / "TASK-1"
    _write(
        task / "state.json",
        {
            "format": 7,
            "schema_version": True,
            "written_by": ["1.0"],
            "task_id": "",
            "runs": [
                "01-task",
                {"workflow": "no-id"},
                {"run_id": "02-task", "state": "completed"},
                {"run_id": "03-task", "state": {"workflow": "task", "status": 1}},
                # A run document is never looked for outside the run's directory.
                {"id": "../../elsewhere", "revision": 1},
            ],
        },
    )
    # What ``runs/../../elsewhere`` would name, were it followed.
    _write(tmp_path / "elsewhere/state.1.json", {"run": {"workflow": "x"}})

    envelope = read_task_envelope(task, "TASK-1")

    assert envelope == TaskEnvelope(
        "TASK-1",
        runs=(
            RunEnvelope("02-task"),
            RunEnvelope("03-task", "task"),
            RunEnvelope("../../elsewhere"),
        ),
    )


def test_an_index_that_does_not_read_is_a_problem_not_an_error(
    tmp_path: Path,
) -> None:
    assert read_task_envelope(tmp_path, "TASK-1").problem

    _write(tmp_path / "state.json", "{not json")
    broken = read_task_envelope(tmp_path, "TASK-1")
    assert (broken.task_id, broken.runs) == ("TASK-1", ())
    assert broken.problem and broken.problem.startswith("state.json")

    _write(tmp_path / "state.json", "[1, 2]")
    assert read_task_envelope(tmp_path, "TASK-1").problem == (
        "state.json is not a mapping"
    )


def test_run_directories_are_listed_by_name(tmp_path: Path) -> None:
    (tmp_path / "runs/02-merge").mkdir(parents=True)
    (tmp_path / "runs/01-task").mkdir()
    _write(tmp_path / "runs/notes.md", "not a run")

    assert run_directories(tmp_path) == ("01-task", "02-merge")
    assert run_directories(tmp_path / "missing") == ()


def test_step_artifacts_are_the_run_s_markdown_files_in_order(
    tmp_path: Path,
) -> None:
    steps = tmp_path / "runs/01-task/steps"
    for name in (
        "02-develop.md",
        "01-init.md",
        "02-develop/iteration-01/01-run.md",
        "02-develop/.hooks/before_complete/03-format.md",
        "02-develop/output.txt",
    ):
        _write(steps / name, "# step\n")
    _write(tmp_path / "secret.md", "not an artifact")
    (steps / "03-link.md").symlink_to(tmp_path / "secret.md")

    assert [path.as_posix() for path in step_artifacts(tmp_path, "01-task")] == [
        "01-init.md",
        "02-develop.md",
        "02-develop/.hooks/before_complete/03-format.md",
        "02-develop/iteration-01/01-run.md",
    ]
    for run_id in ("..", ".", "", "01-task/steps", "missing"):
        assert step_artifacts(tmp_path, run_id) == ()
