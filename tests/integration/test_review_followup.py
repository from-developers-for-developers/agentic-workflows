# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression coverage for the bounded September 2026 follow-up review."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.workflow_helpers import (
    advance_init,
    assignment_token,
    start_after_init,
    start_child_after_init,
)
from ww.cli import main
from ww.errors import StateError
from ww.items import WorkItem
from ww.service import WorkflowService
from ww.storage import Storage
from ww.storage_adapters import MemoryTaskStorageAdapter, ProjectMetadata


def _service(root: Path, configuration: str) -> WorkflowService:
    (root / "ww-agentic-workflows.yaml").write_text(configuration, encoding="utf-8")
    return WorkflowService(Storage(root))


def test_init_named_preparation_hook_runs_before_implicit_init(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        """handlers:
  - name: init
    description: Approve the requirements before initialization.
workflows:
  - name: task
    hooks:
      before_in_progress:
        - steps: [init]
          name: init
    steps:
      - name: work
        artifact: false
""",
    )

    service.start("task", "TASK-INIT", init_artifact="requirements")
    initial = service.tasks.read_execution_state("TASK-INIT", "01-task")
    assert initial is not None
    assert initial.item_executions[0].status == "pending"
    assert initial.item_executions[1].status == "pending"
    assert initial.pending_init_artifact == "requirements"

    service.next("TASK-INIT")
    service.complete("TASK-INIT", summary_for_next="Done.")
    completed = service.tasks.read_execution_state("TASK-INIT", "01-task")
    assert completed is not None
    assert completed.item_executions[1].status == "completed"
    assert completed.pending_init_artifact is None


def test_init_named_preparation_hook_keeps_manager_worker_handoff(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """handlers:
  - name: init
    description: Approve the requirements before initialization.
workflows:
  - name: task
    hooks:
      before_in_progress:
        - steps: [init]
          name: init
    steps:
      - name: work
        artifact: false
""",
    )

    paused = service.start("task", "TASK-ROLES", caller_role="manager")
    assert paused.next_role == "manager"
    assigned = service.next("TASK-ROLES", caller_role="manager")
    assert assigned.next_role == "worker"
    handoff = service.complete(
        "TASK-ROLES",
        caller_role="worker", assignment=assignment_token(service, "TASK-ROLES"),
        summary_for_next="Done.",
    )
    assert handoff.item_name == "work"

    state = service.tasks.read_execution_state("TASK-ROLES", "01-task")
    assert state is not None
    assert [record.status for record in state.item_executions[:2]] == [
        "completed",
        "completed",
    ]
    assert state.pending_init_artifact is None


@pytest.mark.parametrize(
    "existing, output",
    [("result.existing", "result"), ("result", "result.existing")],
)
def test_invalid_project_metadata_shape_does_not_complete_source_item(
    tmp_path: Path, existing: str, output: str
) -> None:
    service = _service(
        tmp_path,
        f"""workflows:
  - name: task
    steps:
      - name: capture
        artifact: false
        update_metadata:
          - name: value
            key: {output}
            scope: project
""",
    )
    service.project_metadata_store.write_project_metadata(
        ProjectMetadata(((existing, "old"),))
    )
    service.start("task", "TASK-METADATA", init_artifact="requirements")
    service.next("TASK-METADATA")

    with pytest.raises(StateError, match="conflicting project metadata key"):
        service.complete(
            "TASK-METADATA",
            metadata_values=(("value", "new"),),
            summary_for_next="Done.",
        )

    state = service.tasks.read_execution_state("TASK-METADATA", "01-task")
    assert state is not None
    assert state.item_executions[1].status == "in_progress"
    assert state.pending_project_metadata is None


def test_concurrent_project_metadata_shape_conflict_can_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: capture
        artifact: false
        update_metadata:
          - name: value
            key: result
            scope: project
