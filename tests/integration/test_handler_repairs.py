# SPDX-License-Identifier: GPL-3.0-or-later
"""Automatic commands retain execution ownership during agent repair assignments."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.workflow_helpers import configured_service
from ww.errors import ConfigurationError, StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage


def workflow(command: str, policy: str = "fix") -> str:
    return f"""workflows:
  - name: task
    steps:
      - A0: Do the manual work.
        kind: prompt
      - A: ~
        shell: echo A >> executions
      - B: ~
        {command}
        on_failure: {policy}
        on_failure_instruction: Fix the broken input.
      - C: Continue the manual work.
        kind: prompt
"""


def complete_manual(service: WorkflowService, runtime: str):
    service.start("task", "TASK-1", workflow_runtime=runtime, caller_role="manager")
    active = service.next("TASK-1", caller_role="manager")
    result = service.complete(
        "TASK-1",
        artifact="Manual work done.",
        summary_for_next="Ready.",
        caller_role="worker" if runtime == "auto" else None,
        assignment=active.assignment_token,
    )
    if runtime == "auto":
        while (
            result.item_name in {"A", "B"}
            and not result.handler_repair
            and not result.operator_reason
        ):
            result = service.next("TASK-1", caller_role="manager")
    return result


def open_repair(service: WorkflowService, runtime: str, instruction):
    return (
        service.next("TASK-1", caller_role="manager")
        if runtime == "auto"
        else instruction
    )


@pytest.mark.parametrize("runtime", ["single", "auto"])
@pytest.mark.parametrize("form", ["shell", "argv"])
def test_repair_is_persistent_and_retries_only_failed_handler(
    tmp_path: Path, runtime: str, form: str
):
    program = (
        "from pathlib import Path; print('broken input diagnostic'); "
        "exit(0 if Path('fixed').exists() else 1)"
    )
    command = (
        "shell: test -e fixed || { echo 'broken input diagnostic'; exit 1; }"
        if form == "shell"
        else "argv: " + json.dumps([sys.executable, "-c", program])
    )
    service = configured_service(tmp_path, workflow(command))
    failed = complete_manual(service, runtime)
    assert failed.handler_repair is not None
    assert failed.operator_reason is None
    assert "broken input diagnostic" in failed.action_text
    assert "Fix the broken input." in failed.action_text
    assert "Do not independently execute" in failed.action_text
    assert (
        "If the failure comes from the environment rather than the work"
        in failed.action_text
    )
    assert "do not change project files" in failed.action_text
    assert "Say in the completion that the cause was environmental." in (
        failed.action_text
    )
    assert failed.action_text.index("environment rather than the work") < (
        failed.action_text.index("Fix the broken input.")
    )
    assert failed.handler_repair["output_refs"]
    state, snapshot = service.load("TASK-1")
    assert snapshot.plan.items[state.cursor].owner == "ww"
    assert len(snapshot.plan.items) == 6  # init, A0, A, B, C, workflow summary
    assert (tmp_path / "executions").read_text() == "A\n"
    service = WorkflowService(Storage(tmp_path))  # resume in another process/session
    repair = open_repair(service, runtime, service.instruction("TASK-1"))
    assert repair.handler_repair is not None
    assert repair.item_status == "in_progress"
    assert "ww complete" in repair.action_text
    assert "complete TASK-1" in repair.continuation_command
    (tmp_path / "fixed").touch()
    result = service.complete(
        "TASK-1",
        artifact="Created fixed input.",
        caller_role="worker",
        assignment=repair.assignment_token,
    )
    assert result.item_name == "C"
    assert result.item_status == ("in_progress" if runtime == "single" else "pending")
    assert (tmp_path / "executions").read_text() == "A\n"
    state, snapshot = service.load("TASK-1")
    record = next(
        record
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.name == "B"
    )
    assert record.status == "completed"
    assert not record.repair_pending
    assert record.repair_artifacts
    assert any(
        "repair_artifact" in artifact for artifact in service.artifacts("TASK-1")
    )


@pytest.mark.parametrize("runtime", ["single", "auto"])
def test_repair_attempts_retain_worker_token_and_stop_at_limit(
    tmp_path: Path, runtime: str
):
    service = configured_service(tmp_path, workflow("shell: echo failure; exit 1"))
    repair = open_repair(service, runtime, complete_manual(service, runtime))
    token = repair.assignment_token
    repair = service.complete(
        "TASK-1",
        artifact="First attempted fix.",
        caller_role="worker",
        assignment=token,
    )
    assert repair.handler_repair["attempt"] == 2
    assert repair.assignment_token == token
    limited = service.complete(
        "TASK-1",
        artifact="Second attempted fix.",
        caller_role="worker",
        assignment=token,
    )
    assert limited.control == "awaiting_operator"
    assert limited.operator_reason == "fix_limit"
    assert "failure" in limited.error
    assert "3 of 3" in limited.error
    assert "skip" in service.force_target("TASK-1")
    # Operator-authorized retry gets a fresh budget; it executes via ww.
    retried = service.next("TASK-1", retry=True, caller_role="manager")
    assert retried.handler_repair["attempt"] == 1
    assert retried.operator_reason is None


@pytest.mark.parametrize("runtime", ["single", "auto"])
def test_operator_policy_stops_without_repair(tmp_path: Path, runtime: str):
    service = configured_service(
        tmp_path, workflow("shell: echo failure; exit 1", "operator")
    )
    failed = complete_manual(service, runtime)
    assert failed.control == "awaiting_operator"
    assert failed.operator_reason == "handler_failed"
    assert failed.handler_repair is None
    assert "failure" in failed.error


@pytest.mark.parametrize("runtime", ["single", "auto"])
@pytest.mark.parametrize(
    "command",
    ['shell: "true"', f"argv: {json.dumps([sys.executable, '-c', 'pass'])}"],
)
def test_successful_automated_steps_do_not_create_repair_assignments(
    tmp_path: Path, runtime: str, command: str
):
    service = configured_service(tmp_path, workflow(command))
    result = complete_manual(service, runtime)
    assert result.item_name == "C"
    assert result.item_status == ("in_progress" if runtime == "single" else "pending")
    assert result.handler_repair is None
    state, _ = service.load("TASK-1")
    assert not any(
        record.repair_pending or record.repair_failures
        for record in state.item_executions
    )
    state, snapshot = service.load("TASK-1")
    b_item = next(item for item in snapshot.plan.items if item.name == "B")
    b_record = state.item_executions[snapshot.plan.items.index(b_item)]
    assert b_item.owner == "ww"
    assert b_record.status == "completed"


def test_explicit_automatic_handler_gets_visibility_guidance_only_for_repair(
    tmp_path: Path,
):
    service = configured_service(
        tmp_path,
        """workflows:
  - name: task
    explicit: true
    steps:
      - A0: Do the manual work.
        kind: prompt
      - B: ~
        shell: test -e fixed || { echo broken; exit 1; }
        on_failure: fix
      - C: Continue the manual work.
        kind: prompt
