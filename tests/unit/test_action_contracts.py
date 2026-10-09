# SPDX-License-Identifier: GPL-3.0-or-later
"""A registered action passes through generic workflow consumers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ww.actions import (
    Action,
    ActionRegistry,
    ActionResult,
    ActionTraits,
    AutomaticAction,
    CommandAction,
    CommandDefinition,
    CommandRequest,
    Commands,
    ExecutionContext,
    InstructionContent,
    InstructionContext,
    PlannedAction,
    RecoveryCheckResult,
    ResolutionContext,
    actions,
)
from ww.actions.command import _parse_command_action
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.execution_models import (
    PLAN_SCHEMA_VERSION,
    PlanSnapshot,
    TaskRunAggregate,
    initial_state,
)
from ww.instructions.text import action_text
from ww.output import render_plan
from ww.plan import (
    WorkflowHandoff,
    compile_workflow_plan,
)
from ww.service import WorkflowService
from ww.storage import Storage
from ww.storage_adapters.memory import MemoryTaskStorageAdapter
from ww.storage_adapters.task_document import decode_run_document, encode_run_document


@dataclass(frozen=True)
class ProbeDefinition:
    template: str


@dataclass(frozen=True)
class ProbePayload:
    message: str
    snapshot: str


class ProbeAction(Action[ProbeDefinition, ProbePayload]):
    identifier = "test_probe"
    planned_type = ProbePayload

    def parse(
        self, source: dict[str, object], name: str, description: str, path: str
    ) -> ProbeDefinition:
        if set(source) != {"message"} or not isinstance(source["message"], str):
            raise ConfigurationError(f"{path}.message must be a string")
        return ProbeDefinition(source["message"])

    def validate(self, definition: ProbeDefinition, path: str) -> None:
        if not definition.template:
            raise ConfigurationError(f"{path}.message must not be empty")

    def templates(self, definition: ProbeDefinition) -> tuple[str, ...]:
        return (definition.template,)

    def plan(
        self, definition: ProbeDefinition, context: ResolutionContext
    ) -> ProbePayload:
        return ProbePayload(context.interpolate(definition.template), "custom-data")

    def instruction(
        self, planned: ProbePayload, context: InstructionContext
    ) -> InstructionContent:
        return InstructionContent(
            planned.message, ("**Probe**", "", planned.message, "")
        )

    def encode(self, planned: ProbePayload) -> dict[str, object]:
        return {"message": planned.message, "snapshot": planned.snapshot}

    def decode(self, data: dict[str, object]) -> ProbePayload:
        if (
            set(data) != {"message", "snapshot"}
            or not isinstance(data["message"], str)
            or not isinstance(data["snapshot"], str)
        ):
            raise ValueError("invalid probe payload")
        return ProbePayload(data["message"], data["snapshot"])


class AlternateCommandAction(CommandAction):
    """A command action with a non-built-in identifier for registry coverage."""

    identifier = "test_command"


class CheckableCommandAction(CommandAction):
    """A generic command action whose checker attests one durable segment."""

    identifier = "test_checkable_command"

    def __init__(self) -> None:
        self.recovery_result = RecoveryCheckResult.unknown()

    def parse(
        self, source: dict[str, Any], name: str, description: str, path: str
    ) -> Commands:
        """Several commands in one payload, which the cli shape cannot write."""
        del name, description
        return Commands(
            tuple(_parse_command_action(item, path) for item in source["commands"])
        )

    def check_recovery(
        self, planned: Commands, context: ExecutionContext
    ) -> RecoveryCheckResult:
        del planned, context
        return self.recovery_result

    def execute(self, planned: Commands, context: ExecutionContext) -> ActionResult:
        result = super().execute(planned, context)
        if not result.ok:
            return result
        return ActionResult.succeeded(result.output, values={"token": "issued"})


@dataclass(frozen=True)
class AutomaticProbePayload:
    text: str


class AutomaticProbeAction(
    AutomaticAction[AutomaticProbePayload, AutomaticProbePayload]
):
    """A non-built-in automatic action using only the public narrow services."""

    identifier = "test_automatic_probe"
    planned_type = AutomaticProbePayload

    def __init__(self) -> None:
        self.check_calls = 0

    def parse(
        self, source: dict[str, object], name: str, description: str, path: str
    ) -> AutomaticProbePayload:
        if set(source) - {"text"} or not isinstance(source.get("text"), str):
            raise ConfigurationError(f"{path}.text must be a string")
        return AutomaticProbePayload(source["text"])

    def validate(self, definition: AutomaticProbePayload, path: str) -> None:
        if not definition.text:
            raise ConfigurationError(f"{path}.text must not be empty")

    def templates(self, definition: AutomaticProbePayload) -> tuple[str, ...]:
        return (definition.text,)

    def plan(
        self, definition: AutomaticProbePayload, context: ResolutionContext
    ) -> AutomaticProbePayload:
        return AutomaticProbePayload(context.interpolate(definition.text))

    def instruction(
        self, planned: AutomaticProbePayload, context: InstructionContext
    ) -> InstructionContent:
        return InstructionContent(planned.text, ())

    def encode(self, planned: AutomaticProbePayload) -> dict[str, object]:
        return {"text": planned.text}

    def decode(self, data: dict[str, object]) -> AutomaticProbePayload:
        if set(data) != {"text"} or not isinstance(data["text"], str):
            raise ValueError("invalid automatic probe payload")
        return AutomaticProbePayload(data["text"])

    def traits(self, planned: AutomaticProbePayload) -> ActionTraits:
        return ActionTraits(
            command_segments=(CommandDefinition(argv=("sh", "-c", planned.text)),),
        )

    def execute(
        self, planned: AutomaticProbePayload, context: ExecutionContext
    ) -> ActionResult:
        outcome = context.commands.completed(0)
        if outcome is None:
            outcome = context.commands.execute(
                0, CommandRequest(("sh", "-c", planned.text), {})
            )
        if not outcome.ok:
            return ActionResult.failed(outcome.stderr or "probe command failed")
        return ActionResult.succeeded(outcome.stdout)

    def check_recovery(
        self, planned: AutomaticProbePayload, context: ExecutionContext
    ) -> RecoveryCheckResult:
        del planned, context
        self.check_calls += 1
        return RecoveryCheckResult.not_succeeded()


class OutputProbeAction(AutomaticProbeAction):
    identifier = "test_output_probe"

    def execute(
        self, planned: AutomaticProbePayload, context: ExecutionContext
    ) -> ActionResult:
        result = super().execute(planned, context)
        if not result.ok:
            return result
        return ActionResult.succeeded(result.output, values={"token": "issued"})


def test_action_registry_rejects_incomplete_or_inconsistent_actions() -> None:
    registry = ActionRegistry()

    class Incomplete:
        identifier = "incomplete"

    with pytest.raises(TypeError, match="must subclass ww.actions.Action"):
        registry.register(Incomplete())  # type: ignore[arg-type]

    class InvalidAutomatic(ProbeAction):
        identifier = "invalid_automatic"
        owner = "ww"
        execution = "automatic"

    with pytest.raises(TypeError, match="must subclass AutomaticAction"):
        registry.register(InvalidAutomatic())

    class AgentAutomatic(AutomaticProbeAction):
        identifier = "agent_automatic"
        owner = "agent"

    with pytest.raises(ValueError, match="incompatible owner/execution"):
        registry.register(AgentAutomatic())


def test_recovery_check_result_rejects_an_unknown_scope() -> None:
    with pytest.raises(ValueError, match="invalid scope"):
        RecoveryCheckResult(
            "succeeded",
            ActionResult.succeeded(),
            scope="typo",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "factory, arguments",
    [(WorkflowHandoff, ("",))],
)
def test_core_operations_reject_invalid_scheduling_data(factory, arguments) -> None:
    with pytest.raises(ValueError):
        factory(*arguments)


def test_custom_action_yaml_outputs_are_declared_and_reach_downstream_steps(
    tmp_path: Path,
) -> None:
    actions.register(OutputProbeAction())
    try:
        (tmp_path / "ww.yaml").write_text(
            """workflows:
  - name: task
    steps:
      - name: issue-token
        action:
          type: test_output_probe
          text: "printf issued"
        variables: [token]
      - name: consume-token
        description: "The token is {{token}}."
        kind: prompt
