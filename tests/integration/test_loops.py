# SPDX-License-Identifier: GPL-3.0-or-later
"""Worker-controlled repeated execution of ordinary nested steps."""

from pathlib import Path

import pytest

from tests.workflow_helpers import (
    assignment_token,
    configured_service,
    start_after_init,
)
from ww.cli import main
from ww.errors import StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage
from ww.storage_adapters import MemoryTaskStorageAdapter


def _service(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        loop:
          - review: Review the implementation.
            break: There are no meaningful findings.
          - fix: Fix the review findings.
""",
        encoding="utf-8",
    )
    return WorkflowService(Storage(tmp_path))


def _complete_iteration(service: WorkflowService, task_id: str) -> None:
    active = service.next(task_id, caller_role="manager")
    assert active.item_name == "review"
    assert active.loop_break_prompt == "There are no meaningful findings."
    assert f"./ww loop {task_id} --break --role worker" in (
        active.loop_break_command or ""
    )
    ready = service.complete(
        task_id,
        artifact="findings",
        caller_role="worker", assignment=assignment_token(service, task_id),
        summary_for_next="Done.",
    )
    assert ready.item_name == "fix"
    service.next(task_id, caller_role="manager")
    repeat = service.complete(
        task_id,
        artifact="fixed",
        caller_role="worker", assignment=assignment_token(service, task_id),
        summary_for_next="Done.",
    )
    assert repeat.action_kind == "loop"


def test_loop_repeats_automatically_until_worker_stops(tmp_path: Path) -> None:
    service = _service(tmp_path)
    entry = start_after_init(
        service, "task", "TASK-LOOP", agent="codex", caller_role="manager"
    )

    assert entry.action_kind == "loop"
    assert "Dispatch the first loop step" in (entry.action_text or "")
    assert "remains active until a child step explicitly breaks it" in (
        entry.action_text or ""
    )
    _complete_iteration(service, "TASK-LOOP")

    first = service.tasks.read_execution_state("TASK-LOOP", "01-task")
    assert first is not None
    first_review_operation = first.item_executions[2].operation_id
    repeated = service.next("TASK-LOOP", caller_role="manager")
    assert repeated.item_name == "review"
    current = service.tasks.read_execution_state("TASK-LOOP", "01-task")
    assert current is not None
    assert dict(current.loop_iterations) == {"review-and-fix": 2}

    second = service.tasks.read_execution_state("TASK-LOOP", "01-task")
    assert second is not None
    assert second.item_executions[2].operation_id != first_review_operation
    stopped = service.loop(
        "TASK-LOOP",
        artifact="clean review",
        caller_role="worker", assignment=assignment_token(service, "TASK-LOOP"),
        summary_for_next="Done.",
    )
    assert stopped.item_name == "update-workflow-summary"

    artifact_root = (
        tmp_path / ".ww/tasks/TASK-LOOP/runs/01-task/steps/02-review-and-fix"
    )
    wrapper_artifact = artifact_root.with_suffix(".md")
    assert wrapper_artifact.read_text().endswith("clean review\n")
    assert (
        (artifact_root / "iteration-01/01-review.md").read_text().endswith("findings\n")
    )
    assert (artifact_root / "iteration-01/02-fix.md").read_text().endswith("fixed\n")
    assert (
        (artifact_root / "iteration-02/01-review.md")
        .read_text()
        .endswith("clean review\n")
    )
    artifacts = service.artifacts("TASK-LOOP")
    assert {
        "step": "review-and-fix",
        "artifact": str(wrapper_artifact.relative_to(tmp_path)),
        "path": str(wrapper_artifact),
    } in artifacts


@pytest.mark.parametrize("in_memory", (False, True))
def test_loop_command_output_keeps_each_iteration_evidence(
    tmp_path: Path, in_memory: bool
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """hooks:
  before_complete:
    - steps: [review]
      name: record-operation
      shell: 'printf %s "$WW_ITEM_OPERATION_ID"'
workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        loop:
          - review: Review the implementation.
            break: There are no meaningful findings.
          - fix: Fix the review findings.
""",
        encoding="utf-8",
    )
    service = WorkflowService(
        Storage(
            tmp_path,
            task_persistence=MemoryTaskStorageAdapter() if in_memory else None,
        )
    )
    start_after_init(service, "task", "TASK-LOOP-OUTPUT", agent="codex")

    service.next("TASK-LOOP-OUTPUT")
    service.complete(
        "TASK-LOOP-OUTPUT",
        artifact="first findings",
        summary_for_next="Done.",
    )
    ready = service.next("TASK-LOOP-OUTPUT")
    assert ready.item_name == "fix"
    snapshot = service.tasks.read_plan_snapshot("TASK-LOOP-OUTPUT", "01-task")
    first = service.tasks.read_execution_state("TASK-LOOP-OUTPUT", "01-task")
    assert snapshot is not None and first is not None
    hook_index = next(
        index
        for index, item in enumerate(snapshot.plan.items)
        if item.name == "record-operation"
    )
    first_command = first.item_executions[hook_index].commands[0]
    assert first_command.stdout_ref is not None
    first_reference = first_command.stdout_ref
    first_output = service.tasks.read_command_output(first_reference)

    service.complete("TASK-LOOP-OUTPUT", artifact="first fix", summary_for_next="Done.")
    repeated = service.next("TASK-LOOP-OUTPUT")
    assert repeated.item_name == "review"
    service.complete(
        "TASK-LOOP-OUTPUT",
        artifact="second findings",
        summary_for_next="Done.",
    )
    ready = service.next("TASK-LOOP-OUTPUT")
    assert ready.item_name == "fix"
    second = service.tasks.read_execution_state("TASK-LOOP-OUTPUT", "01-task")
    assert second is not None
    second_command = second.item_executions[hook_index].commands[0]
    assert second_command.stdout_ref is not None
    assert second_command.stdout_ref != first_reference
    assert service.tasks.read_command_output(first_reference) == first_output
    assert service.tasks.read_command_output(second_command.stdout_ref) != first_output
    assert any(
        command.stdout_ref == first_reference
        for record in second.execution_history
        for command in record.commands
    )
    assert any(
        artifact.get("command_output") == first_reference
        for artifact in service.artifacts("TASK-LOOP-OUTPUT")
    )


def test_loop_wrapper_artifact_can_be_disabled(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - task: ~
    steps:
      - review: ~
        artifact: false
        loop:
          - check: Check the result.
            break: The result is acceptable.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-NO-WRAPPER", agent="codex")
    service.next("TASK-NO-WRAPPER")

    service.loop(
        "TASK-NO-WRAPPER",
        artifact="accepted",
        caller_role="worker", assignment=assignment_token(service, "TASK-NO-WRAPPER"),
        summary_for_next="Done.",
    )

    artifact_root = tmp_path / ".ww/tasks/TASK-NO-WRAPPER/runs/01-task/steps"
    assert not (artifact_root / "02-review.md").exists()
    assert (
        (artifact_root / "02-review/iteration-01/01-check.md")
        .read_text()
        .endswith("accepted\n")
    )


def test_loop_stop_requires_wrapper_artifact_when_body_artifact_is_disabled(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - task: ~
    steps:
      - review: ~
        loop:
          - check: Check the result.
            artifact: false
            break: The result is acceptable.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    active = start_after_init(service, "task", "TASK-WRAPPER-ONLY", agent="codex")
    assert active.action_kind == "loop"
    active = service.next("TASK-WRAPPER-ONLY")
    assert '--artifact="<whole result in Markdown>"' in (
        active.loop_break_command or ""
    )

    service.loop(
        "TASK-WRAPPER-ONLY",
        artifact="accepted",
        caller_role="worker", assignment=assignment_token(service, "TASK-WRAPPER-ONLY"),
        summary_for_next="Done.",
    )

    artifact_root = tmp_path / ".ww/tasks/TASK-WRAPPER-ONLY/runs/01-task/steps"
    assert (artifact_root / "02-review.md").read_text().endswith("accepted\n")
    assert not (artifact_root / "02-review/iteration-01/01-check.md").exists()


def test_loop_stops_and_escalates_when_default_limit_is_reached(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    start_after_init(
        service, "task", "TASK-LIMIT", agent="codex", caller_role="manager"
    )

    _complete_iteration(service, "TASK-LIMIT")
    _complete_iteration(service, "TASK-LIMIT")
    _complete_iteration(service, "TASK-LIMIT")

    limited = service.status("TASK-LIMIT", caller_role="manager")
    rendered = MarkdownOutputAdapter().render_instruction(limited)
    state = service.tasks.read_execution_state("TASK-LIMIT", "01-task")

    assert state is not None
    assert dict(state.loop_iterations) == {"review-and-fix": 3}
    assert limited.loop_iteration == 3
    assert limited.max_rounds == 3
    assert limited.loop_limit_reached is True
    assert limited.control == "awaiting_operator"
    assert limited.next_role == "operator"
    assert limited.operator_reason == "loop_limit"
    assert limited.continuation_command is None
    assert (
        "## Operator decision: the loop reached its iteration limit" in rendered
    )
    assert "(`operator_reason: loop_limit`)" in rendered
    assert "### Loop limit reached" in rendered
    assert "Report the saved loop results and this warning to the user" in rendered
    assert "### Operator recovery" in rendered
    assert [command.action for command in limited.recovery_commands] == ["force"]
    assert './ww next TASK-LIMIT --force --reason "<reason>"' in rendered

    # Asking again never starts a fourth round; it repeats the escalation.
    again = service.next("TASK-LIMIT", caller_role="manager")
    assert again.loop_limit_reached is True
    assert again.continuation_command is None
    state = service.tasks.read_execution_state("TASK-LIMIT", "01-task")
    assert state is not None
    assert dict(state.loop_iterations) == {"review-and-fix": 3}
    assert service.force_target("TASK-LIMIT").startswith(
        "leave the `review-and-fix` loop at its limit of 3 iterations"
    )


def test_operator_force_leaves_a_loop_at_its_limit(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        max_rounds: 1
        loop:
          - review: Review the implementation.
            break: There are no meaningful findings.
          - fix: Fix the review findings.
      - finish: Wrap up.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-EXIT", agent="codex", caller_role="manager")

    # A running loop is not forceable: only a failed, interrupted, or
    # limit-stopped task is.
    with pytest.raises(StateError, match="not stopped at a loop limit"):
        service.force_target("TASK-EXIT")
    with pytest.raises(StateError, match="not stopped at a loop limit"):
        service.next("TASK-EXIT", force=True, force_reason="no", caller_role="manager")

    _complete_iteration(service, "TASK-EXIT")
    assert service.status("TASK-EXIT", caller_role="manager").loop_limit_reached

    after = service.next(
        "TASK-EXIT",
        force=True,
        force_reason="findings accepted by the operator",
        caller_role="manager",
    )
    assert after.item_name == "finish"
    assert after.loop_limit_reached is False
    state, snapshot = service.load("TASK-EXIT")
    repeat = next(
        index
        for index, item in enumerate(snapshot.plan.items)
        if item.id.endswith(":loop:repeat:1")
    )
    boundary = state.item_executions[repeat]
    assert boundary.status == "completed"
    assert "Force reason: findings accepted by the operator" in (boundary.error or "")
    assert dict(state.loop_iterations) == {"review-and-fix": 1}


def test_cli_force_is_checked_before_the_operator_is_asked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        max_rounds: 1
        loop:
          - review: Review the implementation.
            break: There are no meaningful findings.
      - finish: Wrap up.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-CLI", agent="codex")
    root = ["--root", str(tmp_path)]
    force = ["next", "TASK-CLI", "--force", "--reason", "accepted"]

    def refuse_prompt(_prompt: str) -> str:
        raise AssertionError("the operator must not be asked about a refused force")

    monkeypatch.setattr("builtins.input", refuse_prompt)
    assert main([*root, *force]) == 1
    assert "not stopped at a loop limit" in capsys.readouterr().err

    service.next("TASK-CLI")
    service.complete("TASK-CLI", artifact="findings", summary_for_next="Done.")
    assert service.status("TASK-CLI").loop_limit_reached is True
    prompts: list[str] = []

    def approve(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", approve)
    assert main([*root, *force]) == 0
    assert prompts == ["Proceed with force? [y/N] "]
    assert "leave the `review-and-fix` loop at its limit of 1 iterations" in (
        capsys.readouterr().err
    )
    assert service.status("TASK-CLI").item_name == "finish"


def test_loop_step_limit_overrides_global_project_limit(tmp_path: Path) -> None:
    (tmp_path / "ww.json").write_text(
        '{"limits": {"rounds": 9}, "extensions": {}}', encoding="utf-8"
    )
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        max_rounds: 1
        loop:
          - review: Review the implementation.
            break: There are no meaningful findings.
          - fix: Fix the review findings.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-OVERRIDE", agent="codex")

    _complete_iteration(service, "TASK-OVERRIDE")
    limited = service.status("TASK-OVERRIDE", caller_role="manager")

    assert limited.loop_iteration == 1
    assert limited.max_rounds == 1
    assert limited.loop_limit_reached is True


def test_only_stop_enabled_active_step_may_exit_loop(tmp_path: Path) -> None:
    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-NOT-GATE", agent="codex")
    service.next("TASK-NOT-GATE")
    service.complete("TASK-NOT-GATE", artifact="findings", summary_for_next="Done.")
    service.next("TASK-NOT-GATE")

    with pytest.raises(StateError, match="not permitted to break a loop"):
        service.loop(
            "TASK-NOT-GATE",
            artifact="fixed",
            caller_role="worker", assignment=assignment_token(service, "TASK-NOT-GATE"),
            summary_for_next="Done.",
        )


def test_wrapper_completion_hook_waits_for_an_explicit_stop(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """hooks:
  before_complete:
    - steps: [code-review]
      argv: [printf, committed]
workflows:
  - task: ~
    steps:
      - code-review: ~
        loop:
          - code-review: Review the implementation.
            break: There are no meaningful findings.
          - fix: Fix the review findings.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-WRAPPER-HOOK", agent="codex")
    service.next("TASK-WRAPPER-HOOK")

    ready = service.complete(
        "TASK-WRAPPER-HOOK",
        artifact="finding",
        caller_role="worker", assignment=assignment_token(service, "TASK-WRAPPER-HOOK"),
        summary_for_next="Done.",
    )
    assert ready.item_name == "fix"
    snapshot = service.tasks.read_plan_snapshot("TASK-WRAPPER-HOOK", "01-task")
    state = service.tasks.read_execution_state("TASK-WRAPPER-HOOK", "01-task")
    assert snapshot is not None and state is not None
    hook_index = next(
        index
        for index, item in enumerate(snapshot.plan.items)
        if item.name == "inline-argv"
    )
    assert state.item_executions[hook_index].status == "pending"

    service.next("TASK-WRAPPER-HOOK")
    service.complete(
        "TASK-WRAPPER-HOOK",
        artifact="fixed",
        caller_role="worker", assignment=assignment_token(service, "TASK-WRAPPER-HOOK"),
        summary_for_next="Done.",
    )
    service.next("TASK-WRAPPER-HOOK")
    stopped = service.loop(
        "TASK-WRAPPER-HOOK",
        artifact="clean",
        caller_role="worker", assignment=assignment_token(service, "TASK-WRAPPER-HOOK"),
        summary_for_next="Done.",
    )
    assert stopped.item_name == "inline-argv"
    service.next("TASK-WRAPPER-HOOK", caller_role="manager")

    state = service.tasks.read_execution_state("TASK-WRAPPER-HOOK", "01-task")
    assert state is not None
    assert state.item_executions[hook_index].status == "completed"


def test_cli_records_worker_stop_decision(tmp_path: Path, capsys) -> None:
    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-CLI-LOOP", agent="codex")
    service.next("TASK-CLI-LOOP")

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "loop",
                "TASK-CLI-LOOP",
                "--break",
                "--artifact=No meaningful findings.",
                "--summary=Review is clean.",
                "--role",
                "worker",
            ]
        )
        == 0
    )
    rendered = capsys.readouterr().out
    assert "update-workflow-summary" in rendered


