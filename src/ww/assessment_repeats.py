# SPDX-License-Identifier: GPL-3.0-or-later
"""Local assessment rounds and the exact messages their agents exchange.

WW owns the step lifecycle. The host owns agent sessions: pages identify the
session to retain and relay messages without rewriting them, whether the
assessment agent or its manager launches the outcome worker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ww.control import is_coordinator
from ww.errors import ConfigurationError
from ww.execution_models import (
    ExecutionState,
    new_item_execution,
    operation_scope_for,
)
from ww.plan import WorkflowPlan
from ww.plan.gotos import step_span

Clock = Callable[[], str]


def validate_repeats(plan: WorkflowPlan) -> None:
    for item in plan.items:
        if item.repeat_until is None:
            continue
        span = step_span(plan.items, item.step)
        assert span is not None
        for nested in plan.items[span[0] : span[1] + 1]:
            if nested.id != item.id and nested.assessment_question is not None:
                raise ConfigurationError(
                    f"{item.step}.repeat_until cannot contain another assess"
                )
            if (
                is_coordinator(nested)
                or nested.child_operation is not None
                or (nested.item_operation == "collect")
            ):
                raise ConfigurationError(
                    f"{item.step}.repeat_until cannot repeat task launches or "
                    "collection steps; use ordinary outcome steps and handlers"
                )
            if nested.assessment_parent == item.step and (
                nested.owner == "agent" and nested.role == "manager"
            ):
                raise ConfigurationError(
                    f"{item.step}.repeat_until outcome work requires role: worker "
                    "so auto can delegate it separately"
                )
        # Each assessment round needs a durable findings artifact.
        if not item.artifact:
            raise ConfigurationError(
                f"{item.step}.repeat_until requires artifact: true"
            )
        if any(
            edge.source == item.step
            or edge.source.startswith(item.step + "/")
            or edge.target == item.step
            or edge.target.startswith(item.step + "/")
            for edge in plan.gotos
        ):
            raise ConfigurationError(
                f"{item.step}.repeat_until cannot combine its local rounds with goto"
            )


def repeat_owner(plan: WorkflowPlan, index: int) -> int | None:
    """The innermost repeating assessment containing this plan position."""
    owners = []
    for position, item in enumerate(plan.items):
        if item.repeat_until is not None:
            span = step_span(plan.items, item.step)
            if span is not None and span[0] <= index <= span[1]:
                owners.append(position)
    return max(owners, default=None)


def repeat_due(state: ExecutionState, plan: WorkflowPlan) -> int | None:
    """The innermost completed nonterminal round, after all its hooks."""
    for index in reversed(range(len(plan.items))):
        item = plan.items[index]
        record = state.item_executions[index]
        if (
            item.repeat_until is None
            or record.assessment_outcome is None
            or record.assessment_outcome == item.repeat_until
            or record.repeat_waived is not None
        ):
            continue
        span = step_span(plan.items, item.step)
        assert span is not None
        if state.cursor > span[1] and all(
            entry.status == "completed"
            for entry in state.item_executions[span[0] : span[1] + 1]
        ):
            return index
    return None


def stop_at_limit(
    state: ExecutionState, plan: WorkflowPlan, index: int, limit: int, now: Clock
) -> ExecutionState:
    return replace(
        state,
        status="failed",
        failure_kind="assessment_limit",
        active_item_id=None,
        last_error=(
            f"{plan.items[index].step!r} has not reached "
            f"{plan.items[index].repeat_until!r} after "
            f"{state.item_executions[index].repeat_round + 1} rounds "
            f"(limits.assessments: {limit}). The latest findings and "
            "outcome results are saved."
        ),
        updated_at=now(),
    )


def take_repeat(
    state: ExecutionState, plan: WorkflowPlan, index: int, now: Clock
) -> ExecutionState:
    """Reset only this assessment's lifecycle; keep earlier rounds and session."""
    span = step_span(plan.items, plan.items[index].step)
    assert span is not None
    records = list(state.item_executions)
    history = list(state.execution_history)
    scope = operation_scope_for(state)
    # Values produced in this round must not leak into its next round.
    provided = {
        name
        for item in plan.items[span[0] : span[1] + 1]
        for name in (*(variable.name for variable in item.provide), *item.outputs)
    }
    for position in range(span[0], span[1] + 1):
        earlier = records[position]
        if earlier.status != "pending":
            history.append(earlier)
        generation = earlier.goto_round + 1
        records[position] = replace(
            new_item_execution(
                state.task_id, f"{scope}:repeat-{generation}", plan.items[position]
            ),
            goto_round=generation,
            repeat_round=earlier.repeat_round + 1 if position == index else 0,
            repeat_session=(
                earlier.repeat_session or earlier.operation_id
                if position == index
                else None
            ),
        )
    return replace(
        state,
        status="pending",
        cursor=span[0],
        active_item_id=None,
        item_executions=tuple(records),
        execution_history=tuple(history),
        workflow_values=tuple(
            (name, value)
            for name, value in state.workflow_values
            if name not in provided
        ),
        assignment_item_id=None,
        assignment_token=None,
        assignment_model=None,
        assignment_reasoning=None,
        assignment_selected_agent=None,
        assignment_selected_model=None,
        assignment_selected_reasoning=None,
        failure_kind=None,
        last_error=None,
        updated_at=now(),
    )


def communication(
    state: ExecutionState,
    plan: WorkflowPlan,
    index: int,
    *,
    generation: int | None = None,
) -> dict[str, object] | None:
    """Stable session identity and verbatim persisted messages for either route."""
    owner = repeat_owner(plan, index)
    if owner is None:
        return None
    item = plan.items[owner]
    record = state.item_executions[owner]
    all_records = (*state.execution_history, *state.item_executions)
    if generation is not None:
        record = next(
            (
                earlier
                for earlier in reversed(all_records)
                if earlier.plan_item_id == item.id and earlier.goto_round == generation
            ),
            record,
        )
    session = record.repeat_session or record.operation_id
    history = state.execution_history
    previous_position = next(
        (
            position
            for position, earlier in reversed(tuple(enumerate(history)))
            if earlier.plan_item_id == item.id
            and (earlier.repeat_session or earlier.operation_id) == session
        ),
        None,
    )
    previous = history[previous_position] if previous_position is not None else None
    findings = record if record.result_text is not None else previous
    span = step_span(plan.items, item.step)
    assert span is not None
    outcome_ids = {
        nested.id
        for nested in plan.items[span[0] : span[1] + 1]
        if nested.step != item.step and nested.phase == "step"
    }
    # Before re-assessment, return exactly the most recent branch results.
    candidates = (
        tuple(
            {
                earlier.plan_item_id: earlier
                for earlier in all_records
                if earlier.goto_round == record.goto_round
            }.values()
        )
        if record.assessment_outcome
        else history[previous_position + 1 :]
        if previous_position is not None
        else ()
    )
    results = []
    for earlier in candidates:
        if earlier.plan_item_id not in outcome_ids or not earlier.attempts:
            continue
        message = (
            earlier.result_text if earlier.result_text is not None else earlier.result
        )
        if message is not None:
            results.append(
                {
                    "item_id": earlier.plan_item_id,
                    "text": message,
                    "artifact": earlier.artifact,
                }
            )
    return {
        "step": item.step,
        "session": session,
        "round": record.repeat_round + 1,
        "until": item.repeat_until,
        "phase": "assessment" if plan.items[index].step == item.step else "outcome",
        "findings": findings.result_text if findings else None,
        "findings_artifact": findings.artifact if findings else None,
        "results": results,
        "delegation_required": state.workflow_runtime == "auto",
    }