""",
            encoding="utf-8",
        )
        service = WorkflowService(Storage(tmp_path))
        instruction = service.start("task", "TASK-OUTPUT", agent="codex")
        if instruction.item_name == "init":
            service.next("TASK-OUTPUT")
            service.complete(
                "TASK-OUTPUT",
                artifact="requirements",
                summary_for_next="Done.",
            )
        consumed = service.next("TASK-OUTPUT")
        assert consumed.item_name == "consume-token"
        assert consumed.action_text == "The token is issued."
        state = service.tasks.read_execution_state("TASK-OUTPUT", "01-task")
        assert state is not None
        assert state.workflow_values[-1] == ("token", "issued")
    finally:
        actions.unregister("test_output_probe")


def test_custom_command_payload_overrides_text_through_aliases(
    tmp_path: Path,
) -> None:
    """A command-capable custom payload needs no parser or executor special case."""
    actions.register(OutputProbeAction())
    try:
        (tmp_path / "ww.yaml").write_text(
            """handlers:
  - name: restricted-probe
    action:
      type: test_output_probe
      text: "printf inherited"
    variables: [token]
workflows:
  - name: task
    steps:
      - name: use-probe
        handler: restricted-probe
        action:
          type: test_output_probe
          text: "printf overridden"
      - name: consume-token
        description: "Token is {{token}}."
        kind: prompt
