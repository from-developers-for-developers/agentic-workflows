# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable execution coordinator for automatic workflow actions.

Actions decide which narrow effects to perform. This module deliberately owns
the execution ledger, process boundary, output storage, and workflow cursor.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from ww.actions import (
    ActionResult,
    AutomaticAction,
    CommandOutcome,
    CommandRequest,
    CommandService,
    Extension,
    ExtensionIdentityService,
    ExtensionService,
    RecoveryCheckResult,
    RecoveryExtensionService,
    actions,
)
from ww.errors import StateError
from ww.execution_models import (
    CommandExecution,
    ExecutionState,
    PlanSnapshot,
    operation_scope_for,
)
from ww.extensions import (
    ExtensionCheckResult,
    ExtensionContext,
    ExtensionHandler,
    ExtensionRegistry,
    parse_reference,
)
from ww.interpolation import dependencies, interpolate
from ww.metadata_publication import MetadataPublisher, validate_metadata_values
from ww.plan import PlanItem, WorkflowPlan
from ww.storage_adapters import CommandOutputAddress
from ww.variables import (
    PROJECT,
    item_workspace_values,
    unbound_item_message,
    unbound_item_values,
)
from ww.workspace import relative_workspace

_OUTPUT_LIMIT = 16_000
STATE_OUTPUT_PREVIEW_LIMIT = 1_000
# The durable executor, rather than a command action implementation, owns the
# process boundary.  Keeping this dependency here also gives recovery tests a
# stable, local seam for simulating an interrupted launch.
_PROCESS = subprocess
CommitRun = Callable[[ExecutionState, PlanSnapshot], None]
ProjectState = Callable[[ExecutionState, WorkflowPlan], ExecutionState]
Clock = Callable[[], str]
WriteCommandOutput = Callable[[CommandOutputAddress, str], str]
ReadCommandOutput = Callable[[str], str]
TaskValues = Callable[[ExecutionState, WorkflowPlan], dict[str, str]]
# ``{{ww.child.*}}`` for a per-child stage, and ``{{ww.item.*}}`` for a per-item
# stage; empty for any other item.
ChildValues = Callable[[ExecutionState, WorkflowPlan, PlanItem], dict[str, str]]


def _no_child_values(
    state: ExecutionState, plan: WorkflowPlan, item: PlanItem
) -> dict[str, str]:
    return {}


@dataclass
class _Dispatch:
    executor: ActionExecutor
    state: ExecutionState
    snapshot: PlanSnapshot
    item: PlanItem


