# SPDX-License-Identifier: GPL-3.0-or-later
"""A task's one-line description, given at ``start``, and ``ww tasks``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ww.cli import main
from ww.errors import StateError
from ww.service import WorkflowService
from ww.storage import Storage
from ww.task_list import task_rows

WORKFLOWS = """workflows:
  - name: task
    steps:
      - develop: Develop.
  - name: merge
    steps:
      - merge: Merge.
"""


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main", ".")
    _git(tmp_path, "config", "user.email", "t@e.st")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (tmp_path / "ww.json").write_text(
        json.dumps({"extensions": {"ww/git": {"base_branches": {"default": "main"}}}}),
        encoding="utf-8",
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def _run(
    root: Path, capsys: pytest.CaptureFixture[str], *arguments: str, code: int = 0
) -> str:
    assert main(["--root", str(root), *arguments]) == code
    captured = capsys.readouterr()
    return captured.out if code == 0 else captured.err


def _start(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    task_id: str,
    description: str,
    workflow: str = "task",
) -> None:
    _run(
        root,
        capsys,
        "start",
        task_id,
        "--workflow",
        workflow,
        "--agent",
        "codex",
        "--requirements",
        "Build it, in full detail.",
        "--description",
        description,
        "--role",
        "manager",
    )


def _listed(root: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, dict]:
    report = json.loads(_run(root, capsys, "tasks", "--json"))
    return {row["task_id"]: row for row in report["tasks"]}


def test_start_stores_the_description_and_tasks_lists_it(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys, "TASK-1", "  Add a --quiet flag to greet  ")

    row = _listed(project, capsys)["TASK-1"]

    assert row == {
        "task_id": "TASK-1",
        "description": "Add a --quiet flag to greet",
        "status": "pending",
        "workflows": ["task"],
        "updated_at": row["updated_at"],
        "unreadable": None,
    }
    assert row["updated_at"]
    (document,) = (project / ".ww/tasks/TASK-1/runs/01-task").glob("state.*.json")
    state = json.loads(document.read_text(encoding="utf-8"))["run"]["state"]
    assert state["description"] == "Add a --quiet flag to greet"


def test_start_requires_a_description(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "--root",
                str(project),
                "start",
                "TASK-1",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--requirements",
                "Build it.",
            ]
        )

    assert raised.value.code == 2
    assert "--description" in capsys.readouterr().err
    assert not (project / ".ww/tasks/TASK-1").exists()


@pytest.mark.parametrize(
    ("description", "message"),
    [
        ("   ", "non-empty --description"),
        ("First line\nsecond line", "single line"),
        ("x" * 121, "at most 120 characters, not 121"),
    ],
)
def test_a_blank_multi_line_or_long_description_is_refused(
    project: Path, description: str, message: str
) -> None:
    service = WorkflowService(Storage(project))

    with pytest.raises(StateError, match=message):
        service.start(
            "task",
            "TASK-1",
            agent="codex",
            init_artifact="Do it.",
            description=description,
        )

    assert not (project / ".ww/tasks/TASK-1").exists()


def test_a_later_run_keeps_the_task_s_first_description(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys, "TASK-1", "Add a --quiet flag to greet")
    service = WorkflowService(Storage(project))
    service.next("TASK-1")
    service.complete("TASK-1", artifact="Done.", summary_for_next="Done.")
    service.complete("TASK-1", artifact="Done.", variables={"summary": "Done."})
    _start(project, capsys, "TASK-1", "Merge TASK-1 into main", workflow="merge")

    row = _listed(project, capsys)["TASK-1"]

    assert row["description"] == "Add a --quiet flag to greet"
    assert row["workflows"] == ["task", "merge"]
    assert row["status"] == "pending"


def test_tasks_lists_every_task_in_natural_order_with_direct_and_unreadable_ones(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys, "TASK-10", "Tenth")
    _start(project, capsys, "TASK-2", "Second")
    _run(project, capsys, "record", "TASK-3", "--summary", "Fixed a typo.")
    unreadable = project / ".ww/tasks/TASK-4"
    unreadable.mkdir(parents=True)
    (unreadable / "state.json").write_text('{"schema_version": 99}', encoding="utf-8")

    rows = task_rows(Storage(project))

    assert [(row.task_id, row.description, row.status) for row in rows] == [
        ("TASK-2", "Second", "pending"),
        ("TASK-3", None, "direct"),
        ("TASK-4", None, "unreadable"),
        ("TASK-10", "Tenth", "pending"),
    ]
    assert rows[2].unreadable
    assert all(row.unreadable is None for row in rows if row.task_id != "TASK-4")

    text = _run(project, capsys, "tasks")
    lines = text.splitlines()
    assert lines[0].startswith("TASK-2 ")
    assert lines[0].endswith("  Second")
    assert lines[1].startswith("TASK-3 ") and "direct" in lines[1]
    assert lines[3].startswith("TASK-10") and lines[3].endswith("  Tenth")
    assert lines[-1] == (
        "Read only in part, by an older or newer ww: TASK-4; "
        "`ww tasks --json` says why."
    )


def test_an_older_task_is_listed_from_its_envelope_with_its_description(
    project: Path,
) -> None:
    task = project / ".ww/tasks/TASK-7"
    task.mkdir(parents=True)
    (task / "state.json").write_text(
        json.dumps(
            {
                "format": "ww.task-state",
                "schema_version": 2,
                "task_id": "TASK-7",
                "runs": [
                    {
                        "run_id": "01-task",
                        "workflow": "task",
                        "state": {
                            "status": "completed",
                            "created_at": "2026-10-02T10:20:55Z",
                            "updated_at": "2026-10-02T10:29:22Z",
                            "description": "Write readable YAML",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    (row,) = task_rows(Storage(project))

    assert (row.task_id, row.description, row.status, row.workflows) == (
        "TASK-7",
        "Write readable YAML",
        "completed",
        ("task",),
    )
    assert row.updated_at == "2026-10-02T10:29:22Z"
    assert row.unreadable


def test_an_empty_store_says_so(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(project, capsys, "tasks") == "No tasks.\n"
    assert json.loads(_run(project, capsys, "tasks", "--json")) == {"tasks": []}


def test_a_child_is_listed_after_its_parent_with_its_first_line(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - work: Do child work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "parent", "P", agent="codex", init_artifact="Split.", description="The parent"
    )
    service.next("P")
    service.add_child("P", "c1", "Split the parser\nThen move the tests.")
    service.complete("P", artifact="split", summary_for_next="Done.")
    service.next("P")
    service.start_child("P", "c1")

    rows = task_rows(Storage(tmp_path))

    assert [(row.task_id, row.description) for row in rows] == [
        ("P", "The parent"),
        ("P/c1", "Split the parser"),
    ]
