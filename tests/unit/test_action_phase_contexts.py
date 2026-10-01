# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase isolation and explicit manual recovery through real saved tasks."""

import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from tests.unit.test_action_example import Checklist, ChecklistAction
from tests.workflow_helpers import start_after_init
from ww.actions import (
    ActionResult,
    ActionTraits,
    AutomaticAction,
    CommandAction,
    Commands,
    ExecutionContext,
    PreflightContext,
    RecoveryCheckResult,
    RecoveryContext,
    actions,
)
from ww.errors import StateError
from ww.service import WorkflowService
from ww.storage import Storage


def _assert_readonly(
    context: ExecutionContext | PreflightContext | RecoveryContext,
) -> None:
    assert context.runtime_values["ww.task.id"] == context.task_id
    with pytest.raises(FrozenInstanceError):
        context.task_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        context.runtime_values["injected"] = "value"  # type: ignore[index]


class ManualOutputAction(AutomaticAction[Checklist, Checklist], ChecklistAction):
    identifier = "test_manual_output"

    def execute(self, planned: Checklist, context: ExecutionContext) -> ActionResult:
        _assert_readonly(context)
        assert context.attempt == 1
        assert context.operation_id
        raise KeyboardInterrupt

    def traits(self, planned: Checklist) -> ActionTraits:
        return ActionTraits(manual_attestation=frozenset({"values", "workspace"}))


class CheckedOutputAction(ManualOutputAction):
    identifier = "test_checked_output"

    def __init__(self) -> None:
        self.preflight_calls = 0
        self.check_calls = 0

    def preflight(self, planned: Checklist, context: PreflightContext) -> None:
        self.preflight_calls += 1
        _assert_readonly(context)
        assert not hasattr(context, "commands")
        assert not hasattr(context.extensions, "handler")
        assert not hasattr(context.extensions, "check")

    def check_recovery(
        self, planned: Checklist, context: RecoveryContext
    ) -> RecoveryCheckResult:
        self.check_calls += 1
        _assert_readonly(context)
        assert not hasattr(context, "commands")
        assert not hasattr(context.extensions, "handler")
        assert callable(context.extensions.check)
        assert context.attempt == 1
        return RecoveryCheckResult.succeeded(
            ActionResult.succeeded(
                "checked",
                values={"token": "issued"},
                working_directory=context.root / "workspace",
            )
        )


@pytest.mark.parametrize("checked", [False, True])
def test_recovery_values_and_workspace_do_not_require_a_checker(
    tmp_path: Path,
    checked: bool,
) -> None:
    action = CheckedOutputAction() if checked else ManualOutputAction()
    actions.register(action)
    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (tmp_path / "ww.yaml").write_text(
            f"""workflows:
  - name: task
    steps:
      - name: issue
        action:
          type: {action.identifier}
          checks: [issue token]
        variables: [token]
      - name: consume
        kind: prompt
        description: "Token {{{{token}}}}"
""",
            encoding="utf-8",
        )
        service = WorkflowService(Storage(tmp_path))
        start_after_init(service, "task", "TASK-CONTEXT", agent="codex")
        with pytest.raises(KeyboardInterrupt):
            service.next("TASK-CONTEXT")
        resumed = WorkflowService(Storage(tmp_path))
        assert resumed.next("TASK-CONTEXT").status == "interrupted"
        if checked:
            result = resumed.recover("TASK-CONTEXT")
            assert isinstance(action, CheckedOutputAction)
            assert (action.preflight_calls, action.check_calls) == (1, 1)
        else:
            result = resumed.recover(
                "TASK-CONTEXT",
                mark_succeeded=True,
                output="attested",
                variables=(("token", "issued"),),
                working_directory=str(workspace),
            )
        assert result.item_name == "consume"
        assert resumed.next("TASK-CONTEXT").action_text == "Token issued"
        state, _ = resumed.load("TASK-CONTEXT")
        assert state.working_directory == os.path.relpath(workspace, tmp_path)
        assert dict(state.workflow_values)["token"] == "issued"
    finally:
        actions.unregister(action.identifier)


@pytest.mark.parametrize("field", ["values", "workspace"])
def test_segment_attestation_rejects_whole_action_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    class AttestableCommand(CommandAction):
        identifier = "test_attestable_segment"

        def traits(self, planned: Commands) -> ActionTraits:
            return replace(
                super().traits(planned),
                manual_attestation=frozenset({"values", "workspace"}),
            )

    action = AttestableCommand()
    actions.register(action)
    try:
        (tmp_path / "ww.yaml").write_text(
            """workflows:
  - name: task
    steps:
      - name: command
        action:
          type: test_attestable_segment
          shell: printf completed
""",
            encoding="utf-8",
        )
        service = WorkflowService(Storage(tmp_path))
        start_after_init(service, "task", "TASK-SEGMENT", agent="codex")

        def interrupt(*args: object, **kwargs: object) -> None:
            raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr("ww.action_execution._PROCESS.Popen", interrupt)
            with pytest.raises(KeyboardInterrupt):
                service.next("TASK-SEGMENT")
        with pytest.raises(StateError, match="command-segment attestation"):
            service.recover(
                "TASK-SEGMENT",
                mark_succeeded=True,
                variables=(("token", "issued"),) if field == "values" else (),
                working_directory=str(tmp_path) if field == "workspace" else None,
            )
        state, _ = service.load("TASK-SEGMENT")
        assert state.status == "interrupted"
        assert not dict(state.workflow_values).get("token")
        assert (
            service.recover(
                "TASK-SEGMENT", mark_succeeded=True, output="completed"
            ).item_name
            == "update-workflow-summary"
        )
    finally:
        actions.unregister(action.identifier)