class _CommandService:
    """One-segment durable command executor; no sequence policy lives here."""

    def __init__(self, dispatch: _Dispatch) -> None:
        self._dispatch = dispatch

    def completed(self, segment: int) -> CommandOutcome | None:
        command = self._command(segment)
        if command.status != "completed":
            return None
        return CommandOutcome(
            True,
            self._output(command, "stdout"),
            self._output(command, "stderr"),
            command.exit_code,
        )

    def execute(self, segment: int, request: CommandRequest) -> CommandOutcome:
        command = self._command(segment)
        if command.status == "completed":
            completed = self.completed(segment)
            if completed is None:  # pragma: no cover
                raise AssertionError("completed command did not load")
            return completed
        executor, state, item = (
            self._dispatch.executor,
            self._dispatch.state,
            self._dispatch.item,
        )
        item_record = state.item_executions[state.cursor]
        operation_id = item_record.operation_id
        if operation_id is None:  # pragma: no cover - coordinator establishes it
            raise AssertionError("automatic item has no operation ID")
        command_operation_id = command.operation_id or (
            f"{operation_id}:command:{segment + 1}"
        )
        started = replace(
            command,
            status="in_progress",
            started_at=executor.now(),
            operation_id=command_operation_id,
            attempts=command.attempts + 1,
        )
        self._dispatch.state = replace_command(
            state, state.cursor, segment, started, executor.now()
        )
        executor.commit(self._dispatch.state, self._dispatch.snapshot)
        environment = {
            **os.environ,
            **request.environment,
            "WW_ITEM_OPERATION_ID": operation_id,
            "WW_OPERATION_ID": command_operation_id,
            "WW_OPERATION_ATTEMPT": str(started.attempts),
        }
        try:
            process = _PROCESS.Popen(
                request.argv,
                cwd=(
                    executor.item_scope(
                        self._dispatch.state, self._dispatch.snapshot.plan, item
                    )[0]
                    or executor.root
                ),
                stdout=_PROCESS.PIPE,
                stderr=_PROCESS.PIPE,
                text=True,
                shell=False,
                env=environment,
            )
        except OSError as error:
            detail = f"{type(error).__name__}: {error}"
            failed = replace(
                started,
                status="failed",
                completed_at=executor.now(),
                stderr=bounded(detail, STATE_OUTPUT_PREVIEW_LIMIT),
            )
            self._dispatch.state = replace_command(
                self._dispatch.state,
                self._dispatch.state.cursor,
                segment,
                failed,
                executor.now(),
            )
            executor.commit(self._dispatch.state, self._dispatch.snapshot)
            return CommandOutcome(False, stderr=detail, launch_error=detail)
        with process:
            stdout, stderr = process.communicate()

        def address(stream: str) -> CommandOutputAddress:
            return CommandOutputAddress(
                self._dispatch.state.task_id,
                self._dispatch.state.run_id or self._dispatch.state.workflow,
                item.id,
                command_operation_id,
                started.attempts,
                segment + 1,
                stream,
            )

        stdout_ref = (
            executor.write_command_output(address("stdout"), stdout) if stdout else None
        )
        stderr_ref = (
            executor.write_command_output(address("stderr"), stderr) if stderr else None
        )
        completed_record = replace(
            started,
            status="completed" if process.returncode == 0 else "failed",
            completed_at=executor.now(),
            exit_code=process.returncode,
            stdout=bounded(stdout, STATE_OUTPUT_PREVIEW_LIMIT),
            stderr=bounded(stderr, STATE_OUTPUT_PREVIEW_LIMIT),
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
        )
        self._dispatch.state = replace_command(
            self._dispatch.state,
            self._dispatch.state.cursor,
            segment,
            completed_record,
            executor.now(),
        )
        # Every finished segment is a durable boundary: a success is never
        # replayed, and a recorded failure stays a known outcome even when ww
        # dies before the coordinator writes the item failure.
        executor.commit(self._dispatch.state, self._dispatch.snapshot)
        return CommandOutcome(
            process.returncode == 0, stdout, stderr, process.returncode
        )

    def _command(self, segment: int) -> CommandExecution:
        commands = self._dispatch.state.item_executions[
            self._dispatch.state.cursor
        ].commands
        if not 0 <= segment < len(commands):
            raise StateError(
                f"command segment {segment + 1} is not declared for this action"
            )
        return commands[segment]

    def _output(self, command: CommandExecution, stream: str) -> str:
        reference = command.stdout_ref if stream == "stdout" else command.stderr_ref
        if reference is not None:
            return self._dispatch.executor.read_command_output(reference)
        return command.stdout if stream == "stdout" else command.stderr