def test_stop_finishes_step_hooks_before_skipping_remaining_body(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        loop:
          - review: Review the implementation.
            break: There are no meaningful findings.
            hooks:
              after_complete:
                - preserve-review: Preserve the review result.
          - fix: Fix the findings.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-HOOK", agent="codex")
    service.next("TASK-HOOK")

    hook = service.loop(
        "TASK-HOOK",
        artifact="clean",
        caller_role="worker", assignment=assignment_token(service, "TASK-HOOK"),
        summary_for_next="Done.",
    )
    assert hook.item_name == "preserve-review"
    service.complete(
        "TASK-HOOK",
        artifact="preserved",
        caller_role="worker", assignment=assignment_token(service, "TASK-HOOK"),
        summary_for_next="Done.",
    )

    state = service.tasks.read_execution_state("TASK-HOOK", "01-task")
    snapshot = service.tasks.read_plan_snapshot("TASK-HOOK", "01-task")
    assert state is not None and snapshot is not None
    statuses = {
        item.name: record.status
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
    }
    assert statuses["preserve-review"] == "completed"
    assert statuses["fix"] == "completed"


def test_continue_restarts_loop_body_from_its_beginning(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        loop:
          - review: Review the implementation.
            continue: Findings remain and need another review.
          - fix: Fix the review findings.
            break: The findings are fixed.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-CONTINUE", agent="codex")

    review = service.next("TASK-CONTINUE")
    assert review.loop_continue_prompt == "Findings remain and need another review."
    assert "--continue" in (review.loop_continue_command or "")
    repeated = service.loop(
        "TASK-CONTINUE",
        artifact="findings remain",
        continue_loop=True,
        caller_role="worker", assignment=assignment_token(service, "TASK-CONTINUE"),
        summary_for_next="Done.",
    )
    assert repeated.item_name == "review"
    state = service.tasks.read_execution_state("TASK-CONTINUE", "01-task")
    assert state is not None
    assert dict(state.loop_iterations) == {"review-and-fix": 2}

    service.complete(
        "TASK-CONTINUE",
        artifact="clean",
        caller_role="worker", assignment=assignment_token(service, "TASK-CONTINUE"),
        summary_for_next="Done.",
    )
    service.next("TASK-CONTINUE")
    stopped = service.loop(
        "TASK-CONTINUE",
        artifact="fixed",
        caller_role="worker", assignment=assignment_token(service, "TASK-CONTINUE"),
        summary_for_next="Done.",
    )
    assert stopped.item_name == "update-workflow-summary"


def test_per_round_gives_one_worker_the_whole_round(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        loop:
          - review: Review the implementation.
            break: There are no meaningful findings.
          - fix: Fix the review findings.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    entry = start_after_init(
        service,
        "task",
        "TASK-ROUND",
        agent="codex",
        workflow_runtime="auto",
        caller_role="manager",
    )
    assert entry.action_kind == "loop"
    dispatched = service.next("TASK-ROUND", caller_role="manager")
    assert (dispatched.item_name, dispatched.next_role) == ("review", "worker")

    review = service.status(
        "TASK-ROUND",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-ROUND"),
    )
    rendered = MarkdownOutputAdapter().render_instruction(review)
    assert review.loop_name == "review-and-fix"
    assert (review.loop_iteration, review.max_rounds) == (1, 3)
    assert review.assignment_scope == {
        "loop_assignment": "per_round",
        "loop": "review-and-fix",
        "stages": ["review", "fix"],
    }
    assert "### Assignment scope" in rendered
    assert (
        "one worker performs these steps (`review`, `fix`) of one round of the "
        "`review-and-fix` loop." in rendered
    )
    assert "Do one step at a time" in rendered
    assert (
        "This is the first round of the `review-and-fix` loop. It builds on "
        "the work of the steps before the loop." in rendered
    )

    # The same worker receives the next body step; the manager is not involved.
    fix = service.complete(
        "TASK-ROUND",
        artifact="findings",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-ROUND"),
        summary_for_next="Done.",
    )
    assert (fix.item_name, fix.item_status, fix.next_role) == (
        "fix",
        "in_progress",
        "worker",
    )
    assert fix.continues_assignment is True
    assert "## Worker: next stage, `fix`" in (
        MarkdownOutputAdapter().render_instruction(fix)
    )

    # The repeat boundary ends the round and returns control to the manager.
    boundary = service.complete(
        "TASK-ROUND",
        artifact="fixed",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-ROUND"),
        summary_for_next="Done.",
    )
    assert boundary.action_kind == "loop"
    assert boundary.next_role == "manager"

    second = service.next("TASK-ROUND", caller_role="manager")
    assert second.item_name == "review"
    review_again = service.status(
        "TASK-ROUND",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-ROUND"),
    )
    rendered = MarkdownOutputAdapter().render_instruction(review_again)
    assert review_again.loop_iteration == 2
    assert "This is round 2 of the `review-and-fix` loop, limit 3." in rendered
    assert "Concentrate on the work done in the previous rounds" in rendered
    assert "./ww artifacts TASK-ROUND" in rendered

    # A break in the middle of the round still exits the loop.
    done = service.loop(
        "TASK-ROUND",
        artifact="clean",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-ROUND"),
        summary_for_next="Done.",
    )
    assert done.status == "completed" or done.item_name != "fix"
    state = service.tasks.read_execution_state("TASK-ROUND", "01-task")
    assert state is not None
    assert dict(state.loop_iterations) == {"review-and-fix": 2}


def test_a_later_round_sees_the_previous_round_last_step(tmp_path: Path) -> None:
    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-PREV", agent="codex", caller_role="manager")

    review = service.next("TASK-PREV", caller_role="manager")
    assert review.previous_step is None
    service.complete(
        "TASK-PREV",
        artifact="Two findings.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-PREV"),
        summary_for_next="Done.",
    )
    fix = service.next("TASK-PREV", caller_role="manager")
    assert (fix.previous_step, fix.previous_step_summary) == ("review", "Done.")
    assert (fix.previous_step_artifact or "").endswith("iteration-01/01-review.md")
    service.complete(
        "TASK-PREV",
        artifact="Both fixed.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-PREV"),
        summary_for_next="Done.",
    )

    again = service.next("TASK-PREV", caller_role="manager")
    assert again.item_name == "review"
    assert again.loop_iteration == 2
    assert (again.previous_step, again.previous_step_summary) == ("fix", "Done.")
    assert (again.previous_step_artifact or "").endswith("iteration-01/02-fix.md")


def test_one_manager_next_passes_preparation_hooks_and_nested_boundaries(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - task: ~
    steps:
      - develop: Build it.
      - run-tests: ~
        hooks:
          before_start:
            - argv: [touch, prepared.txt]
        loop:
          - test: Run the tests.
            break: Green.
      - outer: ~
        loop:
          - inner: ~
            loop:
              - probe: Probe.
                break: Done.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(
        service,
        "task",
        "TASK-ONE",
        agent="codex",
        workflow_runtime="auto",
        caller_role="manager",
    )
    service.next("TASK-ONE", caller_role="manager")
    handoff = service.complete(
        "TASK-ONE",
        artifact="built",
        summary_for_next="Built.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-ONE"),
    )
    assert handoff.next_role == "manager"
    assert not (tmp_path / "prepared.txt").exists()

    # Hooks run, the loop is entered, and the first body step is dispatched
    # by this one call.
    test = service.next("TASK-ONE", caller_role="manager")
    assert (test.item_name, test.item_status) == ("test", "in_progress")
    assert (tmp_path / "prepared.txt").exists()

    service.loop(
        "TASK-ONE",
        artifact="green",
        summary_for_next="Green.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-ONE"),
    )
    probe = service.next("TASK-ONE", caller_role="manager")
    assert (probe.item_name, probe.item_status) == ("probe", "in_progress")
    state = service.tasks.read_execution_state("TASK-ONE", "01-task")
    assert state is not None
    assert dict(state.loop_iterations) == {"run-tests": 1, "outer": 1, "outer/inner": 1}


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
              before_start:
                - name: evidence
                  shell: 'printf %s "$WW_OPERATION_ATTEMPT"; exit 1'
"""
    (tmp_path / "ww.yaml").write_text(config, encoding="utf-8")
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
              before_start:
                - name: evidence
                  argv: [printf, evidence]
"""
    (tmp_path / "ww.yaml").write_text(config, encoding="utf-8")
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


def test_continue_runs_completion_hook_then_blocks_at_loop_limit(
    tmp_path: Path,
) -> None:
    service = configured_service(
        tmp_path,
        """handlers:
  - name: audit
    argv: [touch, audit.txt]
workflows:
  - name: task
    steps:
      - name: cycle
        max_rounds: 1
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
