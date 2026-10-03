# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent agent repairs of known failures of automated command items."""

from __future__ import annotations

from dataclasses import replace

from ww.execution_models import ExecutionState
from ww.plan import PlanItem, WorkflowPlan
from ww.transitions import Clock, project_steps


def needs_repair(state: ExecutionState) -> bool:
    return (
        state.cursor < len(state.item_executions)
        and state.item_executions[state.cursor].repair_pending
    )


def close_assignment(state: ExecutionState) -> ExecutionState:
    """End the current worker's authority before dispatching another assignment."""
    return replace(
        state,
        assignment_item_id=None,
        assignment_token=None,
        assignment_model=None,
        assignment_reasoning=None,
        assignment_selected_agent=None,
        assignment_selected_model=None,
        assignment_selected_reasoning=None,
    )


def request_repair(
    state: ExecutionState, plan: WorkflowPlan, item: PlanItem, now: Clock
) -> ExecutionState:
    """Keep the failed command and its evidence in place while a worker fixes it."""
    records = list(state.item_executions)
    record = records[state.cursor]
    failures = record.repair_failures + 1
    records[state.cursor] = replace(
        record, repair_pending=True, repair_failures=failures
    )
    limited = failures >= item.max_handler_fixes
    active = not limited and state.workflow_runtime == "single"
    return project_steps(
        replace(
            close_assignment(state),
            item_executions=tuple(records),
            status="failed" if limited else "in_progress" if active else "pending",
            active_item_id=item.id if active else None,
            failure_kind="fix_limit" if limited else None,
            updated_at=now(),
        ),
        plan,
        now,
    )