class _ExtensionService:
    def __init__(self, dispatch: _Dispatch) -> None:
        self._dispatch = dispatch

    def validate_identity(self, planned: Extension) -> None:
        reference = parse_reference(planned.reference)
        self._dispatch.executor.extensions.validate_identity(
            reference.identifier,
            version=planned.version,
            api_version=planned.api_version,
            source=planned.source,
            fingerprint=planned.fingerprint,
        )

    def handler(self, reference: str) -> ExtensionHandler:
        return self._dispatch.executor.extensions.handler(reference)

    def context(self, planned: Extension) -> ExtensionContext:
        executor, state, item = (
            self._dispatch.executor,
            self._dispatch.state,
            self._dispatch.item,
        )
        reference = parse_reference(planned.reference)
        record = state.item_executions[state.cursor]
        workspace, values = executor.item_scope(
            state, self._dispatch.snapshot.plan, item
        )
        missing = sorted(
            {
                name
                for argument in planned.arguments
                for name in dependencies(argument)
                if name not in values
            }
        )
        if unbound_item_values(missing):
            raise StateError(unbound_item_message(missing))
        if missing:
            raise StateError(
                "extension handler arguments are missing variable(s): "
                + ", ".join(missing)
            )
        return ExtensionContext(
            root=executor.root,
            store=executor.extensions.store(reference.identifier),
            config=executor.item_settings(state, item, planned),
            task_id=state.task_id,
            run_id=state.run_id,
            workflow=state.workflow,
            lane=self._dispatch.snapshot.plan.lane,
            values=values,
            arguments=tuple(
                interpolate(argument, values) for argument in planned.arguments
            ),
            workspace=workspace,
            item_id=item.id,
            work_item_id=item.item_id,
            attempt=record.attempts,
            operation_id=record.operation_id or operation_id_for(state, item),
        )


@dataclass(frozen=True)
class _ExecutionContext:
    root: Path
    workspace: Path | None
    runtime_values: Mapping[str, str]
    task_id: str
    run_id: str | None
    operation_id: str
    attempt: int
    commands: CommandService
    extensions: ExtensionService

    @classmethod
    def create(cls, dispatch: _Dispatch) -> _ExecutionContext:
        executor, state, item = dispatch.executor, dispatch.state, dispatch.item
        record = state.item_executions[state.cursor]
        workspace, values = executor.item_scope(state, dispatch.snapshot.plan, item)
        return cls(
            root=executor.root,
            workspace=workspace,
            runtime_values=MappingProxyType(values),
            task_id=state.task_id,
            run_id=state.run_id,
            operation_id=record.operation_id or operation_id_for(state, item),
            attempt=record.attempts,
            commands=_CommandService(dispatch),
            extensions=_ExtensionService(dispatch),
        )


class _HandlerLookup:
    """Handler access without identity checks, stores, or effects."""

    def __init__(self, executor: ActionExecutor) -> None:
        self._executor = executor

    def handler(self, reference: str) -> ExtensionHandler:
        return self._executor.extensions.handler(reference)


@dataclass(frozen=True)
class _InputValidationContext:
    extensions: _HandlerLookup


class _PreflightExtensions:
    def __init__(self, executor: ActionExecutor) -> None:
        self._executor = executor

    def validate_identity(self, planned: Extension) -> None:
        reference = parse_reference(planned.reference)
        self._executor.extensions.validate_identity(
            reference.identifier,
            version=planned.version,
            api_version=planned.api_version,
            source=planned.source,
            fingerprint=planned.fingerprint,
        )


@dataclass(frozen=True)
class _PreflightContext:
    """Pre-start data only; preflight cannot run commands or obtain stores."""

    root: Path
    workspace: Path | None
    runtime_values: Mapping[str, str]
    task_id: str
    run_id: str | None
    extensions: ExtensionIdentityService

    @classmethod
    def create(
        cls,
        executor: ActionExecutor,
        state: ExecutionState,
        plan: WorkflowPlan,
        item: PlanItem,
    ) -> _PreflightContext:
        workspace, values = executor.item_scope(state, plan, item)
        return cls(
            root=executor.root,
            workspace=workspace,
            runtime_values=MappingProxyType(values),
            task_id=state.task_id,
            run_id=state.run_id,
            extensions=_PreflightExtensions(executor),
        )