""",
    )
    service.start("task", "TASK-CONCURRENT", init_artifact="requirements")
    service.next("TASK-CONCURRENT")
    original_commit = service.commit

    def commit_with_concurrent_shape(*args: object, **kwargs: object) -> None:
        state = args[0]
        if getattr(state, "pending_project_metadata", None) is not None:
            service.project_metadata_store.write_project_metadata(
                ProjectMetadata((("result.existing", "other"),))
            )
        original_commit(*args, **kwargs)

    monkeypatch.setattr(service, "commit", commit_with_concurrent_shape)
    with pytest.raises(StateError, match="incompatible metadata shape"):
        service.complete(
            "TASK-CONCURRENT",
            metadata_values=(("value", "new"),),
            summary_for_next="Done.",
        )

    state = service.tasks.read_execution_state("TASK-CONCURRENT", "01-task")
    assert state is not None
    assert state.item_executions[1].status == "completed"
    assert state.pending_project_metadata is not None

    monkeypatch.setattr(service, "commit", original_commit)
    service.project_metadata_store.write_project_metadata(ProjectMetadata())
    service.next("TASK-CONCURRENT")
    assert service.project_metadata() == {"result": "new"}


@pytest.mark.parametrize("memory", (False, True))
def test_continue_and_retry_preserve_command_output_history(
    tmp_path: Path, memory: bool
) -> None:
    config = """workflows:
  - name: task
    steps:
      - name: cycle
        loop:
          - name: review
            continue: Again.
            hooks:
              before_in_progress:
                - name: evidence
                  command:
                    shell: 'printf %s "$WW_OPERATION_ATTEMPT"; exit 1'
"""
    (tmp_path / "ww-agentic-workflows.yaml").write_text(config, encoding="utf-8")
    service = WorkflowService(
        Storage(
            tmp_path,
            task_persistence=MemoryTaskStorageAdapter() if memory else None,
        )
    )
    service.start("task", "TASK-HISTORY", init_artifact="requirements")
    assert service.next("TASK-HISTORY").status == "failed"
    failed = service.tasks.read_execution_state("TASK-HISTORY", "01-task")
    assert failed is not None
    first_reference = next(
        command.stdout_ref
        for record in failed.item_executions
        for command in record.commands
        if command.stdout_ref is not None
    )

    assert service.next("TASK-HISTORY").status == "failed"
    retried = service.tasks.read_execution_state("TASK-HISTORY", "01-task")
    assert retried is not None
    assert any(
        command.stdout_ref == first_reference
        for record in retried.execution_history
        for command in record.commands
    )
    assert any(
        artifact.get("command_output") == first_reference
        for artifact in service.artifacts("TASK-HISTORY")
    )


@pytest.mark.parametrize("memory", (False, True))
def test_continue_preserves_command_output_history(
    tmp_path: Path, memory: bool
) -> None:
    config = """workflows:
  - name: task
    steps:
      - name: cycle
        loop:
          - name: review
            continue: Again.
            hooks:
              before_in_progress:
                - name: evidence
                  command:
                    argv: [printf, evidence]
"""
    (tmp_path / "ww-agentic-workflows.yaml").write_text(config, encoding="utf-8")
    service = WorkflowService(
        Storage(
            tmp_path,
            task_persistence=MemoryTaskStorageAdapter() if memory else None,
        )
    )
    service.start("task", "TASK-CONTINUE", init_artifact="requirements")
    service.next("TASK-CONTINUE")
    first = service.tasks.read_execution_state("TASK-CONTINUE", "01-task")
    assert first is not None
    first_reference = next(
        command.stdout_ref
        for record in first.item_executions
        for command in record.commands
        if command.stdout_ref is not None
    )

    service.loop(
        "TASK-CONTINUE",
        artifact="again",
        continue_loop=True,
        summary_for_next="Done.",
    )
    continued = service.tasks.read_execution_state("TASK-CONTINUE", "01-task")
    assert continued is not None
    assert any(
        command.stdout_ref == first_reference
        for record in continued.execution_history
        for command in record.commands
    )
    assert any(
        artifact.get("command_output") == first_reference
        for artifact in service.artifacts("TASK-CONTINUE")
    )


@pytest.mark.parametrize("outcome", ["completed", "failed"])
def test_parent_follows_child_handoff_until_successor_finishes(
    tmp_path: Path, outcome: str
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: parent
    steps:
      - name: split
        children: ~
      - name: execute
        workflow_per_child: choose
  - name: choose
    handoff: true
    steps:
      - name: select
        provide:
          - name: workflow
        hooks:
          after_complete:
            - workflow: "{{workflow}}"
  - name: work
    steps:
      - name: implement
        artifact: false
""",
    )
    start_after_init(service, "parent", "P")
    service.next("P")
    child = service.add_child("P", "C", "Implement the selected workflow")
    service.complete("P", summary_for_next="Done.")
    start_child_after_init(service, "P", "C")
    service.next("P/C")
    ready = advance_init(
        service,
        service.complete(
            "P/C",
            (("workflow", "work"),),
            artifact="Selected work",
            summary_for_next="Done.",
        ),
    )

    assert ready.workflow == "work"
    assert ready.item_name == "implement"
    selection = service.tasks.read_execution_state("P/C", "01-choose")
    successor = service.tasks.read_execution_state("P/C", "02-work")
    assert selection is not None and successor is not None
    assert successor.parent_task_id == "P"
    assert successor.start_operation_id == child.start_operation_id
    assert successor.execution_instance_id != selection.execution_instance_id
    waiting = service.next("P")
    assert waiting.action_kind == "child_workflow"
    assert waiting.child_tasks[0].status == "in_progress"
    assert waiting.child_tasks[0].run_id == "02-work"
    assert service.recover("P/C").item_name == "implement"

    service.next("P/C")
    if outcome == "failed":
        service.fail("P/C", "Implementation failed")
    else:
        service.complete("P/C", summary_for_next="Done.")
        service.next("P/C")
        # Exercise terminal recovery after the successor has published its result.
        with patch.object(service.children, "reconcile_after_child"):
            service.complete(
                "P/C",
                (("summary", "Implementation done"),),
                summary_for_next="Done.",
            )
        service.next("P")
        assert service.recover("P/C").status == "completed"

    assert service.status("P").status == outcome
    recorded = service.tasks.read_children("P", "01-parent")[0]
    assert recorded.run_id == "02-work"
    assert recorded.status == outcome
    assert recorded.summary == (
        "Implementation done" if outcome == "completed" else None
    )


