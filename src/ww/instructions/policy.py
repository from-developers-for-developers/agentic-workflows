# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure execution-state interpretation for instructions."""

from __future__ import annotations

from enum import Enum

from ww.actions import actions
from ww.assignments import active_assignment, input_only
from ww.contracts import (
    Control,
    InstructionStatus,
    ItemStatus,
    NextRole,
    OperatorReason,
    PlanItemKind,
)
from ww.control import child_workflow, loop_control, replays_harmlessly
from ww.errors import StateError
from ww.execution_models import ExecutionState
from ww.plan import WorkflowPlan
from ww.transitions import loop_limit_reached
from ww.validation import expect_literal

from .models import Instruction


class Audience(Enum):
    """Who reads an instruction and what they are expected to do with it."""

    SINGLE_SESSION = "single"
    """One session plays both roles and does the work itself."""
    MANAGER_DELEGATING = "manager_delegating"
    """The manager hands the next assignment to a worker."""
    WORKER_RETURNING = "worker_returning"
    """A worker has finished its assignment and returns control."""
    WORKER = "worker"
    """A worker performs the active assignment."""
    MANAGER = "manager"
    """The manager runs the next command itself."""


def audience(instruction: Instruction) -> Audience:
    if instruction.workflow_runtime == "single":
        return Audience.SINGLE_SESSION
    if instruction.caller_role == "manager" and instruction.next_role == "worker":
        return Audience.MANAGER_DELEGATING
    if instruction.caller_role == "worker" and instruction.next_role in {
        "manager",
        "operator",
    }:
        return Audience.WORKER_RETURNING
    return Audience.WORKER if instruction.next_role == "worker" else Audience.MANAGER


def _instruction_status(value: str) -> InstructionStatus:
    status: InstructionStatus = expect_literal(
        value, InstructionStatus, "bootstrap request status", error=StateError
    )
    return status


def _item_status(value: str) -> ItemStatus:
    status: ItemStatus = expect_literal(
        value, ItemStatus, "bootstrap request item status", error=StateError
    )
    return status


def _plan_item_kind(value: str) -> PlanItemKind:
    if not actions.contains(value):
        raise StateError(f"bootstrap request has invalid action kind: {value!r}")
    return value


def _has_previous_artifacts(state: ExecutionState, plan: WorkflowPlan) -> bool:
    return any(
        record.artifact is not None
        for index, (item, record) in enumerate(
            zip(plan.items, state.item_executions, strict=True)
        )
        if index < state.cursor and record.status == "completed"
    )


def _next_steps(state: ExecutionState, plan: WorkflowPlan) -> tuple[str, ...]:
    current = plan.items[state.cursor]
    if current.phase != "step":
        return ()
    return tuple(
        dict.fromkeys(
            item.step
            for item in plan.items[state.cursor + 1 :]
            if item.phase == "step"
            and item.parent == current.parent
            and item.step != current.step
        )
    )


def _index_for_id(plan: WorkflowPlan, item_id: str) -> int:
    for index, item in enumerate(plan.items):
        if item.id == item_id:
            return index
    raise StateError(f"plan item {item_id!r} is not in the task snapshot")


def operator_reason(state: ExecutionState, plan: WorkflowPlan) -> OperatorReason | None:
    """Why the task waits for the operator, or ``None`` when it does not."""
    item = plan.items[state.cursor] if state.cursor < len(plan.items) else None
    if state.status == "interrupted":
        # ``next`` replays a harmless handler without asking anyone.
        if item is not None and replays_harmlessly(item):
            return None
        return "interrupted_command"
    if state.status == "failed":
        if item is not None and child_workflow(item) is not None:
            return "child_failed"
        if item is not None and item.owner == "agent":
            return "work_failed"
        return "handler_failed"
    if (
        state.status != "awaiting_input"
        and item is not None
        and loop_limit_reached(state, item)
    ):
        return "loop_limit"
    return None


def _control(state: ExecutionState, plan: WorkflowPlan) -> tuple[Control, NextRole]:
    if operator_reason(state, plan) is not None:
        return "awaiting_operator", "operator"
    if state.status in {"failed", "interrupted"}:
        return "blocked", "manager"
    if state.status in {"completed", "abandoned"}:
        return "handoff_manager", "manager"
    if state.status == "awaiting_input":
        assignment = active_assignment(
            plan, state.assignment_item_id, runtime=state.workflow_runtime
        )
        if (
            state.workflow_runtime == "auto"
            and assignment is not None
            and input_only(plan, assignment)
        ):
            return "handoff_manager", "manager"
        return "continue_worker", "worker"
    if state.cursor < len(plan.items):
        item = plan.items[state.cursor]
        record = state.item_executions[state.cursor]
        if (
            item.owner == "ww"
            and item.execution == "automatic"
            and record.status == "in_progress"
        ):
            return "blocked", "manager"
    if state.cursor < len(plan.items) and (
        child_workflow(plan.items[state.cursor]) is not None
        or loop_control(plan.items[state.cursor]) is not None
    ):
        return "blocked", "manager"
    assignment = active_assignment(
        plan, state.assignment_item_id, runtime=state.workflow_runtime
    )
    if assignment is not None and state.cursor < assignment.stop:
        return "continue_worker", "worker"
    if state.active_item_id and state.cursor < len(plan.items):
        return "continue_worker", "worker"
    return "handoff_manager", "manager"


def _result_saved(state: ExecutionState, plan: WorkflowPlan) -> bool:
    assignment = active_assignment(
        plan, state.assignment_item_id, runtime=state.workflow_runtime
    )
    start = assignment.start if assignment is not None else 0
    stop = min(state.cursor + 1, len(plan.items))
    return any(
        plan.items[index].owner == "agent"
        and state.item_executions[index].status == "completed"
        for index in range(start, stop)
    )
