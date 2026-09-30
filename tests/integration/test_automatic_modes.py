# SPDX-License-Identifier: GPL-3.0-or-later
"""Modes reach the step page, and automatic modes apply without selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.cli import main
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """modes:
  - economy: Keep it short.
  - tdd: Write the failing test first.
    steps: [develop]
  - careful: Double-check everything.
    workflows: [bugfix]
workflows:
  - name: task
    modes: [economy]
    steps:
      - develop: Develop.
      - review: Review.
  - name: bugfix
    steps:
      - fix: Fix.
"""


def _service(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    return WorkflowService(Storage(tmp_path))


def _page(service: WorkflowService, task_id: str) -> str:
    return MarkdownOutputAdapter().render_instruction(
        service.next(task_id, caller_role="manager")
    )


def _complete(service: WorkflowService, task_id: str) -> None:
    service.complete(
        task_id,
        artifact="Done.",
        summary_for_next="Done.",
        caller_role="worker",
    )


def test_a_selected_mode_reaches_the_step_page(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.start(
        "task", "TASK-1", agent="claudecode", init_artifact="Do.", caller_role="manager"
    )

    develop = _page(service, "TASK-1")
    assert (
        "### Modes\n\nWork in these modes throughout this step:\n\n"
        "- `economy` — Keep it short.\n"
        "- `tdd` — Write the failing test first.\n"
    ) in develop

    _complete(service, "TASK-1")
    review = _page(service, "TASK-1")
    assert "- `economy` — Keep it short." in review
    assert "`tdd`" not in review


def test_mode_replaces_defaults_but_not_automatic_modes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.start(
        "task",
        "TASK-1",
        ("careful",),
        agent="claudecode",
        init_artifact="Do.",
        caller_role="manager",
    )

    develop = _page(service, "TASK-1")
    assert "- `careful` — Double-check everything.\n- `tdd`" in develop
    assert "`economy`" not in develop


def test_an_automatic_mode_by_workflow_applies_unselected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.start(
        "bugfix",
        "TASK-1",
        agent="claudecode",
        init_artifact="Do.",
        caller_role="manager",
    )

    fix = _page(service, "TASK-1")
    assert "- `careful` — Double-check everything." in fix
    assert "`economy`" not in fix


def test_a_step_without_modes_has_no_modes_section(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - develop: Develop.\n",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "task", "TASK-1", agent="claudecode", init_artifact="Do.", caller_role="manager"
    )

    assert "### Modes" not in _page(service, "TASK-1")


def test_discover_marks_automatic_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _service(tmp_path)

    assert main(["--root", str(tmp_path), "discover"]) == 0
    output = capsys.readouterr().out
    assert "- `economy` — Keep it short.\n" in output
    assert "- `tdd` — Write the failing test first. Always on: steps `develop`." in (
        output
    )
    assert "- `careful` — Double-check everything. Always on: workflows `bugfix`." in (
        output
    )

    assert main(["--root", str(tmp_path), "discover", "--json"]) == 0
    modes = {
        mode["name"]: mode for mode in json.loads(capsys.readouterr().out)["modes"]
    }
    assert modes["economy"]["automatic"] is None
    assert modes["tdd"]["automatic"] == {"steps": ["develop"]}
    assert modes["careful"]["automatic"] == {"workflows": ["bugfix"]}
