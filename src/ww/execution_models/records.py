# SPDX-License-Identifier: GPL-3.0-or-later
"""Persisted, plan-driven workflow execution models.

These models deliberately contain no YAML or handler-resolution concepts.  A
task starts with a compiled :class:`WorkflowPlan` snapshot and progresses only
through the normalized plan items in that snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ww.contracts import (
    CheckSource,
    CheckStatus,
    CommandStatus,
    ExecutionStatus,
    FailureKind,
    ItemStatus,
    StepStatus,
)
from ww.validation import (
    expect_bool,
    expect_literal,
    expect_nonnegative_int,
    expect_optional_int,
    expect_optional_string,
    expect_positive_int,
    expect_string,
    require_keys,
)
from ww.workflow_config import ProvidedVariable

from .decoding import _positive_int_mapping, _variables

EXECUTION_SCHEMA_VERSION = 7


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check ww ran when a step's worker completed it.

    ``output`` is the tail of what the command printed; the full streams are
    command-output artifacts at ``stdout_ref`` and ``stderr_ref``.
    """

    id: str
    source: CheckSource
    status: CheckStatus
    command: str = ""
    output: str = ""
    exit_code: int | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "status": self.status,
            "command": self.command,
            "output": self.output,
            "exit_code": self.exit_code,
            "stdout_ref": self.stdout_ref,
            "stderr_ref": self.stderr_ref,
        }

    @classmethod
    def from_dict(cls, data: Any) -> CheckResult:
        if not isinstance(data, dict):
            raise ValueError("check result must be a mapping")
        require_keys(data, {"id", "source", "status"}, "check result")
        return cls(
            id=expect_string(data["id"], "check result.id"),
            source=expect_literal(data["source"], CheckSource, "check result.source"),
            status=expect_literal(data["status"], CheckStatus, "check result.status"),
            command=expect_string(data.get("command", ""), "check result.command"),
            output=expect_string(data.get("output", ""), "check result.output"),
            exit_code=expect_optional_int(
                data.get("exit_code"), "check result.exit_code"
            ),
            stdout_ref=expect_optional_string(
                data.get("stdout_ref"), "check result.stdout_ref"
            ),
            stderr_ref=expect_optional_string(
                data.get("stderr_ref"), "check result.stderr_ref"
            ),
        )


@dataclass(frozen=True)
class CheckReport:
    """Every check of one completion attempt, in plan order.

    ``mark`` is the tree the change set was measured to; ``all_files`` says
    there was no change set (no git) and globs selected every project file.
    """

    attempt: int
    checked_at: str
    results: tuple[CheckResult, ...] = ()
    mark: str | None = None
    all_files: bool = False

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.results if result.status == "failed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "checked_at": self.checked_at,
            "results": [result.to_dict() for result in self.results],
            "mark": self.mark,
            "all_files": self.all_files,
        }

    @classmethod
    def from_dict(cls, data: Any) -> CheckReport:
        if not isinstance(data, dict):
            raise ValueError("check report must be a mapping")
        require_keys(data, {"attempt", "checked_at", "results"}, "check report")
        results = data["results"]
        if not isinstance(results, list):
            raise ValueError("check report results must be a list")
        return cls(
            attempt=expect_positive_int(data["attempt"], "check report.attempt"),
            checked_at=expect_string(data["checked_at"], "check report.checked_at"),
            results=tuple(CheckResult.from_dict(item) for item in results),
            mark=expect_optional_string(data.get("mark"), "check report.mark"),
            all_files=expect_bool(
                data.get("all_files", False), "check report.all_files"
            ),
        )