def test_reset_creates_a_new_operation_identity_for_the_same_run_name(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: work
        artifact: false
""",
    )

    start_after_init(service, "task", "TASK-1", agent="codex")
    first = service.tasks.read_execution_state("TASK-1", "01-task")
    assert first is not None

    service.reset("TASK-1")
    start_after_init(service, "task", "TASK-1", agent="codex")
    second = service.tasks.read_execution_state("TASK-1", "01-task")
    assert second is not None

    assert first.execution_instance_id
    assert second.execution_instance_id
    assert first.execution_instance_id != second.execution_instance_id
    first_operation = first.item_executions[0].operation_id
    second_operation = second.item_executions[0].operation_id
    assert first_operation != second_operation


def test_reset_does_not_recover_a_git_effect_from_the_previous_execution(
    tmp_path: Path,
) -> None:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ("git", *arguments),
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "ww test")
    git("config", "user.email", "ww@example.test")
    (tmp_path / ".gitignore").write_text(".ww/\ntasks/\n", encoding="utf-8")
    service = _service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: work
        artifact: false
        hooks:
          after_complete:
            - name: ext/ww/git/handlers:git-commit
""",
    )
    git("add", ".")
    git("commit", "-qm", "seed")

    start_after_init(service, "task", "T", agent="codex")
    service.next("T")
    (tmp_path / "work.txt").write_text("first", encoding="utf-8")
    service.complete("T", (("commit_message", "first"),), summary_for_next="Done.")
    first_head = git("rev-parse", "HEAD")

    service.reset("T")
    start_after_init(service, "task", "T", agent="codex")
    service.next("T")
    (tmp_path / "work.txt").write_text("second", encoding="utf-8")
    service.complete("T", (("commit_message", "second"),), summary_for_next="Done.")

    assert git("rev-parse", "HEAD") != first_head
    assert git("show", "HEAD:work.txt") == "second"
    assert git("status", "--porcelain") == ""