class _RecoveryExtensionService:
    """Checker-only extension adapter built from saved state and plan data."""

    def __init__(
        self,
        executor: ActionExecutor,
        state: ExecutionState,
        item: PlanItem,
        plan: WorkflowPlan,
    ) -> None:
        self._executor = executor
        self._state = state
        self._item = item
        self._plan = plan

    def validate_identity(self, planned: Extension) -> None:
        _PreflightExtensions(self._executor).validate_identity(planned)

    def check(self, planned: Extension) -> ExtensionCheckResult:
        reference = parse_reference(planned.reference)
        handler = self._executor.extensions.handler(planned.reference)
        if handler.check is None:
            raise ValueError("extension has no checker")
        record = self._state.item_executions[self._state.cursor]
        workspace, values = self._executor.item_scope(
            self._state, self._plan, self._item
        )
        context = ExtensionContext(
            root=self._executor.root,
            store=self._executor.extensions.store(reference.identifier),
            config=self._executor.item_settings(self._state, self._item, planned),
            task_id=self._state.task_id,
            run_id=self._state.run_id,
            workflow=self._state.workflow,
            lane=self._plan.lane,
            values=values,
            workspace=workspace,
            item_id=self._item.id,
            work_item_id=self._item.item_id,
            attempt=record.attempts,
            operation_id=(
                record.operation_id or operation_id_for(self._state, self._item)
            ),
        )
        return handler.check(context)


@dataclass(frozen=True)
class _RecoveryContext:
    """Recovery view avoids an execution context and fabricated snapshot."""

    root: Path
    workspace: Path | None
    runtime_values: Mapping[str, str]
    task_id: str
    run_id: str | None
    operation_id: str
    attempt: int
    extensions: RecoveryExtensionService

    @classmethod
    def create(
        cls,
        executor: ActionExecutor,
        state: ExecutionState,
        item: PlanItem,
        plan: WorkflowPlan,
    ) -> _RecoveryContext:
        record = state.item_executions[state.cursor]
        workspace, values = executor.item_scope(state, plan, item)
        return cls(
            root=executor.root,
            workspace=workspace,
            runtime_values=MappingProxyType(values),
            task_id=state.task_id,
            run_id=state.run_id,
            operation_id=record.operation_id or operation_id_for(state, item),
            attempt=record.attempts,
            extensions=_RecoveryExtensionService(executor, state, item, plan),
        )