""",
    )
    failed = complete_manual(service, "auto")
    assert failed.handler_repair is not None
    assert failed.assignment_explicit_steps == ("B",)

    repair = failed
    rendered = MarkdownOutputAdapter().render_instruction(repair)

    assert repair.explicit is True
    assert "Explicit work guidance applies to: `B`." in rendered
    assert "Do not independently execute the handler command" in repair.action_text
    assert "ww complete TASK-1" in repair.continuation_command
    state, snapshot = service.load("TASK-1")
    assert snapshot.plan.items[state.cursor].owner == "ww"

    (tmp_path / "fixed").touch()
    result = service.complete(
        "TASK-1",
        artifact="Created fixed input.",
        caller_role="worker",
        assignment=repair.assignment_token,
    )
    assert result.item_name == "C"
    assert result.handler_repair is None
    state, snapshot = service.load("TASK-1")
    item = snapshot.plan.items[state.cursor - 1]
    record = state.item_executions[state.cursor - 1]
    assert item.name == "B" and item.owner == "ww"
    assert record.status == "completed" and not record.repair_pending


def test_repair_completion_requires_active_assignment_and_artifact(tmp_path: Path):
    service = configured_service(tmp_path, workflow('shell: "false"'))
    repair = complete_manual(service, "auto")
    with pytest.raises(StateError, match="not open"):
        service.complete(
            "TASK-1", artifact="Fix", caller_role="worker", assignment="missing"
        )
    with pytest.raises(StateError, match="requires an artifact"):
        service.complete(
            "TASK-1", caller_role="worker", assignment=repair.assignment_token
        )
    with pytest.raises(StateError, match="not open"):
        service.complete(
            "TASK-1", artifact="Fix", caller_role="worker", assignment="stale"
        )


def test_hook_failure_instruction_inherits_and_member_can_override(tmp_path: Path):
    service = configured_service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - develop: Fix it.
        kind: prompt
        hooks:
          before_complete:
            - on_failure: fix
              on_failure_instruction: Fix the build errors.
              handlers:
                - shell: echo first; exit 1
                - shell: echo second; exit 1
                  on_failure_instruction: Fix the second command.
""",
    )
    service.start("task", "TASK-1")
    service.next("TASK-1")
    failed = service.complete("TASK-1", artifact="Done", summary_for_next="Done")
    assert failed.fix_required is not None
    assert [failure.text for failure in failed.fix_required.failures] == [
        "Fix the build errors.",
        "Fix the second command.",
    ]
    rendered = MarkdownOutputAdapter().render_instruction(failed)
    assert "Fix the build errors." in rendered
    assert "Fix the second command." in rendered


