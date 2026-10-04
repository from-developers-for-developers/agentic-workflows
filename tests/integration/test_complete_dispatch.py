# SPDX-License-Identifier: GPL-3.0-or-later
"""``complete --role manager`` dispatches the manager's own next step."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.cli import main
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "T1"
CONFIG = """workflows:
  - name: task
    steps:
      - plan: Plan the work.
        role: manager
      - decide: Decide the approach.
        role: manager
      - build: Build it.
"""


def _setup(tmp_path: Path, config: str = CONFIG) -> WorkflowService:
    (tmp_path / "ww.yaml").write_text(config, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "task",
        TASK,
        agent="codex",
        workflow_runtime="auto",
        init_artifact="Do it.",
        caller_role="manager",
    )
    return service


def _complete(tmp_path: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> str:
    code = main(
        [
            "--root", str(tmp_path), "complete", TASK, "--role", "manager",
            "--artifact", "Done.", "--summary", "Next.", *extra,
        ]
    )  # fmt: skip
    assert code == 0
    return capsys.readouterr().out


def test_the_manager_completes_and_is_given_its_own_next_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _setup(tmp_path)
    service.next(TASK, caller_role="manager")
    page = _complete(tmp_path, capsys)
    assert "Manager: perform the `decide` assignment" in page
    assert "ww dispatched your next step, `decide`, as `next` would" in page
    state, _ = service.load(TASK)
    assert state.item_executions[state.cursor].status == "in_progress"


def test_json_carries_the_dispatched_page_and_its_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _setup(tmp_path)
    service.next(TASK, caller_role="manager")
    data = json.loads(_complete(tmp_path, capsys, "--json"))
    assert data["item_name"] == "decide"
    assert data["item_status"] == "in_progress"
    assert len(data["notices"]) == 1


def test_no_dispatch_keeps_the_pending_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _setup(tmp_path)
    service.next(TASK, caller_role="manager")
    page = _complete(tmp_path, capsys, "--no-dispatch")
    assert "dispatched your next step" not in page
    state, _ = service.load(TASK)
    assert state.item_executions[state.cursor].status == "pending"


def test_a_worker_must_be_selected_so_nothing_is_dispatched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _setup(tmp_path)
    for _ in range(2):
        service.next(TASK, caller_role="manager")
        service.complete(
            TASK, artifact="Done.", summary_for_next="Next.", caller_role="manager"
        )
    page = _pending_page(service)
    assert "build" in page
    state, _ = service.load(TASK)
    assert state.item_executions[state.cursor].status == "pending"


def _pending_page(service: WorkflowService) -> str:
    from ww.output_adapters.markdown import MarkdownOutputAdapter

    return MarkdownOutputAdapter().render_instruction(
        service.instruction(TASK, caller_role="manager")
    )


def test_completing_the_step_before_a_worker_step_does_not_dispatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _setup(tmp_path)
    service.next(TASK, caller_role="manager")
    service.complete(
        TASK, artifact="Done.", summary_for_next="Next.", caller_role="manager"
    )
    service.next(TASK, caller_role="manager")
    page = _complete(tmp_path, capsys)
    assert "dispatched your next step" not in page
    assert "--selected-agent" in page
    state, _ = service.load(TASK)
    assert state.item_executions[state.cursor].status == "pending"


def test_a_worker_completion_never_dispatches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _setup(tmp_path)
    for _ in range(2):
        service.next(TASK, caller_role="manager")
        service.complete(
            TASK, artifact="Done.", summary_for_next="Next.", caller_role="manager"
        )
    service.next(TASK, caller_role="manager")
    token = service.load(TASK)[0].assignment_token
    code = main(
        [
            "--root", str(tmp_path), "complete", TASK, "--role", "worker",
            "--assignment", str(token), "--artifact", "Built.", "--summary", "Ok.",
        ]
    )  # fmt: skip
    assert code in (0, 1)
    assert "dispatched your next step" not in capsys.readouterr().out


STOP = """workflows:
  - name: task
    steps:
      - plan: Plan the work.
        role: manager
      - gate: ~
        shell: echo broken; exit 1
      - after: Continue.
        role: manager
"""

OUTCOME = """workflows:
  - name: task
    steps:
      - plan: Plan the work.
        role: manager
      - assess:
          question: Is it fine?
          role: manager
          outcomes:
            positive:
              steps:
                - done: Wrap up.
                  role: manager
            negative:
              stop_workflow: true
"""


def test_a_step_ww_runs_is_never_dispatched_by_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _setup(tmp_path, STOP)
    service.next(TASK, caller_role="manager")
    page = _complete(tmp_path, capsys)
    assert "dispatched your next step" not in page
    assert "./ww next T1 --role manager" in page


def test_an_assessment_outcome_choice_is_left_to_the_manager(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _setup(tmp_path, OUTCOME)
    service.next(TASK, caller_role="manager")
    first = _complete(tmp_path, capsys)
    # The assessment itself is the manager's own step, so it is dispatched.
    assert "ww dispatched your next step, `assess`" in first
    second = _complete(tmp_path, capsys)
    assert "dispatched your next step" not in second
    assert "--outcome" in second