class ActionExecutor:
    """Coordinate automatic actions without exposing workflow lifecycle to them."""

    def __init__(
        self,
        *,
        root: Path,
        extensions: ExtensionRegistry,
        commit: CommitRun,
        project_state: ProjectState,
        now: Clock,
        write_command_output: WriteCommandOutput,
        read_command_output: ReadCommandOutput,
        task_values: TaskValues,
        metadata_publisher: MetadataPublisher,
        child_values: ChildValues = _no_child_values,
        item_values: ChildValues = _no_child_values,
    ) -> None:
        self.root = root
        self.item_values = item_values
        self.extensions = extensions
        self.commit = commit
        self.project_state = project_state
        self.now = now
        self.write_command_output = write_command_output
        self.read_command_output = read_command_output
        self.task_values = task_values
        self.metadata_publisher = metadata_publisher
        self.child_values = child_values

    def item_settings(
        self, state: ExecutionState, item: PlanItem, planned: Extension
    ) -> dict[str, object]:
        """The extension settings ``item`` runs with.

        The plan's frozen copy when it has one; otherwise the settings of the
        directory the item acts on, which are the run's project's for an item
        working in the task workspace or the project directory, and the
        root's for one working in the root.
        """
        if planned.settings is not None:
            return deepcopy(dict(planned.settings))
        project = (
            dict(state.workflow_values).get(PROJECT) if item.workdir != "root" else None
        )
        return self.extensions.settings(
            parse_reference(planned.reference).identifier, project or None
        )

    def item_scope(
        self, state: ExecutionState, plan: WorkflowPlan, item: PlanItem
    ) -> tuple[Path | None, dict[str, str]]:
        """The directory ``item`` runs in and the values it interpolates."""
        return item_workspace_values(
            self.root,
            item.workdir,
            state.working_directory,
            {
                **dict(state.workflow_values),
                **self.task_values(state, plan),
                **self.child_values(state, plan, item),
                **self.item_values(state, plan, item),
            },
        )

    def run(
        self, state: ExecutionState, snapshot: PlanSnapshot, item: PlanItem
    ) -> ExecutionState:
        implementation = actions.get(item.kind)
        if not isinstance(implementation, AutomaticAction):
            raise StateError(
                f"automatic action {item.kind!r} has no execution capability"
            )
        planned = item.payload_as(implementation.planned_type)
        # Identity/configuration checks happen before durable start: rejection
        # proves no external operation was initiated and must not create an
        # unknown-outcome recovery boundary.
        implementation.preflight(
            planned, _PreflightContext.create(self, state, snapshot.plan, item)
        )
        state = self._start_item(state, snapshot, item)
        dispatch = _Dispatch(self, state, snapshot, item)
        result = implementation.execute(planned, _ExecutionContext.create(dispatch))
        if not isinstance(result, ActionResult):
            result = ActionResult.failed(
                "automatic action returned an invalid ActionResult"
            )
        error = action_result_error(result, item.outputs)
        if error is not None:
            result = ActionResult.failed(
                f"automatic action returned an invalid result: {error}"
            )
        # Services update dispatch.state at every durable boundary.
        if not result.ok:
            return self._fail_item(
                dispatch.state,
                snapshot,
                item,
                result.error,
                result=result.output or None,
            )
        try:
            task_metadata, project_metadata = validate_metadata_values(
                {saved.name: (result.output.strip(),) for saved in item.save_metadata},
                item.save_metadata,
            )
            updated_metadata, project_publication = self.metadata_publisher.prepare(
                dispatch.state.task_id,
                dispatch.state,
                item,
                task_metadata,
                project_metadata,
            )
        except StateError as error:
            return self._fail_item(
                dispatch.state, snapshot, item, str(error), result=result.output
            )
        records = list(dispatch.state.item_executions)
        records[dispatch.state.cursor] = replace(
            records[dispatch.state.cursor],
            status="completed",
            completed_at=self.now(),
            result=bounded(result.output, STATE_OUTPUT_PREVIEW_LIMIT).strip() or None,
            output_values=tuple(result.values.items()),
        )
        values = {**dict(dispatch.state.workflow_values), **result.values}
        completed = replace(
            dispatch.state,
            status="pending",
            active_item_id=None,
            cursor=dispatch.state.cursor + 1,
            item_executions=tuple(records),
            updated_at=self.now(),
            working_directory=(
                relative_workspace(self.root, result.working_directory)
                if result.working_directory is not None
                else dispatch.state.working_directory
            ),
            workflow_values=tuple(values.items()),
            pending_task_metadata=updated_metadata.values if updated_metadata else (),
            pending_project_metadata=project_publication,
        )
        completed = self._commit_projected(completed, snapshot)
        completed, _ = self.metadata_publisher.reconcile(completed, snapshot)
        return completed

    def validate_inputs(self, item: PlanItem, values: Mapping[str, str]) -> str | None:
        """Ask an automatic item's action whether it would accept these inputs.

        Runs before the completion that carries the values is saved; nothing
        here may record or execute anything.
        """
        implementation = actions.get(item.kind)
        if not isinstance(implementation, AutomaticAction):
            return None
        return implementation.validate_inputs(
            item.payload_as(implementation.planned_type),
            MappingProxyType(dict(values)),
            _InputValidationContext(_HandlerLookup(self)),
        )

    def check_recovery(
        self, state: ExecutionState, plan: WorkflowPlan, item: PlanItem
    ) -> RecoveryCheckResult | None:
        """Ask the action's checker about an interrupted item, if it has one."""
        implementation = actions.get(item.kind)
        if not isinstance(implementation, AutomaticAction):
            return None
        result = implementation.check_recovery(
            item.payload_as(implementation.planned_type),
            _RecoveryContext.create(self, state, item, plan),
        )
        if result is None:
            return None
        if not isinstance(result, RecoveryCheckResult):
            return RecoveryCheckResult.unknown(
                "automatic checker returned an invalid result"
            )
        if result.status == "succeeded" and result.result is not None:
            if result.scope == "command_segment":
                error = action_result_shape_error(result.result)
                if error is not None:
                    return RecoveryCheckResult.unknown(
                        f"automatic checker returned an invalid result: {error}"
                    )
                assert result.segment is not None  # validated by the result contract
                commands = state.item_executions[state.cursor].commands
                if (
                    result.segment >= len(commands)
                    or commands[result.segment].status != "interrupted"
                ):
                    return RecoveryCheckResult.unknown(
                        "checker attested a command segment that is not interrupted"
                    )
            else:
                error = action_result_error(result.result, item.outputs)
                if error is not None:
                    return RecoveryCheckResult.unknown(
                        f"automatic checker returned an invalid result: {error}"
                    )
        return result

    def _start_item(
        self, state: ExecutionState, snapshot: PlanSnapshot, item: PlanItem
    ) -> ExecutionState:
        records = list(state.item_executions)
        record = records[state.cursor]
        records[state.cursor] = replace(
            record,
            status="in_progress",
            started_at=record.started_at or self.now(),
            attempts=record.attempts + 1,
            error=None,
            operation_id=record.operation_id or operation_id_for(state, item),
        )
        started = replace(
            state,
            status="in_progress",
            active_item_id=item.id,
            item_executions=tuple(records),
            updated_at=self.now(),
        )
        self.commit(started, snapshot)
        return started

    def _fail_item(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        item: PlanItem,
        message: str,
        *,
        result: str | None = None,
    ) -> ExecutionState:
        records = list(state.item_executions)
        message = bounded(message)
        records[state.cursor] = replace(
            records[state.cursor],
            status="failed",
            error=message,
            result=(
                bounded(result, STATE_OUTPUT_PREVIEW_LIMIT)
                if result is not None
                else None
            ),
        )
        failed = replace(
            state,
            status="failed",
            active_item_id=item.id,
            item_executions=tuple(records),
            last_error=message,
            updated_at=self.now(),
        )
        return self._commit_projected(failed, snapshot)

    def _commit_projected(
        self, state: ExecutionState, snapshot: PlanSnapshot
    ) -> ExecutionState:
        state = self.project_state(state, snapshot.plan)
        self.commit(state, snapshot)
        return state