def test_parent_status_repairs_a_missed_terminal_child_notification(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: parent
    steps:
      - name: split
        children: ~
      - name: execute
        workflow_per_child: child
  - name: child
    steps:
      - name: work
        artifact: false
""",
    )
    start_after_init(service, "parent", "P", agent="codex")
    service.next("P")
    service.add_child("P", "C", "child")
    service.complete("P", summary_for_next="Done.")
    start_child_after_init(service, "P", "C")
    service.next("P/C")
    service.complete("P/C", summary_for_next="Done.")
    service.next("P/C")

    with patch.object(
        service.children,
        "reconcile_after_child",
        side_effect=OSError("injected death after child publication"),
    ), pytest.raises(OSError, match="injected death"):
        service.complete("P/C", (("summary", "done"),), summary_for_next="Done.")

    assert service.status("P/C").status == "completed"
    assert service.tasks.read_children("P", "01-parent")[0].status == "in_progress"

    resumed = service.status("P")

    assert resumed.status == "completed"
    assert service.tasks.read_children("P", "01-parent")[0].status == "completed"


def test_child_recover_repeats_a_missed_terminal_parent_notification(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: parent
    steps:
      - name: split
        children: ~
      - name: execute
        workflow_per_child: child
  - name: child
    steps:
      - name: work
        artifact: false
""",
    )
    start_after_init(service, "parent", "P", agent="codex")
    service.next("P")
    service.add_child("P", "C", "child")
    service.complete("P", summary_for_next="Done.")
    start_child_after_init(service, "P", "C")
    service.next("P/C")
    service.complete("P/C", summary_for_next="Done.")
    service.next("P/C")
    with (
        patch.object(service.children, "reconcile_after_child", side_effect=OSError),
        pytest.raises(OSError),
    ):
        service.complete("P/C", (("summary", "done"),), summary_for_next="Done.")

    assert service.recover("P/C").status == "completed"
    assert service.status("P").status == "completed"
    assert service.recover("P/C").status == "completed"


def test_parent_refresh_ignores_a_child_run_from_an_older_parent_execution(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: parent
    steps:
      - name: split
        children: ~
      - name: execute
        workflow_per_child: child
  - name: child
    steps:
      - name: work
        artifact: false
""",
    )
    start_after_init(service, "parent", "P", agent="codex")
    service.next("P")
    service.add_child("P", "C", "first execution")
    service.complete("P", summary_for_next="Done.")
    start_child_after_init(service, "P", "C")
    service.next("P/C")
    service.complete("P/C", summary_for_next="Done.")
    service.next("P/C")
    service.complete("P/C", (("summary", "old result"),), summary_for_next="Done.")

    start_after_init(service, "parent", "P", agent="codex")
    service.next("P")
    service.add_child("P", "C", "second execution")
    service.complete("P", summary_for_next="Done.")

    waiting = service.next("P")

    assert waiting.action_kind == "child_workflow"
    assert service.tasks.read_children("P", "02-parent")[0].status == "pending"
    start_child_after_init(service, "P", "C")
    assert [run.run_id for run in service.tasks.execution_runs("P/C")] == [
        "01-child",
        "02-child",
    ]


def test_parent_refresh_does_not_retry_a_hook_that_just_failed(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: parent
    steps:
      - name: split
        children: ~
      - name: execute
        workflow_per_child: child
        hooks:
          after_complete:
            - command:
                argv: ["false"]
  - name: child
    steps:
      - name: work
        artifact: false
""",
    )
    start_after_init(service, "parent", "P", agent="codex")
    service.next("P")
    service.add_child("P", "C", "child")
    service.complete("P", summary_for_next="Done.")
    start_child_after_init(service, "P", "C")
    service.next("P/C")
    service.complete("P/C", summary_for_next="Done.")
    service.next("P/C")
    with patch.object(service.children, "reconcile_after_child"):
        service.complete("P/C", (("summary", "done"),), summary_for_next="Done.")

    result = service.next("P")

    assert result.status == "failed"
    state = service.tasks.read_execution_state("P", "01-parent")
    assert state is not None
    assert state.item_executions[state.cursor].commands[0].attempts == 1


def test_numeric_task_generation_continues_past_fifty(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.json").write_text(
        '{"task_format": "TASK-{digit}"}', encoding="utf-8"
    )
    service = _service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: work
        artifact: false
  - name: parent
    steps:
      - name: split
        children: ~
""",
    )

    for index in range(1, 52):
        instruction = start_after_init(service, "task", None, agent="codex")
        assert instruction.task_id == f"TASK-{index}"

    start_after_init(service, "parent", "P", agent="codex")
    service.next("P")
    for index in range(1, 52):
        child = service.add_child("P", None, f"child {index}")
        assert child.id == f"TASK-{index}"


def test_status_uses_one_aggregate_revision(tmp_path: Path) -> None:
    writer = _service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: collect
        items:
          steps:
            - name: process
              process_item: ~
""",
    )
    start_after_init(writer, "task", "T", agent="codex")
    writer.next("T")
    writer.add_item("T", WorkItem("one", "one"))
    reader = WorkflowService(Storage(tmp_path))
    original_read = reader.tasks.read_task_record

    def read_then_publish(task_id: str):  # type: ignore[no-untyped-def]
        record = original_read(task_id)
        writer.complete("T", summary_for_next="Done.")
        return record

    with patch.object(reader.tasks, "read_task_record", read_then_publish):
        instruction = reader.status("T")

    assert instruction.item_name == "collect"
    assert reader.status("T").item_name == "process"


