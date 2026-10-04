# SPDX-License-Identifier: GPL-3.0-or-later
"""The primary checkout's ww.yaml is in force; a differing worktree copy is flagged."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ww.cli import main
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "T1"
CONFIG = "workflows:\n  - name: task\n    steps:\n      - work: Do the work.\n"


def _task_in_worktree(tmp_path: Path) -> Path:
    (tmp_path / "ww.yaml").write_text(CONFIG, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    service.start("task", TASK, agent="codex", init_artifact="Do it.")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    state, snapshot = service.load(TASK)
    service.commit(replace(state, working_directory="wt"), snapshot)
    return worktree


def _notices(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> list[str]:
    assert main(["--root", str(tmp_path), "instruction", TASK, "--json"]) == 0
    return json.loads(capsys.readouterr().out)["notices"]


def test_a_differing_worktree_configuration_is_flagged_on_the_pages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    worktree = _task_in_worktree(tmp_path)
    (worktree / "ww.yaml").write_text(CONFIG + "      - extra: More.\n")
    (notice,) = _notices(tmp_path, capsys)
    assert "worktree has its own `ww.yaml` that differs" in notice
    assert str(tmp_path / "ww.yaml") in notice
    assert "not in force" in notice
    assert main(["--root", str(tmp_path), "instruction", TASK]) == 0
    assert notice in capsys.readouterr().out


def test_an_identical_or_missing_worktree_configuration_is_not_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    worktree = _task_in_worktree(tmp_path)
    assert _notices(tmp_path, capsys) == []
    (worktree / "ww.yaml").write_text(CONFIG, encoding="utf-8")
    assert _notices(tmp_path, capsys) == []
