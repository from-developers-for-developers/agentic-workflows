# SPDX-License-Identifier: GPL-3.0-or-later
"""Assignment tokens: a worker acts only inside the assignment it was given."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.cli import main
from ww.errors import StateError
from ww.instructions import Instruction
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"

WORKFLOWS = """workflows:
  - name: task
    steps:
      - develop: Develop it.
      - test: Test it.
        model: cheapest
      - review: Review it.
        role: manager
"""


def _service(root: Path, runtime: str = "auto") -> WorkflowService:
    (root / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    service = WorkflowService(Storage(root))
    service.start(
        "task",
        TASK,
        agent="claudecode",
        workflow_runtime=runtime,
        init_artifact="Do it.",
        caller_role="manager",
    )
    return service


def _dispatch(service: WorkflowService) -> Instruction:
    """The manager's ``next``: the page that hands the assignment out."""
    return service.next(TASK, caller_role="manager")


def _complete(service: WorkflowService, token: str | None) -> Instruction:
    return service.complete(
        TASK,
        artifact="Done.",
        summary_for_next="Done.",
        caller_role="worker",
        assignment=token,
    )


def test_the_bootstrap_command_carries_the_token_and_the_worker_acts_with_it(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    page = _dispatch(service)

    token = page.assignment_token
    assert token is not None
    rendered = MarkdownOutputAdapter().render_instruction(page)
    assert (
        f"./ww instruction {TASK} --run 01-task --role worker --assignment {token}"
        in rendered
    )

    worker_page = service.instruction(TASK, caller_role="worker", assignment=token)
    assert worker_page.item_name == "develop"
    assert f"--role worker --assignment {token}" in (
        worker_page.continuation_command or ""
    )
    done = _complete(service, token)
    assert done.control == "handoff_manager"


def test_a_worker_command_without_a_token_is_sent_back_to_the_manager(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _dispatch(service)

    for call in (
        lambda: service.instruction(TASK, caller_role="worker"),
        lambda: _complete(service, None),
        lambda: service.fail(TASK, "Stuck.", caller_role="worker"),
    ):
        with pytest.raises(StateError, match="needs --assignment <token>"):
            call()


def test_a_worker_whose_assignment_ended_cannot_act_on_the_next_one(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    old = _dispatch(service).assignment_token
    _complete(service, old)

    # Between assignments the old worker is only told to go back.
    returning = service.instruction(TASK, caller_role="worker", assignment=old)
    assert returning.continuation_command is None or "--role manager" in (
        returning.continuation_command
    )
    with pytest.raises(StateError, match="no assignment is open"):
        _complete(service, old)

    new = _dispatch(service).assignment_token
    assert new is not None and new != old
    for call in (
        lambda: service.instruction(TASK, caller_role="worker", assignment=old),
        lambda: _complete(service, old),
        lambda: service.loop(
            TASK, artifact="Done.", caller_role="worker", assignment=old
        ),
    ):
        with pytest.raises(StateError, match=f"assignment {old} is not open"):
            call()
    assert _complete(service, new).control == "handoff_manager"


def test_the_managers_step_has_a_token_no_worker_holds(tmp_path: Path) -> None:
    service = _service(tmp_path)
    worker_tokens = []
    for _ in ("develop", "test"):
        token = _dispatch(service).assignment_token
        worker_tokens.append(token)
        _complete(service, token)

    review = _dispatch(service)
    assert review.item_name == "review"
    assert review.role == "manager"
    assert review.assignment_token not in worker_tokens
    for stale in worker_tokens:
        with pytest.raises(StateError, match="is not open"):
            _complete(service, stale)
    assert _complete(service, review.assignment_token).completion_registered


def test_the_manager_gets_the_same_token_back_after_losing_it(tmp_path: Path) -> None:
    service = _service(tmp_path)
    token = _dispatch(service).assignment_token

    # A compacted manager asks again: the same assignment, the same token.
    again = WorkflowService(Storage(tmp_path)).instruction(TASK, caller_role="manager")
    assert again.assignment_token == token
    assert f"--assignment {token}" in MarkdownOutputAdapter().render_instruction(
        again
    )


def test_reassign_closes_the_old_token_and_issues_a_new_one(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(StateError, match="no open assignment to reassign"):
        service.next(TASK, caller_role="manager", reassign=True)
    old = _dispatch(service).assignment_token

    reassigned = service.next(TASK, caller_role="manager", reassign=True)

    new = reassigned.assignment_token
    assert new is not None and new != old
    with pytest.raises(StateError, match=f"assignment {old} is not open"):
        _complete(service, old)
    assert _complete(service, new).control == "handoff_manager"
    with pytest.raises(StateError, match="takes no other decision"):
        service.next(TASK, caller_role="manager", reassign=True, retry=True)


def test_the_single_runtime_needs_no_token(tmp_path: Path) -> None:
    service = _service(tmp_path, runtime="single")
    page = service.next(TASK, caller_role="manager")

    assert page.assignment_token is None
    assert "--assignment" not in (page.continuation_command or "")
    assert _complete(service, None).status in {"pending", "in_progress"}


def test_the_command_line_carries_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    token = _dispatch(service).assignment_token
    base = ["--root", str(tmp_path), "complete", TASK, "--role", "worker"]
    body = ["--artifact", "Done.", "--summary-for-next-step", "Done."]

    assert main([*base, *body]) == 1
    assert "needs --assignment <token>" in capsys.readouterr().err

    assert main([*base, "--assignment", str(token), *body]) == 0
    assert "Completion recorded successfully" in capsys.readouterr().out
