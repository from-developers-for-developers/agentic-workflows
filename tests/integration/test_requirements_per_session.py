# SPDX-License-Identifier: GPL-3.0-or-later
"""The task requirements print on the first page of each session."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.errors import ConfigurationError
from ww.instructions import Instruction
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.project_config import load_project_config
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"
ASK = "Make the example thing work."

WORKERS = """workflows:
  - name: review
    steps:
      - review: Review the pull request.
        items:
          description: Split based on the review comments.
          assignment: per_item
          steps:
            - analyze: Analyze this comment.
              item_phase: analyze
            - fix: Fix this comment.
              item_phase: resolve
"""

MANAGER = """workflows:
  - name: task
    steps:
      - plan: Plan it.
        role: manager
      - act: Act on it.
        role: manager
"""

SINGLE = """workflows:
  - name: task
    steps:
      - plan: Plan it.
      - act: Act on it.
"""

md = MarkdownOutputAdapter()


def _service(
    root: Path, workflows: str, runtime: str = "auto", setting: str | None = None
) -> WorkflowService:
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    if setting is not None:
        (root / "ww.json").write_text(
            json.dumps({"pages": {"worker_requirements": setting}}), encoding="utf-8"
        )
    service = WorkflowService(Storage(root))
    service.start(
        "review" if workflows is WORKERS else "task",
        TASK,
        agent="codex",
        workflow_runtime=runtime,
        init_artifact=ASK,
        caller_role="manager",
    )
    return service


def _in_full(page: Instruction) -> bool:
    assert page.task_requirements == ASK
    return page.requirements_in_full


def _collect(service: WorkflowService) -> None:
    service.next(TASK, caller_role="manager")
    service.add_item(TASK, WorkItem("c1", "First comment"))
    service.complete(
        TASK,
        artifact="collected",
        caller_role="worker",
        assignment=assignment_token(service, TASK),
        summary_for_next="Done.",
    )
    service.next(TASK, caller_role="manager")


def _worker_pages(service: WorkflowService) -> tuple[Instruction, Instruction]:
    """A delegated worker's first page and the page of its second stage."""
    _collect(service)
    first = service.status(
        TASK, caller_role="worker", assignment=assignment_token(service, TASK)
    )
    service.update_item(TASK, "c1", processed_item="analysis")
    second = service.complete(
        TASK,
        artifact="analyzed",
        caller_role="worker",
        assignment=assignment_token(service, TASK),
        summary_for_next="Done.",
    )
    assert first.item_name == "analyze" and second.item_name == "fix"
    assert second.continues_assignment
    return first, second


def test_a_worker_assignment_prints_the_requirements_on_its_first_page_only(
    tmp_path: Path,
) -> None:
    first, second = _worker_pages(_service(tmp_path, WORKERS))

    assert _in_full(first)
    assert ASK in md.render_instruction(first)
    assert not _in_full(second)
    rendered = md.render_instruction(second)
    assert ASK not in rendered
    assert f"`./ww requirements {TASK}`" in rendered


def test_every_worker_assignment_starts_with_the_full_requirements(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, WORKERS)
    _collect(service)
    state, _ = service.load(TASK)
    # The collecting step's assignment is over; this one is a fresh session.
    page = service.status(
        TASK, caller_role="worker", assignment=assignment_token(service, TASK)
    )
    assert state.assignment_token is not None
    assert _in_full(page)


def test_pointer_setting_keeps_the_text_off_worker_first_pages(
    tmp_path: Path,
) -> None:
    first, second = _worker_pages(_service(tmp_path, WORKERS, setting="pointer"))

    for page in (first, second):
        assert not _in_full(page)
        rendered = md.render_instruction(page)
        assert ASK not in rendered
        assert f"`./ww requirements {TASK}`" in rendered


def test_full_setting_is_the_default(tmp_path: Path) -> None:
    first, _ = _worker_pages(_service(tmp_path, WORKERS, setting="full"))
    assert _in_full(first)


def test_the_manager_prints_the_requirements_once(tmp_path: Path) -> None:
    pointer = _service(tmp_path, MANAGER, setting="pointer")
    first = pointer.next(TASK, caller_role="manager")
    assert _in_full(first)
    pointer.complete(
        TASK, artifact="Planned.", summary_for_next="Go.", caller_role="manager"
    )
    second = pointer.next(TASK, caller_role="manager")
    assert second.item_name == "act"
    assert not _in_full(second)
    assert f"`./ww requirements {TASK}`" in md.render_instruction(second)


def test_single_runtime_prints_them_on_the_first_agent_step_only(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, SINGLE, runtime="single", setting="pointer")
    first = service.next(TASK)
    assert _in_full(first)
    service.complete(TASK, artifact="Planned.", summary_for_next="Go.")
    second = service.next(TASK)
    assert second.item_name == "act"
    assert not _in_full(second)


def test_amendments_print_on_every_page(tmp_path: Path) -> None:
    service = _service(tmp_path, WORKERS)
    service.amend(TASK, "Also mind the docs.", caller_role="manager")
    first, second = _worker_pages(service)
    for page in (first, second):
        assert "Also mind the docs." in md.render_instruction(page)


@pytest.mark.parametrize("value", ["always", "", True, 1])
def test_an_invalid_setting_is_rejected(tmp_path: Path, value: object) -> None:
    (tmp_path / "ww.json").write_text(
        json.dumps({"pages": {"worker_requirements": value}}), encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="pages.worker_requirements"):
        load_project_config(tmp_path / "ww.json")