@dataclass(frozen=True)
class CommandExecution:
    index: int
    status: CommandStatus = "pending"
    started_at: str | None = None
    completed_at: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    attempts: int = 0
    # Commands without an external operation do not need an operation ID.
    operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: Any) -> CommandExecution:
        if not isinstance(data, dict):
            raise ValueError("command execution must be a mapping")
        require_keys(
            data,
            {
                "index",
                "status",
                "started_at",
                "completed_at",
                "exit_code",
                "stdout",
                "stderr",
            },
            "command execution",
        )
        return cls(
            index=expect_positive_int(data["index"], "command execution.index"),
            status=expect_literal(
                data["status"], CommandStatus, "command execution.status"
            ),
            started_at=expect_optional_string(
                data["started_at"], "command execution.started_at"
            ),
            completed_at=expect_optional_string(
                data["completed_at"], "command execution.completed_at"
            ),
            exit_code=expect_optional_int(
                data["exit_code"], "command execution.exit_code"
            ),
            stdout=expect_string(data["stdout"], "command execution.stdout"),
            stderr=expect_string(data["stderr"], "command execution.stderr"),
            stdout_ref=expect_optional_string(
                data.get("stdout_ref"), "command execution.stdout_ref"
            ),
            stderr_ref=expect_optional_string(
                data.get("stderr_ref"), "command execution.stderr_ref"
            ),
            operation_id=expect_optional_string(
                data.get("operation_id"), "command execution.operation_id"
            ),
            attempts=expect_nonnegative_int(
                data.get("attempts", 0), "command execution.attempts"
            ),
        )