def test_catalog_handler_policy_is_inherited_and_overridable(tmp_path: Path):
    service = configured_service(
        tmp_path,
        """handlers:
  - build: ~
    shell: "false"
    on_failure: fix
    on_failure_instruction: Repair assets.
workflows:
  - name: task
    steps:
      - build: ~
      - build-again: ~
        handler: build
        on_failure: operator
""",
    )
    service.start("task", "TASK-1")
    result = service.next("TASK-1")
    state, snapshot = service.load("TASK-1")
    builds = [item for item in snapshot.plan.items if item.kind == "cli"]
    assert [item.on_failure for item in builds] == ["fix", "operator"]
    assert builds[0].on_failure_instruction == "Repair assets."
    assert result.handler_repair is not None


@pytest.mark.parametrize(
    "fields",
    [
        "kind: prompt\n        on_failure: fix",
        'shell: "false"\n        on_failure: skip',
        "shell: \"false\"\n        on_failure_instruction: ''",
    ],
)
def test_invalid_repair_configuration_is_rejected(tmp_path: Path, fields: str):
    service = configured_service(
        tmp_path,
        f"workflows:\n  - name: task\n    steps:\n      - build: ~\n        {fields}\n",
    )
    with pytest.raises(ConfigurationError):
        service.start("task", "TASK-1")


def test_interruption_is_not_a_known_failure_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = configured_service(tmp_path, workflow('shell: "true"'))
    service.start("task", "TASK-1")
    service.next("TASK-1")

    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("ww.action_execution._PROCESS.Popen", interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.complete("TASK-1", artifact="Done", summary_for_next="Done")
    resumed = WorkflowService(Storage(tmp_path)).next("TASK-1")
    assert resumed.status == "interrupted"
    assert resumed.operator_reason == "handler_interrupted"
    assert resumed.handler_repair is None


def test_operator_can_force_past_handler_repair_limit(tmp_path: Path):
    (tmp_path / "ww.json").write_text(json.dumps({"limits": {"fixes": 1}}))
    service = configured_service(tmp_path, workflow('shell: "false"'))
    failed = complete_manual(service, "single")
    assert failed.operator_reason == "fix_limit"
    result = service.next(
        "TASK-1",
        force=True,
        force_reason="Operator chose to skip.",
        caller_role="manager",
    )
    assert result.item_name == "C"
    assert result.handler_repair is None
