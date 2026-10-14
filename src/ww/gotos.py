# SPDX-License-Identifier: GPL-3.0-or-later
"""Taking a goto: sending the flow back to a step already run, or ahead.

A step's worker asks for a goto with ``complete --goto <step>``, or an
assessment outcome declares one. The step that asked finishes first, its
completion hooks included; then ww takes the goto here. Going back, every
item from the target to the asking step runs again: their records start
over and the earlier ones stay in the run's history, so nothing done before
is lost, and their artifacts of the new round are kept apart. Going ahead,
the items between are skipped. Either way the run records the goto, and the
pages of the steps it reaches say so.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ww.errors import StateError
from ww.execution_models import (
    ExecutionState,
    Goto,
    GotoRequest,
    PlanItemExecution,
    new_item_execution,
    operation_scope_for,
)
from ww.plan import PlanItem, PlannedGoto, WorkflowPlan
from ww.plan.gotos import DETAIL_REFERENCE, GOTO_FROM, GOTO_SUMMARY, step_span

Clock = Callable[[], str]


def allowed_targets(plan: WorkflowPlan, source: str) -> tuple[str, ...]:
    """Where ``source``'s worker may send the flow with ``complete --goto``."""
    return tuple(
        goto.target
        for goto in plan.gotos
        if goto.outcome is None and goto.source == source
    )


def outcome_goto(plan: WorkflowPlan, source: str, outcome: str) -> PlannedGoto | None:
    """The goto the assessment at ``source`` takes for ``outcome``, if any."""
    return next(
        (
            goto
            for goto in plan.gotos
            if goto.source == source and goto.outcome == outcome
        ),
        None,
    )


def request_goto(plan: WorkflowPlan, item: PlanItem, target: str) -> GotoRequest:
    """The goto ``complete --goto`` asks of ``item``'s step, once allowed."""
    allowed = allowed_targets(plan, item.step)
    if target not in allowed:
        raise StateError(
            f"{item.step!r} may not go to {target!r}; "
            + (
                "it may go to " + ", ".join(repr(entry) for entry in allowed)
                if allowed
                else "the workflow's allowed_gotos lists no goto from it"
            )
        )
    return GotoRequest(item.step, target)


def goto_due(state: ExecutionState, plan: WorkflowPlan) -> bool:
    """Whether the step that asked for the pending goto has finished."""
    request = state.pending_goto
    if request is None:
        return False
    span = step_span(plan.items, request.source)
    if span is None:  # pragma: no cover - the plan checked the source
        raise StateError(f"goto source {request.source!r} is not in the plan")
    return state.cursor > span[1]


def taken(state: ExecutionState, source: str, target: str) -> int:
    """How often the run took the goto from ``source`` to ``target``."""
    return sum(
        1 for goto in state.gotos if (goto.source, goto.target) == (source, target)
    )


def limit_reached(state: ExecutionState, limit: int) -> bool:
    request = state.pending_goto
    return request is not None and taken(state, request.source, request.target) >= limit


def stop_at_limit(state: ExecutionState, limit: int, now: Clock) -> ExecutionState:
    """Stop for the operator: the pending goto's edge ran ``limit`` times."""
    request = state.pending_goto
    assert request is not None  # callers check ``limit_reached``
    return replace(
        state,
        status="failed",
        failure_kind="goto_limit",
        active_item_id=None,
        last_error=(
            f"{request.source!r} asked to go to {request.target!r} again, but "
            f"that goto already ran {limit} times (limits.gotos)."
        ),
        updated_at=now(),
    )


def take_goto(state: ExecutionState, plan: WorkflowPlan, now: Clock) -> ExecutionState:
    """Take the pending goto: run the steps again from its target, or skip ahead."""
    request = state.pending_goto
    if request is None:  # pragma: no cover - callers check ``goto_due``
        raise StateError("no goto is pending")
    target = step_span(plan.items, request.target)
    source = step_span(plan.items, request.source)
    if target is None or source is None:  # pragma: no cover - the plan checked
        raise StateError(f"goto {request.source!r} -> {request.target!r} left the plan")
    records = list(state.item_executions)
    asker = _asking_record(plan, records, request.source)
    summary = asker.summary_for_next if asker is not None else None
    number = len(state.gotos) + 1
    goto = Goto(
        source=request.source,
        target=request.target,
        direction="back" if target[0] <= source[0] else "forward",
        round=taken(state, request.source, request.target) + 1,
        text=(
            DETAIL_REFERENCE.sub(
                lambda found: {
                    GOTO_SUMMARY: summary or "",
                    GOTO_FROM: request.source,
                }.get(found.group(0).replace(" ", ""), found.group(0)),
                request.detail,
            )
            if request.detail is not None
            else summary
        ),
        artifact=asker.artifact if asker is not None else None,
        at=now(),
    )
    history = list(state.execution_history)
    if goto.direction == "back":
        scope = operation_scope_for(state)
        for index in range(target[0], source[1] + 1):
            earlier = records[index]
            if earlier.status != "pending":
                history.append(earlier)
            round_ = earlier.goto_round + 1
            # A fresh operation ID per round: the earlier round's command
            # streams and external operations stay its own.
            records[index] = replace(
                new_item_execution(
                    state.task_id, f"{scope}:goto-{round_}", plan.items[index]
                ),
                goto_round=round_,
                goto=number,
            )
    else:
        for index in range(source[1] + 1, target[0]):
            records[index] = replace(
                records[index],
                status="completed",
                completed_at=now(),
                result=f"skipped: goto from {request.source} to {request.target}",
            )
        for index in range(target[0], target[1] + 1):
            records[index] = replace(records[index], goto=number)
    return replace(
        state,
        status="pending",
        cursor=target[0],
        active_item_id=None,
        item_executions=tuple(records),
        execution_history=tuple(history),
        gotos=(*state.gotos, goto),
        pending_goto=None,
        last_error=None,
        failure_kind=None,
        assignment_item_id=None,
        assignment_token=None,
        assignment_model=None,
        assignment_reasoning=None,
        assignment_selected_agent=None,
        assignment_selected_model=None,
        assignment_selected_reasoning=None,
        updated_at=now(),
    )


def drop_goto(state: ExecutionState, now: Clock) -> ExecutionState:
    """Leave the pending goto untaken; the run continues after its step."""
    return replace(
        state,
        status="pending",
        pending_goto=None,
        last_error=None,
        failure_kind=None,
        updated_at=now(),
    )


def _asking_record(
    plan: WorkflowPlan, records: list[PlanItemExecution], source: str
) -> PlanItemExecution | None:
    """The record of the agent item of the step that asked for the goto."""
    return next(
        (
            records[index]
            for index, item in enumerate(plan.items)
            if item.step == source
            and item.phase == "step"
            and item.owner == "agent"
            and item.verifies is None
        ),
        None,
    )
