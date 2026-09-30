# SPDX-License-Identifier: GPL-3.0-or-later
"""One-level parent/child task orchestration."""

from dataclasses import replace
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.errors import ConfigurationError, StateError
from ww.service import WorkflowService
from ww.storage import Storage


def test_last_child_completion_drains_the_parent_hooks(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
        hooks:
          after_complete:
            - command:
                argv: [touch, parent-finished.txt]
  - name: child
    steps:
      - name: work
        description: Do child work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "parent", "TASK1", agent="codex")
    collect = service.next("TASK1")
    assert (
        './ww add-child TASK1 --id <child-id> --description="<child task description>"'
        in (collect.action_text or "")
    )
    child = service.add_child("TASK1", "TASK1.1", "Implement child work")
    assert child.task_id == "TASK1/TASK1.1"
    ready = service.complete(
        "TASK1",
        artifact="children collected",
        summary_for_next="Done.",
    )
    assert ready.item_name == "children"
    assert not (tmp_path / "parent-finished.txt").exists()

    waiting = service.next("TASK1")
    assert waiting.action_kind == "child_workflow"
    started = service.start_child("TASK1", "TASK1.1")
    assert started.task_id == "TASK1/TASK1.1"
    service.next("TASK1/TASK1.1")
    ready = service.complete(
        "TASK1/TASK1.1",
        artifact="child work complete",
        summary_for_next="Done.",
    )
    assert ready.item_name == "update-workflow-summary"
    service.next("TASK1/TASK1.1")
    service.complete(
        "TASK1/TASK1.1",
        (("summary", "Child done."),),
        summary_for_next="Done.",
    )

    parent = service.status("TASK1")
    state = service.tasks.read_execution_state("TASK1", "01-parent")
    snapshot = service.tasks.read_plan_snapshot("TASK1", "01-parent")
    assert parent.status == "completed", snapshot.plan.items[state.cursor].kind
    assert (tmp_path / "parent-finished.txt").exists()
    assert (tmp_path / ".ww/tasks/TASK1/TASK1.1/metadata.json").exists()


def test_child_failure_marks_the_parent_coordinator_failed(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - name: work
        description: Do child work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "parent", "TASK1", agent="codex")
    service.next("TASK1")
    service.add_child("TASK1", "TASK1.1", "Implement child work")
    service.complete("TASK1", summary_for_next="Done.")
    service.start_child("TASK1", "TASK1.1")
    service.next("TASK1/TASK1.1")
    service.fail("TASK1/TASK1.1", "deliberately stopped")

    parent = service.status("TASK1")
    assert parent.status == "failed"
    assert parent.child_tasks[0].status == "failed"


def test_resumed_child_completion_recovers_its_failed_parent(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - name: work
        description: Do child work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "parent", "TASK1", agent="codex")
    service.next("TASK1")
    service.add_child("TASK1", "TASK1.1", "Implement child work")
    service.complete("TASK1", summary_for_next="Done.")
    service.start_child("TASK1", "TASK1.1")
    service.next("TASK1/TASK1.1")
    service.fail("TASK1/TASK1.1", "deliberately stopped")

    resumed = service.next("TASK1/TASK1.1")
    assert resumed.item_name == "work"
    service.complete(
        "TASK1/TASK1.1",
        artifact="child work complete",
        summary_for_next="Done.",
    )
    service.next("TASK1/TASK1.1")
    service.complete(
        "TASK1/TASK1.1",
        (("summary", "Child done."),),
        summary_for_next="Done.",
    )

    assert service.status("TASK1").status == "completed"


def test_recovered_child_leaves_parent_waiting_for_other_children(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - name: work
        description: Do child work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "parent", "TASK1", agent="codex")
    service.next("TASK1")
    service.add_child("TASK1", "1", "First child")
    service.add_child("TASK1", "2", "Second child")
    service.complete("TASK1", summary_for_next="Done.")
    service.start_child("TASK1", "2")
    service.next("TASK1/2")
    service.fail("TASK1/2", "deliberately stopped")

    service.next("TASK1/2")
    service.complete(
        "TASK1/2",
        artifact="child work complete",
        summary_for_next="Done.",
    )
    service.next("TASK1/2")
    service.complete(
        "TASK1/2",
        (("summary", "Second child done."),),
        summary_for_next="Done.",
    )

    parent = service.status("TASK1")
    assert parent.status == "in_progress"
    assert parent.error is None
    assert parent.action_text is not None
    assert "./ww child start TASK1 1" in parent.action_text


