# SPDX-License-Identifier: GPL-3.0-or-later
"""Commit hooks ask for their message only when there is something to commit.

ww's state lives in memory; only git touches disk, inside a temporary
repository, as in the system scenarios.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.test_system_scenarios import MemoryExtensionRegistry, _git
from ww.config import parse_yaml_text
from ww.feedback import MemoryFeedbackStore
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage
from ww.storage_adapters import (
    MemoryProjectMetadataStorageAdapter,
    MemoryTaskStorageAdapter,
)

WORKFLOWS = """
workflows:
  - name: task
    steps:
      - develop: Implement it.
        hooks:
          after_complete:
            - ext/ww/git/handlers:git-commit: ~
      - review: Review it.
        hooks:
          after_complete:
            - ext/ww/git/handlers:git-commit: ~
      - finish: Finish it.
        hooks:
          after_complete:
            - ext/ww/git/handlers:git-commit: ~
"""

OPTIONAL = "Optional when nothing changed"


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main", ".")
    _git(tmp_path, "config", "user.email", "t@e.st")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


@pytest.fixture
def service(repository: Path) -> WorkflowService:
    return WorkflowService(
        Storage(repository),
        MemoryTaskStorageAdapter(),
        MemoryExtensionRegistry.discover(repository),
        configuration_loader=lambda: parse_yaml_text(WORKFLOWS, "scenario"),
        project_metadata=MemoryProjectMetadataStorageAdapter(),
        feedback_store=MemoryFeedbackStore(Storage(repository)),
    )


def test_commit_hooks_ask_for_a_message_only_when_there_is_work_to_commit(
    repository: Path, service: WorkflowService
) -> None:
    task, markdown = "TASK-1", MarkdownOutputAdapter()
    service.start("task", task, agent="codex", init_artifact="Do it.")

    # The step's page lists the hook's message as optional when nothing changed.
    develop = service.next(task)
    assert [(value.name, value.conditional) for value in develop.required_values] == [
        ("commit_message", True)
    ]
    assert OPTIONAL in markdown.render_instruction(develop)
    assert "--variable commit_message=" in (develop.continuation_command or "")

    # The tree is dirty: a completion without the message is accepted, and the
    # hook asks for it; the request is not optional, and it lists the work.
    (repository / "greeting.txt").write_text("Hello\n", encoding="utf-8")
    request = service.complete(
        task, artifact="Developed.", summary_for_next="Added a greeting."
    )
    assert (request.status, request.item_name) == ("awaiting_input", "git-commit")
    assert [(value.name, value.conditional) for value in request.required_values] == [
        ("commit_message", False)
    ]
    assert [(found.step, found.summary) for found in request.input_context] == [
        ("develop", "Added a greeting.")
    ]
    assert OPTIONAL not in markdown.render_instruction(request)
    with pytest.raises(
        Exception, match="missing required variable\\(s\\): commit_message"
    ):
        service.complete(task)
    review = service.complete(task, variables=(("commit_message", "Add a greeting"),))
    # Supplying the input resumes the plan up to the next step.
    assert (review.item_name, review.item_status) == ("review", "in_progress")
    assert _git(repository, "log", "--format=%s").splitlines() == [
        "TASK-1: Add a greeting",
        "seed",
    ]

    # The tree is clean: the hook runs without asking and commits nothing.
    finish = service.complete(
        task, artifact="Reviewed.", summary_for_next="Nothing to change."
    )
    assert (finish.item_name, finish.item_status) == ("finish", "in_progress")
    assert _git(repository, "log", "--format=%s").splitlines() == [
        "TASK-1: Add a greeting",
        "seed",
    ]

    # Dirty again: the request lists the steps since the last real commit; the
    # no-op after review hides nothing.
    (repository / "greeting.txt").write_text("Hello!\n", encoding="utf-8")
    request = service.complete(
        task, artifact="Finished.", summary_for_next="Polished the greeting."
    )
    assert request.status == "awaiting_input"
    assert [found.step for found in request.input_context] == ["review", "finish"]
    summary = service.complete(
        task, variables=(("commit_message", "Polish the greeting"),)
    )
    assert summary.item_name == "update-workflow-summary"
    assert _git(repository, "log", "--format=%s").splitlines() == [
        "TASK-1: Polish the greeting",
        "TASK-1: Add a greeting",
        "seed",
    ]
    assert not (repository / ".ww").exists()
