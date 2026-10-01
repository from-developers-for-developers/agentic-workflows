# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the pure workflow transition boundary."""

from __future__ import annotations

from dataclasses import replace

from ww.actions import CommandDefinition, Commands, PlannedAction, Prompt
from ww.execution_models import (
    PLAN_SCHEMA_VERSION,
    ExecutionState,
    PlanSnapshot,
    initial_state,
)
from ww.plan import PlanItem, WorkflowPlan
from ww.transitions import (
    begin_agent_item,
    complete_agent_item,
    fail_agent_item,
    interrupt_automatic_item,
    resume_interrupted_item,
    retry_failed_item,
    settle_stale_automatic_item,
)

NOW = "2026-09-09T12:00:00Z"


def _now() -> str:
    return NOW


def _item(**changes: object) -> PlanItem:
    fields: dict[str, object] = {
        "id": "workflow:step:step:work:1",
        "position": 1,
        "name": "work",
        "description": "",
        "operation": PlannedAction("prompt", Prompt("Do the work.")),
        "owner": "agent",
        "execution": "agent_instruction",
        "requires_agent_input": False,
        "workflow": "workflow",
        "step": "work",
        "parent": None,
        "phase": "step",
        "source": "step",
        "registered_handler": None,
    }
    fields.update(changes)
    return PlanItem(**fields)  # type: ignore[arg-type]


def _run(item: PlanItem) -> tuple[WorkflowPlan, ExecutionState]:
    plan = WorkflowPlan(
        workflow="workflow",
        workflow_description="",
        agent="codex",
        task_id="TASK-1",
        modes=(),
        handoff=False,
        items=(item,),
    )
    snapshot = PlanSnapshot(
        schema_version=PLAN_SCHEMA_VERSION,
        compiler_version="test",
        configuration_digest="digest",
        compiled_at=NOW,
        plan=plan,
    )
    return plan, initial_state(snapshot, (), NOW, run_id="01-workflow")


def test_agent_lifecycle_updates_item_run_and_step_together() -> None:
    item = _item()
    plan, state = _run(item)

    state = begin_agent_item(
        state, plan, item, model="gpt-test", reasoning="high", now=_now
    )
    assert state.status == "in_progress"
    assert state.active_item_id == item.id
    assert state.item_executions[0].status == "in_progress"
    assert state.item_executions[0].attempts == 1
    assert state.item_executions[0].model == "gpt-test"
    assert state.steps[0].status == "in_progress"

    state = fail_agent_item(state, plan, item, "broken", _now)
    assert state.status == "failed"
    assert state.last_error == "agent item 'work' failed: broken"
    assert state.item_executions[0].error == state.last_error
    assert state.steps[0].status == "failed"

    state = retry_failed_item(state, plan, _now)
    assert state.status == "pending"
    assert state.active_item_id is None
    assert state.last_error is None
    assert state.item_executions[0].status == "pending"
    assert state.item_executions[0].error is None

    state = begin_agent_item(
        state, plan, item, model="gpt-test", reasoning="high", now=_now
    )
    state = complete_agent_item(state, plan, {"answer": "done"}, "artifact.md", _now)
    assert state.status == "pending"
    assert state.cursor == 1
    assert state.active_item_id is None
    assert dict(state.workflow_values) == {"answer": "done"}
    assert state.item_executions[0].status == "completed"
    assert state.item_executions[0].attempts == 2
    assert state.item_executions[0].artifact == "artifact.md"
    assert state.steps[0].status == "completed"


def test_interrupted_automatic_item_preserves_one_recovery_boundary() -> None:
    item = _item(
        operation=PlannedAction(
            "cli", Commands((CommandDefinition(argv=("example",)),))
        ),
        owner="ww",
        execution="automatic",
    )
    plan, state = _run(item)
    command = replace(state.item_executions[0].commands[0], status="in_progress")
    record = replace(
        state.item_executions[0], status="in_progress", commands=(command,)
    )
    state = replace(
        state,
        status="in_progress",
        active_item_id=item.id,
        item_executions=(record,),
    )

    state = interrupt_automatic_item(state, plan, item, _now)
    assert state.status == "interrupted"
    assert state.item_executions[0].status == "interrupted"
    assert state.item_executions[0].commands[0].status == "interrupted"
    assert state.item_executions[0].error == state.last_error

    state = resume_interrupted_item(state, plan, item, now=_now)
    assert state.status == "pending"
    assert state.active_item_id == item.id
    assert state.last_error is None
    assert state.item_executions[0].status == "pending"
    assert state.item_executions[0].commands[0].status == "pending"


def _stale_automatic_run(
    **command_changes: object,
) -> tuple[PlanItem, WorkflowPlan, ExecutionState]:
    item = _item(
        operation=PlannedAction(
            "cli", Commands((CommandDefinition(argv=("example",)),))
        ),
        owner="ww",
        execution="automatic",
    )
    plan, state = _run(item)
    command = replace(state.item_executions[0].commands[0], **command_changes)
    record = replace(
        state.item_executions[0], status="in_progress", commands=(command,)
    )
    state = replace(
        state, status="in_progress", active_item_id=item.id, item_executions=(record,)
    )
    return item, plan, state


def test_a_stale_segment_that_recorded_its_exit_is_a_known_failure() -> None:
    item, plan, state = _stale_automatic_run(
        status="failed", exit_code=3, stderr="boom\n"
    )

    state = settle_stale_automatic_item(state, plan, item, _now)

    assert state.status == "failed"
    assert state.item_executions[0].status == "failed"
    assert state.item_executions[0].commands[0].status == "failed"
    assert state.last_error is not None
    assert "failed (3) at command 1" in state.last_error
    assert "before it recorded the failure" in state.last_error
    assert state.last_error.endswith("boom")
    assert state.steps[0].status == "failed"


def test_a_stale_segment_still_running_stays_an_unknown_outcome() -> None:
    item, plan, state = _stale_automatic_run(status="in_progress")

    state = settle_stale_automatic_item(state, plan, item, _now)

    assert state.status == "interrupted"
    assert state.item_executions[0].commands[0].status == "interrupted"
