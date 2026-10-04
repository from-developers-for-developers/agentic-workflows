# SPDX-License-Identifier: GPL-3.0-or-later
"""Core workflow-control operations embedded in compiled plans.

This is a leaf module: it depends only on ``ww.contracts`` so that the
engine, the parser, the validator, and the plan compiler can all import it
without pulling in the ``ww.plan`` package.

Loops, workflow handoffs, and child-workflow runs are behaviour of the workflow
engine itself.  Core code plans, persists, and dispatches them directly; they
are deliberately not part of the ordinary action registry and cannot be added
or replaced through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeAlias

from ww.contracts import ExecutionKind, LoopOperation, PlanItemOwner
from ww.validation import expect_keys, is_positive_int

if TYPE_CHECKING:
    from ww.actions.contracts import PlannedAction


@dataclass(frozen=True)
class LoopBoundary:
    """Manager-owned entry or repeat boundary generated for a ``loop`` step."""

    kind: ClassVar[str] = "loop"
    owner: ClassVar[PlanItemOwner] = "ww"
    execution: ClassVar[ExecutionKind] = "loop_control"

    loop_id: str
    boundary: LoopOperation
    max_times: int

    def __post_init__(self) -> None:
        if not isinstance(self.loop_id, str) or not self.loop_id.strip():
            raise ValueError("loop boundary requires a non-empty loop ID")
        if self.boundary not in {"enter", "repeat"}:
            raise ValueError("loop boundary must be enter or repeat")
        if not is_positive_int(self.max_times):
            raise ValueError("loop boundary requires a positive integer limit")

    def to_dict(self) -> dict[str, object]:
        return {
            "loop_id": self.loop_id,
            "boundary": self.boundary,
            "max_times": self.max_times,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoopBoundary:
        expect_keys(data, {"loop_id", "boundary", "max_times"}, f"{cls.kind} operation")
        return cls(data["loop_id"], data["boundary"], data["max_times"])


@dataclass(frozen=True)
class WorkflowHandoff:
    """Terminal transition of a handoff workflow into its selected successor."""

    kind: ClassVar[str] = "workflow_transition"
    owner: ClassVar[PlanItemOwner] = "ww"
    execution: ClassVar[ExecutionKind] = "workflow_transition"

    target: str

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not self.target.strip():
            raise ValueError("workflow handoff requires a non-empty target")

    def to_dict(self) -> dict[str, object]:
        return {"target": self.target}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowHandoff:
        expect_keys(data, {"target"}, f"{cls.kind} operation")
        return cls(data["target"])


CHILD_LAUNCH_SETTINGS = ("workflow", "runtime", "model", "reasoning", "agent")


@dataclass(frozen=True)
class ChildLaunch:
    """The launch settings ww applies when it starts a child itself.

    Each setting is a template rendered, when the stage runs, from the child's
    record (``{{ww.child.field.<name>}}`` and the like).  ``None`` or a value
    that renders empty inherits, exactly as an omitted ``start-child`` option.
    """

    workflow: str | None = None
    runtime: str | None = None
    model: str | None = None
    reasoning: str | None = None
    agent: str | None = None

    @property
    def templates(self) -> tuple[str, ...]:
        return tuple(
            value
            for name in CHILD_LAUNCH_SETTINGS
            if isinstance(value := getattr(self, name), str)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            name: value
            for name in CHILD_LAUNCH_SETTINGS
            if (value := getattr(self, name)) is not None
        }

    @classmethod
    def from_dict(cls, data: object) -> ChildLaunch:
        if not isinstance(data, dict) or not all(
            key in CHILD_LAUNCH_SETTINGS and isinstance(value, str)
            for key, value in data.items()
        ):
            raise ValueError("child launch must map launch settings to strings")
        return cls(**data)


@dataclass(frozen=True)
class ChildWorkflowRun:
    """Coordinator item that runs the named workflow once per collected child.

    With ``launch`` ww starts the child itself when the item is reached.
    """

    kind: ClassVar[str] = "child_workflow"
    owner: ClassVar[PlanItemOwner] = "ww"
    execution: ClassVar[ExecutionKind] = "agent_instruction"

    workflow: str
    launch: ChildLaunch | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workflow, str) or not self.workflow.strip():
            raise ValueError("child workflow requires a non-empty target")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"workflow": self.workflow}
        if self.launch is not None:
            data["launch"] = self.launch.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChildWorkflowRun:
        expect_keys(data, {"workflow"}, f"{cls.kind} operation")
        launch = data.get("launch")
        return cls(
            data["workflow"],
            ChildLaunch.from_dict(launch) if launch is not None else None,
        )


CoreOperation: TypeAlias = "LoopBoundary | WorkflowHandoff | ChildWorkflowRun"
PlanOperation: TypeAlias = "PlannedAction | CoreOperation"

_CORE_OPERATIONS: dict[str, type[LoopBoundary | WorkflowHandoff | ChildWorkflowRun]] = {
    LoopBoundary.kind: LoopBoundary,
    WorkflowHandoff.kind: WorkflowHandoff,
    ChildWorkflowRun.kind: ChildWorkflowRun,
}
_ACTION_TYPE = "action"


def encode_operation(operation: PlanOperation) -> dict[str, object]:
    """Serialize a plan operation with an explicit ``type`` discriminator."""
    # Runtime import: ``ww.actions`` must stay independent of core operations.
    from ww.actions.contracts import PlannedAction

    if isinstance(operation, PlannedAction):
        return {"type": _ACTION_TYPE, **operation.to_dict()}
    return {"type": operation.kind, **operation.to_dict()}


def decode_operation(data: object) -> PlanOperation:
    """Rebuild a plan operation from its serialized form."""
    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        raise ValueError("plan operation must declare a type")
    fields = {key: value for key, value in data.items() if key != "type"}
    if data["type"] == _ACTION_TYPE:
        from ww.actions.contracts import PlannedAction

        return PlannedAction.from_dict(fields)
    try:
        operation_type = _CORE_OPERATIONS[data["type"]]
    except KeyError:
        raise ValueError(f"unknown plan operation type: {data['type']!r}") from None
    return operation_type.from_dict(fields)