def test_child_start_retries_after_parent_binding_was_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - name: work
        description: Do child work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "parent", "TASK1", agent="codex")
    service.next("TASK1")
    service.add_child("TASK1", "TASK1.1", "Implement child work")
    service.complete("TASK1", summary_for_next="Done.")
    service.next("TASK1")

    original = service.children.start_run

    def fail_child_start(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise OSError("injected child start failure")

    monkeypatch.setattr(service.children, "start_run", fail_child_start)
    with pytest.raises(OSError, match="injected child start failure"):
        service.start_child("TASK1", "TASK1.1")
    assert service.status("TASK1").child_tasks[0].status == "starting"

    monkeypatch.setattr(service.children, "start_run", original)
    started = service.start_child("TASK1", "TASK1.1")
    assert started.task_id == "TASK1/TASK1.1"
    assert service.status("TASK1").child_tasks[0].status == "in_progress"


def test_child_start_does_not_overwrite_terminal_child_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - name: work
        description: Do child work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "parent", "TASK1", agent="codex")
    service.next("TASK1")
    service.add_child("TASK1", "TASK1.1", "Implement child work")
    service.complete("TASK1", summary_for_next="Done.")
    service.next("TASK1")

    original = service.children.start_run

    def start_and_finish_child(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        result = original(*args, **kwargs)
        child_state, child_snapshot = service.load("TASK1/TASK1.1")
        records = tuple(
            replace(record, status="completed", completed_at=child_state.updated_at)
            for record in child_state.item_executions
        )
        child_state = replace(
            child_state,
            status="completed",
            active_item_id=None,
            cursor=len(records),
            item_executions=records,
        )
        service.commit(
            service._with_steps(child_state, child_snapshot.plan), child_snapshot
        )
        return result

    monkeypatch.setattr(service.children, "start_run", start_and_finish_child)
    service.start_child("TASK1", "TASK1.1")

    parent = service.status("TASK1")
    assert parent.status == "completed"
    children = service.tasks.read_children("TASK1", "01-parent")
    assert children[0].status == "completed"


def test_child_start_reconciles_published_child_after_parent_relink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - name: work
        description: Do child work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "parent", "TASK1", agent="codex")
    service.next("TASK1")
    service.add_child("TASK1", "TASK1.1", "Implement child work")
    service.complete("TASK1", summary_for_next="Done.")
    service.next("TASK1")

    original = service.commit

    def fail_parent_relink(state, snapshot, **kwargs):  # type: ignore[no-untyped-def]
        children = kwargs.get("children")
        if (
            state.task_id == "TASK1"
            and children
            and children[0].status == "in_progress"
        ):
            raise OSError("injected parent relink failure")
        return original(state, snapshot, **kwargs)

    monkeypatch.setattr(service, "commit", fail_parent_relink)
    with pytest.raises(OSError, match="injected parent relink failure"):
        service.start_child("TASK1", "TASK1.1")
    assert service.tasks.execution_runs("TASK1/TASK1.1")
    assert service.tasks.read_children("TASK1", "01-parent")[0].status == "starting"

    monkeypatch.setattr(service, "commit", original)
    resumed = service.start_child("TASK1", "TASK1.1")
    assert resumed.task_id == "TASK1/TASK1.1"
    child = service.tasks.read_children("TASK1", "01-parent")[0]
    assert child.status == "in_progress"
    assert child.run_id == "01-child"


def test_child_workflows_are_limited_to_one_level(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - name: split-again
        children:
          workflow: grandchild
  - name: grandchild
    steps:
      - name: work
        description: Do work.
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="recursive child tasks"):
        WorkflowService(Storage(tmp_path)).start("parent", "TASK1", agent="codex")


def test_children_step_requires_at_least_one_child(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - name: work
        description: Do child work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "parent", "TASK1", agent="codex")
    service.next("TASK1")
    with pytest.raises(StateError, match="without recorded children"):
        service.complete("TASK1", summary_for_next="Done.")


def test_child_ids_default_to_the_standard_generation_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - name: work
        description: Do child work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    monkeypatch.setattr(
        "ww.task_ids.generated_task_id", lambda task_format=None: "TASK-20000101000000"
    )

    start_after_init(service, "parent", "TASK1", agent="codex")
    service.next("TASK1")
    first = service.add_child("TASK1", None, "First child")
    second = service.add_child("TASK1", None, "Second child")

    assert first.id == "TASK-20000101000000"
    assert first.task_id == "TASK1/TASK-20000101000000"
    assert second.id == "TASK-20000101000000-2"


_PARENT_AND_CHILD = """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - name: work
        description: Do child work.
"""


def test_update_child_edits_a_pending_child_until_it_starts(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        _PARENT_AND_CHILD, encoding="utf-8"
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "parent", "TASK1", agent="codex")
    service.next("TASK1")
    service.add_child("TASK1", "1", "First draft")
    service.add_child("TASK1", "2", "Second child")

    # While the children step collects.
    updated = service.update_child("TASK1", "1", text="First, refined")
    assert updated.description == "First, refined"
    service.complete("TASK1", artifact="split", summary_for_next="Done.")

    # While the parent waits at the run leaf.
    waiting = service.next("TASK1")
    assert "./ww update-child TASK1 1" in (waiting.action_text or "")
    service.update_child("TASK1", "1", text="First, final")
    started = service.start_child("TASK1", "1")
    assert started.task_id == "TASK1/1"
    # The refined text became the child's recorded init requirements.
    assert service.next("TASK1/1").task_requirements == (
        "Requirements for child task 1: First, final"
    )

    with pytest.raises(StateError, match="child '1' is in_progress; only a pending"):
        service.update_child("TASK1", "1", text="Too late")
    assert service.update_child("TASK1", "2", text="Still pending").status == (
        "pending"
    )


def test_update_child_rejects_bad_requests(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        _PARENT_AND_CHILD, encoding="utf-8"
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "parent", "TASK1", agent="codex")
    service.next("TASK1")
    service.add_child("TASK1", "1", "First child")

    with pytest.raises(StateError, match="needs --text, --project, or both"):
        service.update_child("TASK1", "1")
    with pytest.raises(StateError, match="child text must be non-empty"):
        service.update_child("TASK1", "1", text="  ")
    with pytest.raises(StateError, match="child 'nope' was not found"):
        service.update_child("TASK1", "nope", text="Text")
    with pytest.raises(StateError, match="unknown project"):
        service.update_child("TASK1", "1", project="nowhere")