@dataclass(frozen=True)
class PlanItemExecution:
    plan_item_id: str
    position: int
    status: ItemStatus = "pending"
    started_at: str | None = None
    completed_at: str | None = None
    attempts: int = 0
    supplied_values: tuple[tuple[str, str], ...] = ()
    output_values: tuple[tuple[str, str], ...] = ()
    result: str | None = None
    error: str | None = None
    commands: tuple[CommandExecution, ...] = ()
    artifact: str | None = None
    # The short handover a step's worker wrote for the next step.
    summary_for_next: str | None = None
    # An interactive step's conversation with the operator, as recorded by
    # ``interact``: how many entries, and whether the operator ended it.
    interaction_entries: int = 0
    interaction_ended: bool = False
    # The option the operator chose, when the step offered choices.
    chosen: str | None = None
    # Stable across retries of this plan item.  It identifies the external
    # operation whose outcome may be checked after an interrupted process.
    operation_id: str | None = None
    operation_id_known: bool = True
    model: str | None = None
    reasoning: str | None = None
    selected_agent: str | None = None
    selected_model: str | None = None
    selected_reasoning: str | None = None
    # The worktree's tree when the step began, the base of its change set;
    # ``None`` without git, or for a step without checks.
    change_mark: str | None = None
    # Every completion attempt ww checked, rejected ones first; the number of
    # failed reports is the number of fixes the worker was sent back for.
    check_reports: tuple[CheckReport, ...] = ()
    # The artifact of the last rejected completion, kept for its revision.
    draft_artifact: str | None = None
    # The operator's reason for completing without the checks.
    checks_waived: str | None = None

    @property
    def fix_attempts(self) -> int:
        """How many completions of this step ww rejected for failed checks."""
        return sum(1 for report in self.check_reports if report.failed)

    def check_failures(self, check_id: str) -> int:
        """How many rejected completions this check failed."""
        return sum(
            1
            for report in self.check_reports
            for result in report.failed
            if result.id == check_id
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_item_id": self.plan_item_id,
            "position": self.position,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "attempts": self.attempts,
            "supplied_values": dict(self.supplied_values),
            "output_values": dict(self.output_values),
            "result": self.result,
            "error": self.error,
            "commands": [item.to_dict() for item in self.commands],
            "artifact": self.artifact,
            "summary_for_next": self.summary_for_next,
            "interaction_entries": self.interaction_entries,
            "interaction_ended": self.interaction_ended,
            "chosen": self.chosen,
            "operation_id": self.operation_id,
            "operation_id_known": self.operation_id_known,
            "model": self.model,
            "reasoning": self.reasoning,
            "selected_agent": self.selected_agent,
            "selected_model": self.selected_model,
            "selected_reasoning": self.selected_reasoning,
            "change_mark": self.change_mark,
            "check_reports": [report.to_dict() for report in self.check_reports],
            "draft_artifact": self.draft_artifact,
            "checks_waived": self.checks_waived,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PlanItemExecution:
        if not isinstance(data, dict):
            raise ValueError("plan item execution must be a mapping")
        required = {
            "plan_item_id",
            "position",
            "status",
            "started_at",
            "completed_at",
            "attempts",
            "supplied_values",
            "result",
            "error",
            "commands",
            "artifact",
        }
        require_keys(data, required, "plan item execution")
        values = _variables(data["supplied_values"], "supplied values")
        commands = data["commands"]
        if not isinstance(commands, list):
            raise ValueError("command executions must be a list")
        return cls(
            plan_item_id=expect_string(data["plan_item_id"], "plan item ID"),
            position=expect_positive_int(data["position"], "plan item position"),
            status=expect_literal(
                data["status"], ItemStatus, "plan item execution.status"
            ),
            started_at=expect_optional_string(data["started_at"], "started_at"),
            completed_at=expect_optional_string(data["completed_at"], "completed_at"),
            attempts=expect_nonnegative_int(data["attempts"], "attempts"),
            supplied_values=values,
            output_values=_variables(data.get("output_values", {}), "output values"),
            result=expect_optional_string(data["result"], "result"),
            error=expect_optional_string(data["error"], "error"),
            commands=tuple(CommandExecution.from_dict(item) for item in commands),
            artifact=expect_optional_string(data["artifact"], "artifact"),
            summary_for_next=expect_optional_string(
                data.get("summary_for_next"), "summary for next step"
            ),
            interaction_entries=expect_nonnegative_int(
                data.get("interaction_entries", 0), "interaction entries"
            ),
            interaction_ended=expect_bool(
                data.get("interaction_ended", False), "interaction ended"
            ),
            chosen=expect_optional_string(data.get("chosen"), "chosen option"),
            operation_id=expect_optional_string(
                data.get("operation_id"), "operation_id"
            ),
            operation_id_known=expect_bool(
                data.get("operation_id_known", True), "operation_id_known"
            ),
            model=expect_optional_string(data.get("model"), "execution model"),
            reasoning=expect_optional_string(
                data.get("reasoning"), "execution reasoning"
            ),
            selected_agent=expect_optional_string(
                data.get("selected_agent"), "selected agent"
            ),
            selected_model=expect_optional_string(
                data.get("selected_model"), "selected model"
            ),
            selected_reasoning=expect_optional_string(
                data.get("selected_reasoning"), "selected reasoning"
            ),
            change_mark=expect_optional_string(data.get("change_mark"), "change mark"),
            check_reports=_check_reports(data.get("check_reports", [])),
            draft_artifact=expect_optional_string(
                data.get("draft_artifact"), "draft artifact"
            ),
            checks_waived=expect_optional_string(
                data.get("checks_waived"), "checks waived"
            ),
        )


def _check_reports(value: Any) -> tuple[CheckReport, ...]:
    if not isinstance(value, list):
        raise ValueError("check reports must be a list")
    return tuple(CheckReport.from_dict(item) for item in value)


@dataclass(frozen=True)
class StepProgress:
    """Recursive human-facing state projection; item records stay authoritative."""

    name: str
    path: str
    status: StepStatus = "pending"
    started_at: str | None = None
    completed_at: str | None = None
    children: tuple[StepProgress, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "children": [child.to_dict() for child in self.children],
        }

    @classmethod
    def from_dict(cls, data: Any) -> StepProgress:
        if not isinstance(data, dict):
            raise ValueError("step progress must be a mapping")
        require_keys(
            data,
            {"name", "path", "status", "started_at", "completed_at", "children"},
            "step progress",
        )
        if not isinstance(data["children"], list):
            raise ValueError("step progress children must be a list")
        return cls(
            name=expect_string(data["name"], "step name"),
            path=expect_string(data["path"], "step path"),
            status=expect_literal(data["status"], StepStatus, "step progress.status"),
            started_at=expect_optional_string(data["started_at"], "started_at"),
            completed_at=expect_optional_string(data["completed_at"], "completed_at"),
            children=tuple(cls.from_dict(item) for item in data["children"]),
        )


@dataclass(frozen=True)
class InputRequest:
    item_id: str
    values: tuple[ProvidedVariable, ...]
    continuation: str = "complete"

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "values": [value.to_dict() for value in self.values],
            "continuation": self.continuation,
        }

    @classmethod
    def from_dict(cls, data: Any) -> InputRequest:
        if not isinstance(data, dict):
            raise ValueError("input request must be a mapping")
        require_keys(data, {"item_id", "values", "continuation"}, "input request")
        values = data["values"]
        if not isinstance(values, list):
            raise ValueError("input request values must be a list")
        if not all(isinstance(item, dict) for item in values):
            raise ValueError("input request values must contain only objects")
        return cls(
            item_id=expect_string(data["item_id"], "input request item ID"),
            values=tuple(
                ProvidedVariable(
                    expect_string(item.get("name"), "provided name"),
                    expect_optional_string(
                        item.get("description", ""), "provided description"
                    )
                    or "",
                )
                for item in values
            ),
            continuation=expect_string(data["continuation"], "input continuation"),
        )