""",
            encoding="utf-8",
        )
        plan = compile_workflow_plan(
            load_configuration(tmp_path / "ww.yaml"),
            tmp_path,
            "task",
            "codex",
            "TASK-CUSTOM-COMMAND",
        )
        probe = next(item for item in plan.items if item.name == "use-probe")
        assert probe.payload_as(AutomaticProbePayload) == AutomaticProbePayload(
            "printf overridden"
        )

        service = WorkflowService(Storage(tmp_path))
        started = service.start("task", "TASK-CUSTOM-COMMAND", agent="codex")
        if started.item_name == "init":
            service.next("TASK-CUSTOM-COMMAND")
            service.complete(
                "TASK-CUSTOM-COMMAND",
                artifact="requirements",
                summary_for_next="Done.",
            )
        consumed = service.next("TASK-CUSTOM-COMMAND")
        assert consumed.item_name == "consume-token"
        assert consumed.action_text == "Token is issued."
    finally:
        actions.unregister("test_output_probe")


def test_registered_action_round_trips_without_consumer_changes(tmp_path: Path) -> None:
    actions.register(ProbeAction())
    try:
        path = tmp_path / "ww.yaml"
        path.write_text(
            """workflows:
  - name: task
    steps:
      - name: probe
        action:
          type: test_probe
          message: Inspect {{ww.task.id}}