def test_process_creation_failure_is_a_retryable_cli_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _service(
        tmp_path,
        """workflows:
  - name: task
    hooks:
      before_start_workflow:
        - command:
            argv: [/nonexistent-ww-review-command]
    steps:
      - name: work
""",
    )

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "start",
            "T",
            "--workflow",
            "task",
            "--agent",
            "codex",
            "--init-artifact",
            "requirements",
        ]
    )

    assert exit_code == 1
    assert "could not launch command 1" in capsys.readouterr().out
    service = WorkflowService(Storage(tmp_path))
    state = service.tasks.read_execution_state("T", "01-task")
    assert state is not None
    assert state.status == "failed"
    assert state.item_executions[0].commands[0].status == "failed"
    assert service.next("T").status == "failed"
    retried = service.tasks.read_execution_state("T", "01-task")
    assert retried is not None
    assert retried.item_executions[0].commands[0].attempts == 2


def test_error_after_process_creation_keeps_the_outcome_unknown(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: task
    hooks:
      before_start_workflow:
        - command:
            argv: [touch, effect.txt]
    steps:
      - name: work
""",
    )
    communicate = subprocess.Popen.communicate

    def fail_after_communicate(
        process: subprocess.Popen[str], *args: object, **kwargs: object
    ) -> tuple[str, str]:
        communicate(process, *args, **kwargs)
        raise OSError("injected output collection failure")

    with (
        patch.object(subprocess.Popen, "communicate", fail_after_communicate),
        pytest.raises(OSError, match="output collection failure"),
    ):
        start_after_init(service, "task", "T", agent="codex")

    assert (tmp_path / "effect.txt").exists()
    state = service.tasks.read_execution_state("T", "01-task")
    assert state is not None
    assert state.item_executions[0].commands[0].status == "in_progress"
    recovered = service.next("T")
    assert recovered.status == "interrupted"
    assert recovered.operation_id == state.item_executions[0].operation_id


def test_start_retains_requirements_through_init_preparation_and_stops_before_work(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """handlers:
  - name: approve
    description: Approve requirements.
  - name: prepare
    provide:
      - name: note
    command:
      argv: [printf, "{{note}}"]
workflows:
  - name: task
    hooks:
      before_in_progress:
        - steps: [init]
          name: approve
    steps:
      - name: work
        hooks:
          before_in_progress:
            - name: prepare
""",
    )

    approval = service.start("task", "T", init_artifact="Requirements.")
    assert approval.item_name == "approve"
    service.next("T")
    ready = service.complete("T", artifact="approved", summary_for_next="Done.")
    assert ready.item_name == "prepare"
    state = service.tasks.read_execution_state("T", "01-task")
    assert state is not None
    assert state.item_executions[1].artifact is not None
    assert state.status == "pending"


def test_continue_runs_completion_hook_then_blocks_at_loop_limit(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """handlers:
  - name: audit
    command:
      argv: [touch, audit.txt]
workflows:
  - name: task
    steps:
      - name: cycle
        loop_max_times: 1
        loop:
          - name: review
            continue: Review again.
            hooks:
              after_complete:
                - name: audit
""",
    )

    service.start("task", "T", init_artifact="Requirements.")
    service.next("T")
    blocked = service.loop(
        "T",
        artifact="again",
        continue_loop=True,
        summary_for_next="Done.",
    )
    assert (tmp_path / "audit.txt").exists()
    assert blocked.loop_limit_reached is True
    assert blocked.control == "awaiting_operator"
    assert blocked.operator_reason == "loop_limit"
    state = service.tasks.read_execution_state("T", "01-task")
    assert state is not None
    assert dict(state.loop_iterations) == {"cycle": 1}


def test_parent_reset_rejects_existing_child_without_losing_child_artifact(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: work
""",
    )
    service.start("task", "P", init_artifact="parent")
    service.start("task", "P/C", init_artifact="child")
    child = service.tasks.read_execution_state("P/C", "01-task")
    assert child is not None and child.item_executions[0].artifact is not None

    with pytest.raises(StateError, match="child task"):
        service.reset("P")

    assert service.tasks.read_execution_artifact(child.item_executions[0].artifact)