def _is_metadata_leaf(value: object) -> bool:
    return isinstance(value, str) or (
        isinstance(value, tuple) and all(isinstance(item, str) for item in value)
    )


def _metadata_leaves_to_dict(
    values: tuple[tuple[str, str | tuple[str, ...]], ...],
) -> dict[str, str | list[str]]:
    return {
        key: list(value) if isinstance(value, tuple) else value for key, value in values
    }


def _metadata_leaves(
    data: Any, label: str
) -> tuple[tuple[str, str | tuple[str, ...]], ...]:
    """Decode metadata leaves: strings, or lists of strings for append keys."""
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a mapping")
    result: list[tuple[str, str | tuple[str, ...]]] = []
    for key, value in data.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings")
        if isinstance(value, str):
            result.append((key, value))
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            result.append((key, tuple(value)))
        else:
            raise ValueError(f"{label} values must be strings or lists of strings")
    return tuple(result)


@dataclass(frozen=True)
class ProjectMetadataPublication:
    """A project-metadata update durably tied to one completed operation.

    ``expected_values`` records the values observed before the source operation
    committed.  Reconciliation may apply the update only when that snapshot is
    still current, or when the desired values are already present from an
    earlier attempt.
    """

    operation_id: str
    values: tuple[tuple[str, str | tuple[str, ...]], ...]
    expected_values: tuple[tuple[str, str | None], ...]

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("project metadata publication requires an operation ID")
        if not self.values or any(
            not isinstance(key, str) or not key or not _is_metadata_leaf(value)
            for key, value in self.values
        ):
            raise ValueError("project metadata publication has invalid values")
        if tuple(key for key, _ in self.values) != tuple(
            key for key, _ in self.expected_values
        ):
            raise ValueError("project metadata publication has mismatched keys")

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "values": _metadata_leaves_to_dict(self.values),
            "expected_values": dict(self.expected_values),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ProjectMetadataPublication:
        if not isinstance(data, dict):
            raise ValueError("project metadata publication must be a mapping")
        values = _metadata_leaves(data.get("values", {}), "project metadata values")
        expected = data.get("expected_values", {})
        if not isinstance(expected, dict) or not all(
            isinstance(key, str) and (value is None or isinstance(value, str))
            for key, value in expected.items()
        ):
            raise ValueError("project metadata expected values must be string values")
        return cls(
            expect_string(data.get("operation_id"), "project metadata operation ID"),
            values,
            tuple(expected.items()),
        )