""",
            encoding="utf-8",
        )
        plan = compile_workflow_plan(
            load_configuration(path), tmp_path, "task", "codex", "TASK-1"
        )
        item = next(item for item in plan.items if item.name == "probe")
        assert item.payload_as(ProbePayload) == ProbePayload(
            "Inspect TASK-1", "custom-data"
        )
        assert action_text(item) == "Inspect TASK-1"

        snapshot = PlanSnapshot(
            schema_version=PLAN_SCHEMA_VERSION,
            compiler_version="test",
            configuration_digest="test",
            compiled_at="2026-09-21T00:00:00Z",
            plan=plan,
        )
        serialized = snapshot.to_dict()
        restored = PlanSnapshot.from_dict(serialized)
        restored_item = next(
            item for item in restored.plan.items if item.name == "probe"
        )
        assert restored_item.payload_as(ProbePayload) == ProbePayload(
            "Inspect TASK-1", "custom-data"
        )

        state = initial_state(snapshot, (), "2026-09-21T00:00:00Z", "01-task")
        document = encode_run_document(
            "TASK-1", TaskRunAggregate("01-task", "task", snapshot, state), 1
        )
        raw_item = document["run"]["snapshot"]["plan"]["items"][1]
        assert raw_item["operation"]["payload"]["snapshot"] == "custom-data"
        decoded_run = decode_run_document(document, "TASK-1", "01-task", 1)
        decoded_item = decoded_run.snapshot.plan.items[1]
        assert decoded_item.payload_as(ProbePayload).snapshot == "custom-data"
    finally:
        actions.unregister("test_probe")

    readable = PlanSnapshot.from_dict(serialized)
    unavailable = next(item for item in readable.plan.items if item.name == "probe")
    assert isinstance(unavailable.operation, PlannedAction)
    assert unavailable.operation.payload == {
        "message": "Inspect TASK-1",
        "snapshot": "custom-data",
    }
    rendered = render_plan(readable.plan, False)
    assert "**Unavailable action:** `test_probe`" in rendered
    assert '"snapshot": "custom-data"' in rendered
    with pytest.raises(
        ConfigurationError, match="implementation 'test_probe' is unavailable"
    ):
        action_text(unavailable)


def test_registered_action_can_supply_the_bootstrap_task_id(tmp_path: Path) -> None:
    actions.register(ProbeAction())
    try:
        (tmp_path / "ww.yaml").write_text(
            """workflows:
  - name: task
    steps:
      - name: create-external-task
        action:
          type: test_probe
          message: Create the external task.
        variables:
          - name: task_id
      - name: work
        description: Complete the work.
""",
            encoding="utf-8",
        )
        service = WorkflowService(Storage(tmp_path))

        pending = service.start("task", None, agent="codex")
        active = service.next(pending.task_id)

        assert active.action_kind == "test_probe"
        assert active.required_values[0].name == "task_id"
    finally:
        actions.unregister("test_probe")


@pytest.mark.parametrize("in_memory", (False, True))
def test_registered_command_action_tracks_and_executes_commands(
    tmp_path: Path, in_memory: bool
) -> None:
    actions.register(AlternateCommandAction())
    try:
        (tmp_path / "ww.yaml").write_text(
            """workflows:
  - name: task
    steps:
      - name: touch-marker
        action:
          type: test_command
          argv: [touch, command-ran.txt]
""",
            encoding="utf-8",
        )
        persistence = MemoryTaskStorageAdapter() if in_memory else None
        service = WorkflowService(Storage(tmp_path, task_persistence=persistence))

        instruction = service.start("task", "TASK-1", agent="codex")
        if instruction.item_name == "init":
            service.next("TASK-1")
            instruction = service.complete(
                "TASK-1",
                artifact="requirements",
                summary_for_next="Done.",
            )
        instruction = service.next("TASK-1")

        assert (tmp_path / "command-ran.txt").exists()
        state = service.tasks.read_execution_state("TASK-1", "01-task")
        assert state is not None
        command_record = state.item_executions[1].commands
        assert len(command_record) == 1
        assert command_record[0].status == "completed"
    finally:
        actions.unregister("test_command")

    reopened = WorkflowService(Storage(tmp_path, task_persistence=persistence))
    assert reopened.status("TASK-1").task_id == "TASK-1"


def test_checker_segment_attestation_resumes_a_generic_command_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = CheckableCommandAction()
    actions.register(action)
    try:
        (tmp_path / "ww.yaml").write_text(
            """workflows:
  - name: task
    steps:
      - name: run-commands
        action:
          type: test_checkable_command
          commands:
            - shell: printf first > first.txt
            - shell: printf second > second.txt
        variables: [token]
