# SPDX-License-Identifier: GPL-3.0-or-later
"""A dead task's leftovers must not shape a new task, and ./ww stays current.

Regressions found while starting TASK-2: ww/git branch records outlived the
task state they belonged to, so a reused ID took the old task's base branch;
and ``init`` kept a launcher an older ww had written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.integration.test_git_extension import context, git_extension, handler
from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.defaults import PROJECT_LAUNCHER
from ww.extensions import ExtensionStore
from ww.service import WorkflowService
from ww.storage import Storage

_WORKFLOW = """workflows:
  - name: task
    steps:
      - work: Do the work.
"""


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments), cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main", ".")
    _git(tmp_path, "config", "user.email", "t@e.st")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    # A development branch ahead of main, so the chosen base is visible.
    _git(tmp_path, "switch", "-qc", "dev")
    (tmp_path / "dev.txt").write_text("dev\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "dev")
    return tmp_path


def _record(root: Path, **fields: object) -> None:
    ExtensionStore(root, "ww/git").append_line(
        "branches.jsonl", json.dumps(fields, sort_keys=True)
    )


_CONFIG = {
    "separate_branch": True,
    "base_branches": {"default": "dev"},
    "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
}


# --------------------------------------------------------------------------- #
# Bug 1: a dead task's ww/git branch record chooses the base
# --------------------------------------------------------------------------- #


def test_a_dead_tasks_record_for_another_branch_does_not_choose_the_base(
    repository: Path,
) -> None:
    # An old TASK-1 was a hotfix from main; its task state is long gone.
    _record(repository, task_id="TASK-1", branch="hotfix/task-1", base="main")

    result = handler("start-task-branch")(context(repository, _CONFIG))

    assert result.ok, result.error
    assert "from dev" in result.output
    assert _git(repository, "merge-base", "feature/task-1", "dev") == _git(
        repository, "rev-parse", "dev"
    )


def test_a_record_for_a_deleted_branch_does_not_choose_the_base(
    repository: Path,
) -> None:
    # Same branch name, but the branch it recorded no longer exists.
    _record(repository, task_id="TASK-1", branch="feature/task-1", base="main")

    result = handler("start-task-branch")(context(repository, _CONFIG))

    assert result.ok, result.error
    assert "from dev" in result.output


def test_a_record_for_the_existing_task_branch_keeps_its_base(
    repository: Path,
) -> None:
    started = handler("start-task-branch")(
        context(repository, {**_CONFIG, "base_branches": {"default": "main"}})
    )
    assert started.ok, started.error
    _git(repository, "switch", "-q", "dev")

    # The configured base changed since; the live branch keeps its own.
    branch, base, error = git_extension._task_branch(
        context(repository, _CONFIG), git_extension.settings_from(_CONFIG)
    )

    assert error is None
    assert (branch, base) == ("feature/task-1", "main")


def _git_service(root: Path, task_format: str | None = None) -> WorkflowService:
    (root / "ww.yaml").write_text(_WORKFLOW, encoding="utf-8")
    settings: dict[str, object] = {"enabled": True, "extensions": {"ww/git": {}}}
    if task_format is not None:
        settings["task_format"] = task_format
    (root / "ww.json").write_text(json.dumps(settings), encoding="utf-8")
    return WorkflowService(Storage(root))


def _branch_records(root: Path) -> list[dict[str, object]]:
    lines = ExtensionStore(root, "ww/git").read_lines("branches.jsonl")
    return [json.loads(line) for line in lines if line]


def _commit_records(root: Path) -> list[dict[str, object]]:
    lines = ExtensionStore(root, "ww/git").read_lines("commits.jsonl")
    return [json.loads(line) for line in lines if line]


def test_reset_forgets_the_tasks_git_records(repository: Path) -> None:
    service = _git_service(repository)
    start_after_init(service, "task", "T1", agent="codex")
    _record(repository, task_id="T1", branch="feature/t1", base="dev")
    _record(repository, task_id="T1/A", branch="feature/t1-a", base="feature/t1")
    _record(repository, task_id="T10", branch="feature/t10", base="dev")
    store = ExtensionStore(repository, "ww/git")
    store.append_line("commits.jsonl", json.dumps({"task_id": "T1", "sha": "a"}))
    store.append_line("commits.jsonl", json.dumps({"task_id": "T10", "sha": "b"}))

    service.reset("T1")

    assert [record["task_id"] for record in _branch_records(repository)] == ["T10"]
    assert [record["task_id"] for record in _commit_records(repository)] == ["T10"]


def test_a_generated_id_skips_one_ww_git_still_holds_records_for(
    repository: Path,
) -> None:
    service = _git_service(repository, "TASK-{{digit}}")
    _record(repository, task_id="TASK-1", branch="hotfix/task-1", base="main")

    started = service.start("task", None, agent="codex")

    assert started.task_id == "TASK-2"


def test_a_generated_id_skips_one_whose_task_branch_exists(repository: Path) -> None:
    service = _git_service(repository, "TASK-{{digit}}")
    # The default branch name format is the task ID itself.
    _git(repository, "branch", "task-1")

    started = service.start("task", None, agent="codex")

    assert started.task_id == "TASK-2"


# --------------------------------------------------------------------------- #
# Bug 2: a stale ./ww launcher survives init
# --------------------------------------------------------------------------- #

_OLD_LAUNCHER = """#!/bin/sh
set -eu
executable=ww-agentic-workflows
for path in ww-agentic-workflows.json ww-agentic-workflows.local.json; do :; done
exec "$executable" "$@"
"""


def test_init_rewrites_a_launcher_that_differs_from_the_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ww").write_text(_OLD_LAUNCHER, encoding="utf-8")

    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    output = capsys.readouterr().out

    assert (tmp_path / "ww").read_text(encoding="utf-8") == PROJECT_LAUNCHER
    assert "ww (updated" in output


def test_lint_warns_about_a_launcher_that_differs_from_the_template(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ww.yaml").write_text(_WORKFLOW, encoding="utf-8")
    (tmp_path / "ww").write_text(_OLD_LAUNCHER, encoding="utf-8")

    assert main(["--root", str(tmp_path), "lint"]) == 0
    output = capsys.readouterr().out

    assert "./ww" in output
    assert "init" in output