@dataclass(frozen=True)
class ExecutionState:
    task_id: str
    workflow: str
    agent: str
    modes: tuple[str, ...]
    status: ExecutionStatus
    created_at: str
    updated_at: str
    snapshot_digest: str
    cursor: int
    active_item_id: str | None
    item_executions: tuple[PlanItemExecution, ...]
    steps: tuple[StepProgress, ...]
    # Completed records displaced by a loop reset.  They retain the operation
    # IDs and stream references needed to inspect earlier iterations.
    execution_history: tuple[PlanItemExecution, ...] = ()
    workflow_values: tuple[tuple[str, str], ...] = ()
    pending_input_request: InputRequest | None = None
    last_error: str | None = None
    run_id: str | None = None
    execution_instance_id: str | None = None
    working_directory: str | None = None
    plan_revision: int = 1
    plan_digest: str | None = None
    parent_task_id: str | None = None
    start_operation_id: str | None = None
    workflow_runtime: str = "single"
    model: str = "auto"
    reasoning: str = "auto"
    assignment_item_id: str | None = None
    assignment_model: str | None = None
    assignment_reasoning: str | None = None
    assignment_selected_agent: str | None = None
    assignment_selected_model: str | None = None
    assignment_selected_reasoning: str | None = None
    loop_iterations: tuple[tuple[str, int], ...] = ()
    loop_exit_item_id: str | None = None
    loop_continue_item_id: str | None = None
    # The requirements submitted with start survive preparation hooks and are
    # consumed only by the compiler-owned init item.
    pending_init_artifact: str | None = None
    # Metadata is projected after the source completion has committed.  These
    # intents make a failed projection retryable without early visibility.
    pending_task_metadata: tuple[tuple[str, str | tuple[str, ...]], ...] = ()
    pending_project_metadata: ProjectMetadataPublication | None = None
    # The operator said they are done for now.  The agent stops until they
    # return; any further word or answer of theirs lifts it.
    operator_paused: bool = False
    # Why a ``failed`` run failed when that is not the item's own error.
    failure_kind: FailureKind | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXECUTION_SCHEMA_VERSION,
            "task_id": self.task_id,
            "workflow": self.workflow,
            "agent": self.agent,
            "modes": list(self.modes),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "snapshot_digest": self.snapshot_digest,
            "cursor": self.cursor,
            "active_item_id": self.active_item_id,
            "item_executions": [item.to_dict() for item in self.item_executions],
            "execution_history": [item.to_dict() for item in self.execution_history],
            "steps": [item.to_dict() for item in self.steps],
            "workflow_values": dict(self.workflow_values),
            "pending_input_request": self.pending_input_request.to_dict()
            if self.pending_input_request
            else None,
            "last_error": self.last_error,
            "run_id": self.run_id,
            "execution_instance_id": self.execution_instance_id,
            "working_directory": self.working_directory,
            "plan_revision": self.plan_revision,
            "plan_digest": self.plan_digest,
            "parent_task_id": self.parent_task_id,
            "start_operation_id": self.start_operation_id,
            "workflow_runtime": self.workflow_runtime,
            "model": self.model,
            "reasoning": self.reasoning,
            "assignment_item_id": self.assignment_item_id,
            "assignment_model": self.assignment_model,
            "assignment_reasoning": self.assignment_reasoning,
            "assignment_selected_agent": self.assignment_selected_agent,
            "assignment_selected_model": self.assignment_selected_model,
            "assignment_selected_reasoning": self.assignment_selected_reasoning,
            "loop_iterations": dict(self.loop_iterations),
            "loop_exit_item_id": self.loop_exit_item_id,
            "loop_continue_item_id": self.loop_continue_item_id,
            "pending_init_artifact": self.pending_init_artifact,
            "pending_task_metadata": _metadata_leaves_to_dict(
                self.pending_task_metadata
            ),
            "pending_project_metadata": self.pending_project_metadata.to_dict()
            if self.pending_project_metadata
            else None,
            "operator_paused": self.operator_paused,
            "failure_kind": self.failure_kind,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ExecutionState:
        if not isinstance(data, dict):
            raise ValueError("execution state must be a mapping")
        schema_version = data.get("schema_version")
        if schema_version != EXECUTION_SCHEMA_VERSION:
            raise ValueError(f"unsupported execution state schema: {schema_version!r}")
        required = {
            "schema_version",
            "task_id",
            "workflow",
            "agent",
            "modes",
            "status",
            "created_at",
            "updated_at",
            "snapshot_digest",
            "cursor",
            "active_item_id",
            "item_executions",
            "steps",
            "workflow_values",
            "pending_input_request",
            "last_error",
        }
        require_keys(data, required, "execution state")
        if not isinstance(data["modes"], list) or not all(
            isinstance(item, str) for item in data["modes"]
        ):
            raise ValueError("modes must be a list of strings")
        if not isinstance(data["item_executions"], list) or not isinstance(
            data["steps"], list
        ):
            raise ValueError("execution records and steps must be lists")
        task_id = expect_string(data["task_id"], "task ID")
        run_id = expect_optional_string(data.get("run_id"), "run ID")
        raw_items = data["item_executions"]
        item_executions = tuple(PlanItemExecution.from_dict(item) for item in raw_items)
        raw_history = data.get("execution_history", [])
        if not isinstance(raw_history, list):
            raise ValueError("execution history must be a list")
        execution_history = tuple(
            PlanItemExecution.from_dict(item) for item in raw_history
        )
        return cls(
            task_id=task_id,
            workflow=expect_string(data["workflow"], "workflow"),
            agent=expect_string(data["agent"], "agent"),
            modes=tuple(data["modes"]),
            status=expect_literal(
                data["status"], ExecutionStatus, "execution state.status"
            ),
            created_at=expect_string(data["created_at"], "created_at"),
            updated_at=expect_string(data["updated_at"], "updated_at"),
            snapshot_digest=expect_string(data["snapshot_digest"], "snapshot digest"),
            cursor=expect_nonnegative_int(data["cursor"], "cursor"),
            active_item_id=expect_optional_string(
                data["active_item_id"], "active item ID"
            ),
            item_executions=item_executions,
            execution_history=execution_history,
            steps=tuple(StepProgress.from_dict(item) for item in data["steps"]),
            workflow_values=_variables(data["workflow_values"], "workflow values"),
            pending_input_request=InputRequest.from_dict(data["pending_input_request"])
            if data["pending_input_request"] is not None
            else None,
            last_error=expect_optional_string(data["last_error"], "last error"),
            run_id=run_id,
            execution_instance_id=expect_optional_string(
                data.get("execution_instance_id"), "execution instance ID"
            ),
            working_directory=expect_optional_string(
                data.get("working_directory"), "working directory"
            ),
            plan_revision=expect_positive_int(
                data.get("plan_revision", 1), "plan revision"
            ),
            plan_digest=expect_optional_string(data.get("plan_digest"), "plan digest"),
            parent_task_id=expect_optional_string(
                data.get("parent_task_id"), "parent task ID"
            ),
            start_operation_id=expect_optional_string(
                data.get("start_operation_id"), "start operation ID"
            ),
            workflow_runtime=expect_string(
                data.get("workflow_runtime", "single"), "workflow runtime"
            ),
            model=expect_string(data.get("model", "auto"), "model"),
            reasoning=expect_string(data.get("reasoning", "auto"), "reasoning"),
            assignment_item_id=expect_optional_string(
                data.get("assignment_item_id"), "assignment item ID"
            ),
            assignment_model=expect_optional_string(
                data.get("assignment_model"), "assignment model"
            ),
            assignment_reasoning=expect_optional_string(
                data.get("assignment_reasoning"), "assignment reasoning"
            ),
            assignment_selected_agent=expect_optional_string(
                data.get("assignment_selected_agent"), "assignment selected agent"
            ),
            assignment_selected_model=expect_optional_string(
                data.get("assignment_selected_model"), "assignment selected model"
            ),
            assignment_selected_reasoning=expect_optional_string(
                data.get("assignment_selected_reasoning"),
                "assignment selected reasoning",
            ),
            loop_iterations=_positive_int_mapping(
                data.get("loop_iterations", {}), "execution state.loop_iterations"
            ),
            loop_exit_item_id=expect_optional_string(
                data.get("loop_exit_item_id"), "execution state.loop_exit_item_id"
            ),
            loop_continue_item_id=expect_optional_string(
                data.get("loop_continue_item_id"),
                "execution state.loop_continue_item_id",
            ),
            pending_init_artifact=expect_optional_string(
                data.get("pending_init_artifact"),
                "execution state.pending_init_artifact",
            ),
            pending_task_metadata=_metadata_leaves(
                data.get("pending_task_metadata", {}), "pending task metadata"
            ),
            pending_project_metadata=ProjectMetadataPublication.from_dict(
                data["pending_project_metadata"]
            )
            if data.get("pending_project_metadata") is not None
            else None,
            operator_paused=expect_bool(
                data.get("operator_paused", False), "operator paused"
            ),
            failure_kind=(
                None
                if data.get("failure_kind") is None
                else expect_literal(
                    data["failure_kind"], FailureKind, "execution state.failure_kind"
                )
            ),
        )
