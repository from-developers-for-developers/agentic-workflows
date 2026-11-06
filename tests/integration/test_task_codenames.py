# SPDX-License-Identifier: GPL-3.0-or-later
"""A task's codename: an alias every command addressing the task accepts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ww.cli import main
from ww.errors import StateError
from ww.service import WorkflowService
from ww.storage import Storage
from ww.storage_adapters import FileTaskStorageAdapter, TaskMetadata
from ww.storage_adapters.memory import MemoryTaskStorageAdapter
from ww.task_codenames import resolve_task, set_codename, validate_codename
from ww.task_list import task_rows

WORKFLOWS = """workflows:
  - name: task
    steps:
      - develop: Develop.
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
        json.dumps(
            {
                "task_format": "TASK-{{digit}}",
                "extensions": {"ww/git": {"base_branches": {"default": "main"}}},
            }
        ),
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
    *extra: str,
    code: int = 0,
) -> str:
    return _run(
        root,
        capsys,
        "start",
        task_id,
        "--workflow",
        "task",
        "--agent",
        "codex",
        "--requirements",
        "Build it.",
        "--description",
        "Build it",
        *extra,
        "--role",
        "manager",
        code=code,
    )


def test_a_codename_given_at_start_names_the_task_everywhere(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    page = _start(project, capsys, "TASK-1", "--codename", "Atlas")

    assert page.startswith("# TASK-1 (Atlas) · task\n")
    status = _run(project, capsys, "status", "atlas")
    assert "task id: TASK-1" in status
    instruction = json.loads(
        _run(project, capsys, "instruction", "ATLAS", "--role", "manager", "--json")
    )
    assert instruction["task_id"] == "TASK-1"
    assert instruction["task_codename"] == "Atlas"
    _run(project, capsys, "next", "Atlas", "--role", "manager")
    _run(
        project,
        capsys,
        "complete",
        "Atlas",
        "--role",
        "manager",
        "--artifact",
        "Done.",
        "--summary",
        "Done.",
    )
    (row,) = json.loads(_run(project, capsys, "tasks", "--json"))["tasks"]
    assert (row["task_id"], row["codename"]) == ("TASK-1", "Atlas")
    # The branch keeps the task ID.
    branches = subprocess.run(
        ["git", "branch", "--list"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "atlas" not in branches.casefold()


def test_the_codename_command_shows_sets_changes_and_clears_it(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys, "TASK-1")

    assert _run(project, capsys, "codename", "TASK-1") == (
        "TASK-1 has no codename.\n"
    )
    assert _run(project, capsys, "codename", "TASK-1", "Atlas") == (
        "TASK-1 is now also called Atlas.\n"
    )
    assert json.loads(_run(project, capsys, "codename", "atlas", "--json")) == {
        "task_id": "TASK-1",
        "codename": "Atlas",
    }
    _run(project, capsys, "codename", "Atlas", "Orion")
    assert _run(project, capsys, "codename", "orion") == (
        "TASK-1 is also called Orion.\n"
    )
    assert _run(project, capsys, "codename", "Orion", "--clear") == (
        "TASK-1 has no codename now.\n"
    )
    assert not (project / ".ww/tasks/TASK-1/codename.json").exists()
    assert "no codename" in _run(project, capsys, "codename", "TASK-1")


def test_a_codename_for_a_missing_task_is_refused(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error = _run(project, capsys, "codename", "TASK-9", "Atlas", code=1)

    assert "task 'TASK-9' was not found" in error


def test_a_codename_is_unique_in_any_letter_case(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys, "TASK-1", "--codename", "Atlas")
    _start(project, capsys, "TASK-2")

    error = _run(project, capsys, "codename", "TASK-2", "ATLAS", code=1)

    assert "codename 'ATLAS' already names TASK-1" in error
    # A start with a taken codename writes nothing.
    error = _start(project, capsys, "TASK-3", "--codename", "atlas", code=1)
    assert "already names TASK-1" in error
    assert not (project / ".ww/tasks/TASK-3").exists()
    # Giving a task its own codename again, in another case, is fine.
    _run(project, capsys, "codename", "TASK-1", "ATLAS")
    assert task_rows(Storage(project))[0].codename == "ATLAS"


def test_a_codename_may_not_read_as_a_task_id(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys, "TASK-1")
    _start(project, capsys, "Release")

    for name, message in (
        ("TASK-7", "reads as a tracker key"),
        ("release", "reads as task ID Release"),
        ("7up", "must start with a letter"),
        ("Two words", "must start with a letter and hold only"),
        ("A" * 41, "longer than 40 characters"),
    ):
        error = _run(project, capsys, "codename", "TASK-1", name, code=1)
        assert message in error, (name, error)


def test_a_new_task_id_may_not_be_another_task_s_codename(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys, "TASK-1", "--codename", "Atlas")

    error = _start(project, capsys, "atlas", code=1)

    assert "'atlas' is the codename of TASK-1" in error
    assert not (project / ".ww/tasks/atlas").exists()


def test_an_existing_task_id_wins_over_a_codename() -> None:
    tasks = MemoryTaskStorageAdapter()
    tasks.write_codename("TASK-1", "Atlas")
    tasks.write_task_metadata(TaskMetadata("TASK-1"))
    tasks.write_task_metadata(TaskMetadata("TASK-1/api"))

    assert resolve_task(tasks, "atlas") == "TASK-1"
    assert resolve_task(tasks, "Atlas/api") == "TASK-1/api"
    assert resolve_task(tasks, "Orion") == "Orion"
    tasks.write_task_metadata(TaskMetadata("Orion"))
    tasks.write_codename("TASK-1", "Orion")
    assert resolve_task(tasks, "Orion") == "Orion"


def test_lookup_matches_a_codename(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys, "TASK-1", "--codename", "Atlas")

    report = json.loads(
        _run(project, capsys, "lookup", "atlas", "--agent", "codex", "--json")
    )

    assert report["outcome"] == "continue"
    assert report["task_id"] == "TASK-1"
    assert report["matches"][0]["codename"] == "Atlas"


def test_tasks_shows_a_codename_column_only_when_a_task_has_one(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys, "TASK-1")
    _start(project, capsys, "TASK-2")
    assert "Atlas" not in _run(project, capsys, "tasks")
    first = _run(project, capsys, "tasks").splitlines()[0]
    assert first.split()[:2] == ["TASK-1", "pending"]

    _run(project, capsys, "codename", "TASK-2", "Atlas")
    lines = _run(project, capsys, "tasks").splitlines()

    assert lines[0].split()[:2] == ["TASK-1", "pending"]
    assert lines[1].split()[:3] == ["TASK-2", "Atlas", "pending"]
    assert lines[0].index("pending") == lines[1].index("pending")


def test_a_bootstrapped_task_takes_its_codename_when_its_id_is_bound(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: jira-task
    steps:
      - name: create-jira
        mcp: jira
        description: Create the Jira issue.
        variables:
          - name: task_id
      - name: develop
        description: Implement work for {{task_id}}.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    pending = service.start("jira-task", None, agent="codex", codename="Atlas")
    service.next(pending.task_id)

    service.complete(
        pending.task_id, (("task_id", "PROJ-1"),), summary_for_next="Done."
    )

    assert service.task_codename("PROJ-1") == "Atlas"


def test_removing_a_task_removes_its_codename(tmp_path: Path) -> None:
    tasks = FileTaskStorageAdapter(tmp_path)
    tasks.write_codename("TASK-1", "Atlas")

    assert tasks.read_codename("TASK-1") == "Atlas"
    assert tasks.remove_task("TASK-1")
    assert tasks.read_codename("TASK-1") is None


def test_set_codename_validates_and_trims() -> None:
    tasks = MemoryTaskStorageAdapter()
    tasks.write_task_metadata(TaskMetadata("TASK-1"))

    assert set_codename(tasks, "TASK-1", "  Atlas ", None) == "Atlas"
    assert validate_codename("Project_Atlas-2.0") == "Project_Atlas-2.0"
    with pytest.raises(StateError, match="must not be empty"):
        validate_codename("  ")