def replace_command(
    state: ExecutionState,
    item_index: int,
    command_index: int,
    command: CommandExecution,
    now: str,
) -> ExecutionState:
    records = list(state.item_executions)
    commands = list(records[item_index].commands)
    commands[command_index] = command
    records[item_index] = replace(records[item_index], commands=tuple(commands))
    return replace(state, item_executions=tuple(records), updated_at=now)


def bounded(value: str, limit: int = _OUTPUT_LIMIT) -> str:
    return value if len(value) <= limit else value[:limit] + "\n[output truncated]"


def operation_id_for(state: ExecutionState, item: PlanItem) -> str:
    return f"{state.task_id}:{operation_scope_for(state)}:{item.id}"


def extension_output_error(
    values: object, declared_outputs: tuple[str, ...]
) -> str | None:
    if not isinstance(values, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in values.items()
    ):
        return "values must be a mapping of strings to strings"
    undeclared = sorted(set(values) - set(declared_outputs))
    missing = sorted(set(declared_outputs) - set(values))
    if undeclared:
        return "returned undeclared workflow value(s): " + ", ".join(undeclared)
    if missing:
        return "did not return declared workflow value(s): " + ", ".join(missing)
    return None


def action_result_error(
    result: ActionResult, declared_outputs: tuple[str, ...]
) -> str | None:
    error = action_result_shape_error(result)
    if error is not None:
        return error
    return (
        extension_output_error(result.values, declared_outputs) if result.ok else None
    )


def action_result_shape_error(result: ActionResult) -> str | None:
    """Validate fields valid for both full-action and segment attestations."""
    if (
        not isinstance(result.ok, bool)
        or not isinstance(result.output, str)
        or not isinstance(result.error, str)
    ):
        return "ok, output, and error must have their declared types"
    if result.working_directory is not None and not isinstance(
        result.working_directory, Path
    ):
        return "working_directory must be a Path or null"
    return None