""",
            encoding="utf-8",
        )
        service = WorkflowService(Storage(tmp_path))
        instruction = service.start("task", "TASK-CHECK-SEGMENT", agent="codex")
        if instruction.item_name == "init":
            service.next("TASK-CHECK-SEGMENT")
            service.complete(
                "TASK-CHECK-SEGMENT",
                artifact="requirements",
                summary_for_next="Done.",
            )

        def interrupted(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise KeyboardInterrupt

        import subprocess

        real_popen = subprocess.Popen
        monkeypatch.setattr("ww.action_execution._PROCESS.Popen", interrupted)
        with pytest.raises(KeyboardInterrupt):
            service.next("TASK-CHECK-SEGMENT")
        monkeypatch.setattr("ww.action_execution._PROCESS.Popen", real_popen)

        action.recovery_result = RecoveryCheckResult.succeeded_segment(
            0,
            123,  # type: ignore[arg-type]
        )
        malformed = WorkflowService(Storage(tmp_path)).recover("TASK-CHECK-SEGMENT")
        assert malformed.status == "interrupted"
        assert "declared types" in (malformed.error or "")

        action.recovery_result = RecoveryCheckResult.succeeded_segment(
            0, "checked-first"
        )
        recovered = WorkflowService(Storage(tmp_path)).recover("TASK-CHECK-SEGMENT")
        assert recovered.item_name == "update-workflow-summary"
        assert not (tmp_path / "first.txt").exists()
        assert (tmp_path / "second.txt").read_text() == "second"
        state = service.tasks.read_execution_state("TASK-CHECK-SEGMENT", "01-task")
        assert state is not None
        commands = state.item_executions[1].commands
        assert [command.status for command in commands] == ["completed", "completed"]
        assert commands[0].stdout == "checked-first"
        assert state.workflow_values[-1] == ("token", "issued")
    finally:
        actions.unregister("test_checkable_command")


@pytest.mark.parametrize("in_memory", (False, True))
def test_custom_automatic_action_uses_generic_dispatch_and_command_service(
    tmp_path: Path, in_memory: bool
) -> None:
    actions.register(AutomaticProbeAction())
    try:
        (tmp_path / "ww.yaml").write_text(
            """workflows:
  - name: task
    steps:
      - name: automatic-probe
        action:
          type: test_automatic_probe
          text: "printf probe-output"
""",
            encoding="utf-8",
        )
        persistence = MemoryTaskStorageAdapter() if in_memory else None
        service = WorkflowService(Storage(tmp_path, task_persistence=persistence))
        instruction = service.start("task", "TASK-PROBE", agent="codex")
        if instruction.item_name == "init":
            service.next("TASK-PROBE")
            service.complete(
                "TASK-PROBE",
                artifact="requirements",
                summary_for_next="Done.",
            )
        service.next("TASK-PROBE")
        state = service.tasks.read_execution_state("TASK-PROBE", "01-task")
        assert state is not None
        record = state.item_executions[1]
        assert record.status == "completed"
        assert record.result == "probe-output"
        assert len(record.commands) == 1
        assert record.commands[0].status == "completed"
        assert isinstance(actions.get("test_automatic_probe"), AutomaticAction)
    finally:
        actions.unregister("test_automatic_probe")


def test_custom_automatic_action_recovery_checker_uses_generic_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = AutomaticProbeAction()
    actions.register(action)
    try:
        (tmp_path / "ww.yaml").write_text(
            """workflows:
  - name: task
    steps:
      - name: automatic-probe
        action:
          type: test_automatic_probe
          text: "printf recovered"
