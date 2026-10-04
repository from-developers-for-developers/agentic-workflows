# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent child session settings, including interrupted launches."""

from pathlib import Path

import pytest

from ww.cli import main
from ww.errors import ConfigurationError, StateError
from ww.service import WorkflowService
from ww.storage import Storage


def _parent(tmp_path: Path, *, identity: bool = False) -> WorkflowService:
    identity_step = (
        """      - identify: Obtain the child's external ID.
        variables:
          - task_id: The external ID.
"""
        if identity
        else ""
    )
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - parent: Parent work.
    steps:
      - split: Collect children.
        role: manager
        children:
          steps:
            - implement:
                workflow: child
            - review: Review the child.
              role: manager
  - child: Child work.
    steps:
"""
        + identity_step
        + "      - work: Implement the child.\n",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "parent",
        "P",
        model="sol",
        reasoning="medium",
        workflow_runtime="auto",
        caller_role="manager",
    )
    service.next("P", caller_role="manager")
    service.add_child("P", None if identity else "A", "Child requirements.")
    service.complete(
        "P", artifact="Collected.", summary_for_next="Ready.", caller_role="manager"
    )
    service.next("P", caller_role="manager")
    return service


def test_cli_starts_single_child_and_returns_to_auto_parent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _parent(tmp_path)
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "start-child",
                "P",
                "A",
                "--runtime",
                "single",
                "--model",
                "luna",
                "--reasoning",
                "high",
            ]
        )
        == 0
    )
    capsys.readouterr()
    state, _ = service.load("P/A")
    assert (state.workflow_runtime, state.model, state.reasoning) == (
        "single",
        "luna",
        "high",
    )
    parent, _ = service.load("P")
    assert (parent.workflow_runtime, parent.model, parent.reasoning) == (
        "auto",
        "sol",
        "medium",
    )
    service.next("P/A")
    service.complete("P/A", artifact="Implemented.", summary_for_next="Done.")
    service.next("P/A")
    service.complete("P/A", (("summary", "Child done."),), summary_for_next="Done.")
    review = service.next("P", caller_role="manager")
    assert (review.item_name, review.role, review.workflow_runtime) == (
        "review",
        "manager",
        "auto",
    )


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, ("auto", "sol", "medium")),
        ({"workflow_runtime": "single"}, ("single", "sol", "medium")),
        ({"model": "luna"}, ("auto", "luna", "auto")),
        ({"model": "sol"}, ("auto", "sol", "medium")),
        ({"reasoning": "high"}, ("auto", "sol", "high")),
    ],
)
def test_omitted_launch_settings_inherit_and_model_changes_reset_reasoning(
    tmp_path: Path, settings: dict[str, str], expected: tuple[str, str, str]
) -> None:
    service = _parent(tmp_path)
    service.start_child("P", "A", **settings)
    state, _ = service.load("P/A")
    assert (state.workflow_runtime, state.model, state.reasoning) == expected


@pytest.mark.parametrize(
    "settings",
    [{"model": ""}, {"reasoning": " "}, {"workflow_runtime": "unsupported"}],
)
def test_invalid_settings_do_not_begin_launch(
    tmp_path: Path, settings: dict[str, str]
) -> None:
    service = _parent(tmp_path)
    with pytest.raises((StateError, ConfigurationError)):
        service.start_child("P", "A", **settings)
    child = service.tasks.read_children("P", "01-parent")[0]
    assert child.status == "pending"
    assert child.model is None
    assert not service.tasks.execution_runs("P/A")


def test_interrupted_launch_freezes_settings_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _parent(tmp_path)
    original = service.children.start_run

    def interrupted(*args: object, **kwargs: object) -> None:
        raise OSError("launch interrupted")

    monkeypatch.setattr(service.children, "start_run", interrupted)
    with pytest.raises(OSError, match="launch interrupted"):
        service.start_child(
            "P", "A", workflow_runtime="single", model="luna", reasoning="high"
        )
    with pytest.raises(StateError, match="launch settings cannot change"):
        service.start_child("P", "A", model="different")
    monkeypatch.setattr(service.children, "start_run", original)
    # Reload persisted state and retry without repeating the flags.
    service = WorkflowService(Storage(tmp_path))
    service.start_child("P", "A")
    state, _ = service.load("P/A")
    assert (state.workflow_runtime, state.model, state.reasoning) == (
        "single",
        "luna",
        "high",
    )
    with pytest.raises(StateError, match="not pending"):
        service.start_child("P", "A", workflow_runtime="auto")


def test_retry_after_child_publication_keeps_the_same_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _parent(tmp_path)
    original = service.children._publish_child

    def interrupted(*args: object, **kwargs: object) -> None:
        raise OSError("publication interrupted")

    monkeypatch.setattr(service.children, "_publish_child", interrupted)
    with pytest.raises(OSError, match="publication interrupted"):
        service.start_child(
            "P", "A", workflow_runtime="single", model="luna", reasoning="high"
        )
    assert len(service.tasks.execution_runs("P/A")) == 1
    monkeypatch.setattr(service.children, "_publish_child", original)
    service.start_child("P", "A")
    assert len(service.tasks.execution_runs("P/A")) == 1
    child = service.tasks.read_children("P", "01-parent")[0]
    assert (child.status, child.workflow_runtime, child.model, child.reasoning) == (
        "in_progress",
        "single",
        "luna",
        "high",
    )


def test_identity_request_and_bound_run_keep_child_settings(tmp_path: Path) -> None:
    service = _parent(tmp_path, identity=True)
    child = service.tasks.read_children("P", "01-parent")[0]
    request = service.start_child(
        "P", child.id, workflow_runtime="single", model="luna", reasoning="high"
    )
    assert request.workflow_runtime == "single"
    service = WorkflowService(Storage(tmp_path))
    repeated = service.start_child("P", child.id)
    assert repeated.task_id == request.task_id
    service.next(child.id)
    service.complete(
        child.id,
        (("task_id", "EXT-1"),),
        artifact="Created EXT-1.",
        summary_for_next="Ready.",
    )
    state, _ = service.load("P/EXT-1")
    assert (state.workflow_runtime, state.model, state.reasoning) == (
        "single",
        "luna",
        "high",
    )