""",
            encoding="utf-8",
        )
        service = WorkflowService(Storage(tmp_path))
        instruction = service.start("task", "TASK-PROBE-RECOVERY", agent="codex")
        if instruction.item_name == "init":
            service.next("TASK-PROBE-RECOVERY")
            service.complete(
                "TASK-PROBE-RECOVERY",
                artifact="requirements",
                summary_for_next="Done.",
            )

        def interrupted(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise KeyboardInterrupt

        import subprocess

        real_popen = subprocess.Popen
        monkeypatch.setattr("ww.action_execution._PROCESS.Popen", interrupted)
        with pytest.raises(KeyboardInterrupt):
            service.next("TASK-PROBE-RECOVERY")
        monkeypatch.setattr("ww.action_execution._PROCESS.Popen", real_popen)

        resumed = WorkflowService(Storage(tmp_path))
        assert resumed.next("TASK-PROBE-RECOVERY").status == "interrupted"
        recovered = resumed.recover("TASK-PROBE-RECOVERY")
        assert action.check_calls == 1
        assert recovered.item_name == "update-workflow-summary"
    finally:
        actions.unregister("test_automatic_probe")


def test_recovery_checker_action_scope_applies_its_output_to_a_command_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checker result is an action result, not a discarded CLI output flag."""
    action = AutomaticProbeAction()
    actions.register(action)
    try:
        (tmp_path / "ww.yaml").write_text(
            """workflows:
  - name: task
    steps:
      - name: automatic-probe
        action:
          type: test_automatic_probe
          text: "printf should-not-run"
""",
            encoding="utf-8",
        )
        service = WorkflowService(Storage(tmp_path))
        instruction = service.start("task", "TASK-PROBE-CHECKED", agent="codex")
        if instruction.item_name == "init":
            service.next("TASK-PROBE-CHECKED")
            service.complete(
                "TASK-PROBE-CHECKED",
                artifact="requirements",
                summary_for_next="Done.",
            )

        def interrupted(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise KeyboardInterrupt

        import subprocess

        real_popen = subprocess.Popen
        monkeypatch.setattr("ww.action_execution._PROCESS.Popen", interrupted)
        with pytest.raises(KeyboardInterrupt):
            service.next("TASK-PROBE-CHECKED")
        monkeypatch.setattr("ww.action_execution._PROCESS.Popen", real_popen)

        def checked(
            planned: AutomaticProbePayload, context: ExecutionContext
        ) -> RecoveryCheckResult:
            del planned, context
            return RecoveryCheckResult.succeeded(
                ActionResult.succeeded("checked-output")
            )

        action.check_recovery = checked  # type: ignore[method-assign]
        recovered = WorkflowService(Storage(tmp_path)).recover("TASK-PROBE-CHECKED")
        assert recovered.item_name == "update-workflow-summary"
        state = service.tasks.read_execution_state("TASK-PROBE-CHECKED", "01-task")
        assert state is not None
        assert state.item_executions[1].result == "checked-output"
    finally:
        actions.unregister("test_automatic_probe")


def test_custom_command_action_can_attest_an_interrupted_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = AutomaticProbeAction()
    actions.register(action)
    try:
        (tmp_path / "ww.yaml").write_text(
            """workflows:
  - name: task
    steps:
      - name: automatic-probe
        action:
          type: test_automatic_probe
          text: "printf attested"
""",
            encoding="utf-8",
        )
        service = WorkflowService(Storage(tmp_path))
        instruction = service.start("task", "TASK-PROBE-ATTEST", agent="codex")
        if instruction.item_name == "init":
            service.next("TASK-PROBE-ATTEST")
            service.complete(
                "TASK-PROBE-ATTEST",
                artifact="requirements",
                summary_for_next="Done.",
            )

        def interrupted(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise KeyboardInterrupt

        import subprocess

        real_popen = subprocess.Popen
        monkeypatch.setattr("ww.action_execution._PROCESS.Popen", interrupted)
        with pytest.raises(KeyboardInterrupt):
            service.next("TASK-PROBE-ATTEST")
        monkeypatch.setattr("ww.action_execution._PROCESS.Popen", real_popen)

        resumed = WorkflowService(Storage(tmp_path))
        assert resumed.next("TASK-PROBE-ATTEST").status == "interrupted"
        attested = resumed.recover(
            "TASK-PROBE-ATTEST", mark_succeeded=True, output="operator-output"
        )
        assert attested.item_name == "update-workflow-summary"
        state = resumed.tasks.read_execution_state("TASK-PROBE-ATTEST", "01-task")
        assert state is not None
        assert state.item_executions[1].result == "operator-output"
    finally:
        actions.unregister("test_automatic_probe")
