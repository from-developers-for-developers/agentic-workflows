# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure, named state transitions for workflow execution.

The lifecycle service decides when a transition is allowed and when it is
committed.  This module owns the corresponding immutable record changes so the
item, run, and step-projection invariants are changed together.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace

from ww.assessments import outcome_region, pending_assessment
from ww.children import ChildTask
from ww.contracts import StepStatus
from ww.control import loop_control
from ww.errors import StateError
from ww.execution_models import (
    PLAN_SCHEMA_VERSION,
    CheckReport,
    CommandExecution,
    Dispute,
    ExecutionState,
    InputRequest,
    PlanItemExecution,
    PlanSnapshot,
    StepProgress,
    build_step_projection,
    new_item_execution,
    operation_scope_for,
)
from ww.execution_models.records import RuleResolution
from ww.items import WorkItem
from ww.plan import (
    LoopBoundary,
    PlanItem,
    PlannedCheck,
    WorkflowPlan,
    number_step_paths,
)
from ww.workflow_config import ProvidedVariable

Clock = Callable[[], str]


def retry_failed_item(
    state: ExecutionState, plan: WorkflowPlan, now: Clock
) -> ExecutionState:
    """Return the current failed item to pending for another attempt."""
    records = list(state.item_executions)
    values = dict(state.workflow_values)
    if state.cursor < len(records):
        record = records[state.cursor]
        item = plan.items[state.cursor]
        supplied = record.supplied_values
        if item.owner == "ww" and item.execution == "automatic" and item.provide:
            # The handler failed with the values it was given; ask for them
            # again rather than replaying them.  They stay on the record so
            # the request can show what was supplied last time.
            previous = tuple(
                (value.name, values.pop(value.name))
                for value in item.provide
                if value.name in values
            )
            supplied = previous or supplied
        # Commands are replaced in-place when a failed automatic item is
        # retried.  Preserve the prior attempt so its stream references remain
        # discoverable through the public artifact listing.
        history = (*state.execution_history, record)
        records[state.cursor] = replace(
            record,
            status="pending",
            error=None,
            repair_pending=False,
            repair_failures=0
            if state.failure_kind == "fix_limit"
            else record.repair_failures,
            supplied_values=supplied,
            # A retry after the fix limit gives the worker a fresh count; the
            # rejected attempts stay in the history record. A retry after a
            # dispute keeps the count: the check stands.
            check_reports=(
                () if state.failure_kind == "fix_limit" else record.check_reports
            ),
            dispute=None,
        )
    else:
        history = state.execution_history
    return project_steps(
        replace(
            state,
            status="pending",
            active_item_id=None,
            item_executions=tuple(records),
            execution_history=history,
            workflow_values=tuple(values.items()),
            last_error=None,
            failure_kind=None,
            updated_at=now(),
        ),
        plan,
        now,
    )


def waive_checks(
    state: ExecutionState,
    plan: WorkflowPlan,
    waived: tuple[str, ...],
    reason: str,
    now: Clock,
) -> ExecutionState:
    """Return a stopped step to its worker without the ``waived`` checks.

    At the fix limit every check and rule of the step is waived, after a
    dispute the one it named. The next completion skips them, their rules
    are not verified, and its artifact records the waiver with its reason;
    nothing else about the step changes.
    """
    records = list(state.item_executions)
    record = records[state.cursor]
    waivers = dict(record.checks_waived)
    waivers.update(dict.fromkeys(waived, reason))
    records[state.cursor] = replace(
        record,
        status="pending",
        error=None,
        checks_waived=tuple(waivers.items()),
        dispute=None,
    )
    return project_steps(
        replace(
            state,
            status="pending",
            active_item_id=None,
            item_executions=tuple(records),
            last_error=None,
            failure_kind=None,
            updated_at=now(),
        ),
        plan,
        now,
    )


def fix_limits(item: PlanItem, record: PlanItemExecution) -> dict[str, int]:
    """How often each check of an agent item may fail before the operator decides.

    A verifier's verdict on a rule counts under the rule's ID, a derived check
    under its name.
    """
    limits = {rule.id: rule.max_fixes for rule in item.rules}
    limits.update(
        {check.id: check.max_fixes for check in (*item.checks, *record.resolved_checks)}
    )
    return limits


def reject_completion(
    state: ExecutionState,
    plan: WorkflowPlan,
    item: PlanItem,
    report: CheckReport,
    artifact: str | None,
    now: Clock,
    *,
    keep_active: bool = True,
) -> ExecutionState:
    """Record a completion ww refused because checks failed.

    The step stays in progress for its worker to fix, with the supplied
    artifact kept as the draft to revise. A check that has now failed as many
    times as its ``max_fixes`` allows stops the run for the operator instead.
    ``keep_active`` is false when someone other than the step's worker
    learned of the failure, a verifier or the operator's approval: the step
    then waits to be handed back to a worker.
    """
    records = list(state.item_executions)
    record = replace(
        records[state.cursor],
        check_reports=(*records[state.cursor].check_reports, report),
        draft_artifact=artifact,
        held_completion=None,
    )
    limits = fix_limits(item, record)
    exhausted = [
        result.id
        for result in report.failed
        if record.check_failures(result.id) >= limits.get(result.id, 1)
    ]
    if not exhausted and keep_active:
        records[state.cursor] = record
        return replace(state, item_executions=tuple(records), updated_at=now())
    if not exhausted:
        records[state.cursor] = replace(record, status="pending")
        return project_steps(
            replace(
                state,
                status="pending",
                active_item_id=None,
                item_executions=tuple(records),
                assignment_item_id=None,
                assignment_token=None,
                assignment_model=None,
                assignment_reasoning=None,
                assignment_selected_agent=None,
                assignment_selected_model=None,
                assignment_selected_reasoning=None,
                updated_at=now(),
            ),
            plan,
            now,
        )
    message = "check limit reached: " + ", ".join(exhausted)
    records[state.cursor] = replace(record, status="failed", error=message)
    return project_steps(
        replace(
            state,
            status="failed",
            active_item_id=item.id,
            item_executions=tuple(records),
            last_error=message,
            failure_kind="fix_limit",
            updated_at=now(),
        ),
        plan,
        now,
    )


def dispute_check(
    state: ExecutionState, plan: WorkflowPlan, dispute: Dispute, now: Clock
) -> ExecutionState:
    """Stop the run for the operator: the step's worker disputes a check.

    Nothing about the rejections changes; the operator either lets the check
    stand (``next --retry``) or waives it for this step (``next --force``).
    """
    message = f"check disputed: {dispute.check}"
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor], status="failed", error=message, dispute=dispute
    )
    return project_steps(
        replace(
            state,
            status="failed",
            item_executions=tuple(records),
            last_error=message,
            failure_kind="check_disputed",
            updated_at=now(),
        ),
        plan,
        now,
    )


def stop_for_values(
    state: ExecutionState,
    plan: WorkflowPlan,
    item: PlanItem,
    message: str,
    now: Clock,
) -> ExecutionState:
    """Stop the run for the operator: a ``ww.`` value the step reads is missing.

    The step does not start. ``next --retry`` checks the values again, for
    example after the operator created the branch they come from; ``next
    --force`` skips the step.
    """
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor], status="failed", error=message
    )
    return project_steps(
        replace(
            state,
            status="failed",
            active_item_id=item.id,
            item_executions=tuple(records),
            last_error=message,
            failure_kind="value_unavailable",
            updated_at=now(),
        ),
        plan,
        now,
    )


def skip_failed_item(
    state: ExecutionState, plan: WorkflowPlan, now: Clock, reason: str | None = None
) -> ExecutionState:
    """Advance past the current failed item after an explicit force request."""
    records = list(state.item_executions)
    if state.cursor < len(records) and reason:
        prior_error = records[state.cursor].error
        records[state.cursor] = replace(
            records[state.cursor],
            error=f"{prior_error or 'operator force-skipped'}\nForce reason: {reason}",
        )
    return project_steps(
        replace(
            state,
            status="pending",
            active_item_id=None,
            cursor=state.cursor + 1,
            item_executions=tuple(records),
            last_error=None,
            failure_kind=None,
            updated_at=now(),
        ),
        plan,
        now,
    )


def begin_agent_item(
    state: ExecutionState,
    plan: WorkflowPlan,
    item: PlanItem,
    *,
    model: str,
    reasoning: str,
    selected_agent: str | None = None,
    selected_model: str | None = None,
    selected_reasoning: str | None = None,
    change_mark: str | None = None,
    resolution: tuple[tuple[RuleResolution, ...], tuple[PlannedCheck, ...]]
    | None = None,
    now: Clock,
) -> ExecutionState:
    """Mark one agent-owned item and its run as in progress.

    ``change_mark`` is the tree the step's change set starts from, and
    ``resolution`` how its rules without a command are enforced; a step that
    began before keeps what it began with.
    """
    records = list(state.item_executions)
    record = records[state.cursor]
    if resolution is not None and not record.rule_resolutions:
        records[state.cursor] = replace(
            record,
            status="in_progress",
            started_at=record.started_at or now(),
            attempts=record.attempts + 1,
            model=model,
            reasoning=reasoning,
            selected_agent=selected_agent,
            selected_model=selected_model,
            selected_reasoning=selected_reasoning,
            change_mark=record.change_mark or change_mark,
            rule_resolutions=resolution[0],
            resolved_checks=resolution[1],
        )
    else:
        records[state.cursor] = replace(
            record,
            status="in_progress",
            started_at=record.started_at or now(),
            attempts=record.attempts + 1,
            model=model,
            reasoning=reasoning,
            selected_agent=selected_agent,
            selected_model=selected_model,
            selected_reasoning=selected_reasoning,
            change_mark=record.change_mark or change_mark,
        )
    return project_steps(
        replace(
            state,
            status="in_progress",
            active_item_id=item.id,
            item_executions=tuple(records),
            updated_at=now(),
        ),
        plan,
        now,
    )


def interrupt_automatic_item(
    state: ExecutionState, plan: WorkflowPlan, item: PlanItem, now: Clock
) -> ExecutionState:
    """Record an automatic action whose externally visible outcome is unknown."""
    message = f"automatic handler {item.name!r} was interrupted; its outcome is unknown"
    records = list(state.item_executions)
    record = records[state.cursor]
    records[state.cursor] = replace(
        record,
        status="interrupted",
        commands=tuple(
            replace(command, status="interrupted")
            if command.status == "in_progress"
            else command
            for command in record.commands
        ),
        error=message,
    )
    return project_steps(
        replace(
            state,
            status="interrupted",
            active_item_id=item.id,
            item_executions=tuple(records),
            last_error=message,
            updated_at=now(),
        ),
        plan,
        now,
    )


def settle_stale_automatic_item(
    state: ExecutionState, plan: WorkflowPlan, item: PlanItem, now: Clock
) -> ExecutionState:
    """Classify an automatic item whose process died before it recorded an end.

    A command segment that already recorded its non-zero exit is a known
    failure: the external process finished, only ww's bookkeeping was cut
    short.  Any other stale record still has an operation unaccounted for and
    stays an unknown outcome.
    """
    commands = state.item_executions[state.cursor].commands
    if any(command.status == "in_progress" for command in commands):
        return interrupt_automatic_item(state, plan, item, now)
    failed = next((command for command in commands if command.status == "failed"), None)
    if failed is None:
        return interrupt_automatic_item(state, plan, item, now)
    return _fail_stale_automatic_item(state, plan, item, failed, now)


def _fail_stale_automatic_item(
    state: ExecutionState,
    plan: WorkflowPlan,
    item: PlanItem,
    command: CommandExecution,
    now: Clock,
) -> ExecutionState:
    exit_code = f" ({command.exit_code})" if command.exit_code is not None else ""
    # ``CommandExecution.index`` is the segment's one-based declaration ordinal.
    message = (
        f"automatic handler {item.name!r} failed{exit_code} at command "
        f"{command.index}; ww was interrupted before it recorded the failure"
    )
    detail = command.stderr.strip() or command.stdout.strip()
    if detail:
        message = f"{message}\n\n{detail}"
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor], status="failed", error=message
    )
    return project_steps(
        replace(
            state,
            status="failed",
            active_item_id=item.id,
            item_executions=tuple(records),
            last_error=message,
            updated_at=now(),
        ),
        plan,
        now,
    )


def resume_interrupted_item(
    state: ExecutionState,
    plan: WorkflowPlan,
    item: PlanItem,
    *,
    now: Clock,
) -> ExecutionState:
    """Make an interrupted action runnable after its outcome is resolved."""
    records = list(state.item_executions)
    record = records[state.cursor]
    records[state.cursor] = replace(
        record,
        status="pending",
        error=None,
        commands=tuple(
            replace(command, status="pending")
            if command.status == "interrupted"
            else command
            for command in record.commands
        ),
    )
    return project_steps(
        replace(
            state,
            status="pending",
            active_item_id=item.id,
            item_executions=tuple(records),
            last_error=None,
            updated_at=now(),
        ),
        plan,
        now,
    )


def record_recovery_uncertainty(
    state: ExecutionState, error: str, now: Clock
) -> ExecutionState:
    """Keep an interrupted item intact while exposing a checker diagnostic."""
    return replace(state, last_error=error, updated_at=now())


def attest_cli_command(
    state: ExecutionState,
    plan: WorkflowPlan,
    item: PlanItem,
    command_index: int,
    output: str,
    output_reference: str | None,
    now: Clock,
) -> ExecutionState:
    """Record operator attestation for one interrupted CLI command segment."""
    records = list(state.item_executions)
    record = records[state.cursor]
    commands = list(record.commands)
    commands[command_index] = replace(
        commands[command_index],
        status="completed",
        completed_at=now(),
        exit_code=0,
        stdout=output,
        stdout_ref=output_reference,
    )
    records[state.cursor] = replace(
        record, status="pending", error=None, commands=tuple(commands)
    )
    return project_steps(
        replace(
            state,
            status="pending",
            active_item_id=item.id,
            item_executions=tuple(records),
            last_error=None,
            updated_at=now(),
        ),
        plan,
        now,
    )


def attest_extension_item(
    state: ExecutionState,
    plan: WorkflowPlan,
    values: dict[str, str],
    result: str,
    working_directory: str | None,
    now: Clock,
) -> ExecutionState:
    """Complete an interrupted extension from an operator/checker attestation."""
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor],
        status="completed",
        completed_at=now(),
        error=None,
        output_values=tuple(values.items()),
        result=result,
    )
    workflow_values = {**dict(state.workflow_values), **values}
    return project_steps(
        replace(
            state,
            status="pending",
            active_item_id=None,
            cursor=state.cursor + 1,
            item_executions=tuple(records),
            last_error=None,
            updated_at=now(),
            working_directory=working_directory or state.working_directory,
            workflow_values=tuple(workflow_values.items()),
        ),
        plan,
        now,
    )


def fail_agent_item(
    state: ExecutionState,
    plan: WorkflowPlan,
    item: PlanItem,
    error: str,
    now: Clock,
) -> ExecutionState:
    """Fail the active agent item and preserve the same error on the run."""
    message = f"agent item {item.name!r} failed: {error}"
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor], status="failed", error=message
    )
    return project_steps(
        replace(
            state,
            status="failed",
            active_item_id=item.id,
            item_executions=tuple(records),
            last_error=message,
            updated_at=now(),
        ),
        plan,
        now,
    )


def supply_requested_input(
    state: ExecutionState,
    item_index: int,
    supplied: dict[str, str],
    now: Clock,
    selected_agent: str | None = None,
    selected_model: str | None = None,
    selected_reasoning: str | None = None,
) -> ExecutionState:
    """Satisfy a pending input request and make its item runnable again."""
    values = {**dict(state.workflow_values), **supplied}
    records = list(state.item_executions)
    records[item_index] = replace(
        records[item_index],
        status="pending",
        supplied_values=tuple(supplied.items()),
        error=None,
        selected_agent=selected_agent,
        selected_model=selected_model,
        selected_reasoning=selected_reasoning,
    )
    return replace(
        state,
        status="pending",
        pending_input_request=None,
        workflow_values=tuple(values.items()),
        item_executions=tuple(records),
        updated_at=now(),
    )


def complete_agent_item(
    state: ExecutionState,
    plan: WorkflowPlan,
    supplied: dict[str, str],
    artifact_reference: str | None,
    now: Clock,
    selected_agent: str | None = None,
    selected_model: str | None = None,
    selected_reasoning: str | None = None,
    clear_selected_model: bool = False,
    clear_selected_reasoning: bool = False,
    summary_for_next: str | None = None,
    check_report: CheckReport | None = None,
) -> ExecutionState:
    """Complete the active agent item and merge its provided values.

    ``check_report`` is the passing report of the checks ww ran, if any.
    """
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor],
        check_reports=(
            (*records[state.cursor].check_reports, check_report)
            if check_report is not None
            else records[state.cursor].check_reports
        ),
        draft_artifact=None,
        held_completion=None,
        status="completed",
        completed_at=now(),
        supplied_values=tuple(supplied.items()),
        artifact=artifact_reference,
        summary_for_next=summary_for_next,
        selected_agent=selected_agent or records[state.cursor].selected_agent,
        selected_model=(
            None
            if clear_selected_model
            else selected_model
            if selected_model is not None
            else records[state.cursor].selected_model
        ),
        selected_reasoning=(
            None
            if clear_selected_reasoning
            else selected_reasoning
            if selected_reasoning is not None
            else records[state.cursor].selected_reasoning
        ),
    )
    values = {**dict(state.workflow_values), **supplied}
    return project_steps(
        replace(
            state,
            status="pending",
            active_item_id=None,
            cursor=state.cursor + 1,
            item_executions=tuple(records),
            workflow_values=tuple(values.items()),
            updated_at=now(),
        ),
        plan,
        now,
    )


def begin_child_workflow(
    state: ExecutionState, item: PlanItem, now: Clock
) -> ExecutionState:
    """Enter the coordinator-owned wait state for a child workflow item."""
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor], status="in_progress", started_at=now()
    )
    return replace(
        state,
        status="in_progress",
        active_item_id=item.id,
        item_executions=tuple(records),
        updated_at=now(),
    )


def fail_child_workflow(
    state: ExecutionState,
    plan: WorkflowPlan,
    item: PlanItem,
    child_id: str,
    now: Clock,
) -> ExecutionState:
    """Fail the active child coordinator when one of its children fails."""
    message = f"child {child_id!r} failed"
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor], status="failed", error=message
    )
    return project_steps(
        replace(
            state,
            status="failed",
            active_item_id=item.id,
            item_executions=tuple(records),
            last_error=message,
            updated_at=now(),
        ),
        plan,
        now,
    )


def complete_child_workflow(
    state: ExecutionState,
    plan: WorkflowPlan,
    now: Clock,
    artifact: str | None = None,
) -> ExecutionState:
    """Complete a child coordinator after its children finish.

    ``artifact`` is the saved result of a per-child run: the child's summary.
    """
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor],
        status="completed",
        completed_at=now(),
        artifact=artifact,
    )
    return project_steps(
        replace(
            state,
            status="pending",
            active_item_id=None,
            cursor=state.cursor + 1,
            item_executions=tuple(records),
            updated_at=now(),
        ),
        plan,
        now,
    )


def complete_child_summary(
    state: ExecutionState, summary: str, now: Clock
) -> ExecutionState:
    """Complete the summary item that immediately follows child coordination."""
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor], status="completed", completed_at=now()
    )
    values = {**dict(state.workflow_values), "summary": summary}
    return replace(
        state,
        status="pending",
        cursor=state.cursor + 1,
        item_executions=tuple(records),
        workflow_values=tuple(values.items()),
        updated_at=now(),
    )


def await_item_input(
    state: ExecutionState,
    item: PlanItem,
    missing: tuple[ProvidedVariable, ...],
    now: Clock,
) -> ExecutionState:
    """Pause an automatic item until all declared values are supplied."""
    records = list(state.item_executions)
    records[state.cursor] = replace(records[state.cursor], status="awaiting_input")
    return replace(
        state,
        status="awaiting_input",
        item_executions=tuple(records),
        pending_input_request=InputRequest(item_id=item.id, values=missing),
        updated_at=now(),
    )


def block_item_phase(state: ExecutionState, message: str, now: Clock) -> ExecutionState:
    """Stop before leaving an items pass whose items lack what it declares.

    Nothing failed and the next step has not started: ``next --retry``
    checks the items again once the operator recorded what they lack.
    """
    return replace(
        state,
        status="failed",
        last_error=message,
        failure_kind="pass_incomplete",
        updated_at=now(),
    )


def advance_completed_item(state: ExecutionState, now: Clock) -> ExecutionState:
    """Move the cursor past an item already recorded as completed."""
    return replace(state, cursor=state.cursor + 1, updated_at=now())


def pause_for_agent(state: ExecutionState, now: Clock) -> ExecutionState:
    """Expose a pending agent item without activating it."""
    return replace(state, status="pending", updated_at=now())


def enter_loop(
    state: ExecutionState, plan: WorkflowPlan, item: PlanItem, now: Clock
) -> ExecutionState:
    """Enter a loop once and advance to its first ordinary nested step."""
    loop = loop_control(item)
    if loop is None or loop.boundary != "enter":
        raise StateError("enter_loop requires a loop entry item")
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor], status="completed", completed_at=now()
    )
    iterations = {
        **dict(state.loop_iterations),
        loop.loop_id: 1,
    }
    return project_steps(
        replace(
            state,
            cursor=state.cursor + 1,
            status="pending",
            item_executions=tuple(records),
            loop_iterations=tuple(iterations.items()),
            updated_at=now(),
        ),
        plan,
        now,
    )


def repeat_loop(
    state: ExecutionState, plan: WorkflowPlan, item: PlanItem, now: Clock
) -> ExecutionState:
    """Reset a completed loop body for its next durable iteration."""
    loop = loop_control(item)
    if loop is None or loop.boundary != "repeat":
        raise StateError("repeat_loop requires a loop repeat item")
    records = list(state.item_executions)
    entry = next(
        (
            index
            for index in range(state.cursor - 1, -1, -1)
            if (candidate := loop_control(plan.items[index])) is not None
            and candidate.loop_id == loop.loop_id
            and candidate.boundary == "enter"
        ),
        None,
    )
    if entry is None:
        raise StateError(f"loop {loop.loop_id!r} has no entry boundary")
    iteration = dict(state.loop_iterations).get(loop.loop_id, 1) + 1
    scope = loop_operation_scope(state, plan, entry, iteration)
    history = [*state.execution_history, *records[entry + 1 : state.cursor + 1]]
    for index in range(entry + 1, state.cursor + 1):
        records[index] = new_item_execution(state.task_id, scope, plan.items[index])
    iterations = {
        **dict(state.loop_iterations),
        loop.loop_id: iteration,
    }
    return project_steps(
        replace(
            state,
            cursor=entry + 1,
            status="pending",
            active_item_id=None,
            item_executions=tuple(records),
            execution_history=tuple(history),
            assignment_item_id=None,
            assignment_token=None,
            loop_iterations=tuple(iterations.items()),
            updated_at=now(),
        ),
        plan,
        now,
    )


def loop_limit_reached(state: ExecutionState, item: PlanItem) -> bool:
    """Return whether a repeat boundary has exhausted its saved iteration limit."""
    loop = loop_control(item)
    if loop is None or loop.boundary != "repeat":
        return False
    return dict(state.loop_iterations).get(loop.loop_id, 0) >= loop.max_times


def exit_exhausted_loop(
    state: ExecutionState, plan: WorkflowPlan, now: Clock, reason: str | None = None
) -> ExecutionState:
    """Leave a loop at its iteration limit after an explicit operator force.

    The repeat boundary is recorded as completed with the operator's reason so
    the exit stays visible in the run history, and execution continues with
    whatever follows the loop wrapper.
    """
    boundary = plan.items[state.cursor] if state.cursor < len(plan.items) else None
    if boundary is None or not loop_limit_reached(state, boundary):
        raise StateError("exit_exhausted_loop requires a loop at its iteration limit")
    records = list(state.item_executions)
    note = "operator force-exited the loop at its iteration limit"
    if reason:
        note = f"{note}\nForce reason: {reason}"
    records[state.cursor] = replace(
        records[state.cursor], status="completed", completed_at=now(), error=note
    )
    return project_steps(
        replace(
            state,
            status="pending",
            active_item_id=None,
            assignment_item_id=None,
            assignment_token=None,
            cursor=state.cursor + 1,
            item_executions=tuple(records),
            last_error=None,
            updated_at=now(),
        ),
        plan,
        now,
    )


def loop_operation_scope(
    state: ExecutionState,
    plan: WorkflowPlan,
    entry: int,
    iteration: int,
) -> str:
    """Return an operation namespace including every enclosing loop iteration."""
    loop_entry = plan.items[entry]
    active = loop_control(loop_entry)
    if active is None:  # pragma: no cover - caller invariant
        raise StateError("loop entry has no loop ID")
    enclosing = set(loop_entry.ancestors)
    lineage = [
        loop.loop_id
        for item in plan.items[: entry + 1]
        if (loop := loop_control(item)) is not None
        and loop.boundary == "enter"
        and (loop.loop_id in enclosing or loop.loop_id == active.loop_id)
    ]
    iterations = dict(state.loop_iterations)
    encoded_segments = []
    for loop_id in lineage:
        value = _loop_iteration(loop_id, active.loop_id, iteration, iterations)
        encoded_segments.append(f"{loop_id}:{value}")
    encoded = ":".join(encoded_segments)
    return f"{operation_scope_for(state)}{LOOP_SCOPE_MARKER}{encoded}"


# What ``loop_operation_scope`` puts between the run's scope and the loops.
LOOP_SCOPE_MARKER = ":loop:"


def loop_iteration_of(record: PlanItemExecution, loop_id: str) -> int | None:
    """The iteration of ``loop_id`` an execution record was made for.

    Read back from the record's operation ID, which ``loop_operation_scope``
    encodes as ``loop:<loop>:<iteration>:...``; a record without a segment
    for the loop belongs to its first iteration. ``None`` when the record has
    no operation ID to read.
    """
    if record.operation_id is None:
        return None
    scope = record.operation_id.removesuffix(f":{record.plan_item_id}")
    _, marker, encoded = scope.partition(LOOP_SCOPE_MARKER)
    if not marker:
        return 1
    # The loop's "<loop>:<iteration>" pair, e.g. "review:3" in "build:1:review:3".
    found = re.search(rf"(?:^|:){re.escape(loop_id)}:(\d+)(?=:|$)", encoded)
    return int(found.group(1)) if found else 1


def _loop_iteration(
    loop_id: str, active_loop_id: str, iteration: int, iterations: dict[str, int]
) -> int:
    """Select the reset iteration or its enclosing loop's saved iteration."""
    return iteration if loop_id == active_loop_id else iterations[loop_id]


def enclosing_loop_entry_index(plan: WorkflowPlan, item_index: int) -> int:
    """Return the entry boundary for a plan item nested in a loop."""
    item = plan.items[item_index]
    lineage = {item.step, *item.ancestors}
    entry = next(
        (
            index
            for index in range(item_index - 1, -1, -1)
            if (loop := loop_control(plan.items[index])) is not None
            and loop.boundary == "enter"
            and loop.loop_id in lineage
        ),
        None,
    )
    if entry is None:
        raise StateError(f"step {item.step!r} has no enclosing loop")
    return entry


def request_loop_exit(
    state: ExecutionState,
    plan: WorkflowPlan,
    stopped_item: PlanItem,
    wrapper_artifact_reference: str | None,
    now: Clock,
) -> ExecutionState:
    """Persist a worker's stop decision while its completion hooks still run.

    A ``break`` that ends per-child stages has no loop wrapper to record.
    """
    if stopped_item.loop_break is None or stopped_item.owner != "agent":
        raise StateError("request_loop_exit requires a break-enabled agent step")
    if stopped_item.breaks_children:
        return replace(state, loop_exit_item_id=stopped_item.id, updated_at=now())
    stopped_index = next(
        index for index, item in enumerate(plan.items) if item.id == stopped_item.id
    )
    entry = enclosing_loop_entry_index(plan, stopped_index)
    records = list(state.item_executions)
    records[entry] = replace(records[entry], artifact=wrapper_artifact_reference)
    return project_steps(
        replace(
            state,
            loop_exit_item_id=stopped_item.id,
            item_executions=tuple(records),
            updated_at=now(),
        ),
        plan,
        now,
    )


def request_loop_continue(
    state: ExecutionState,
    plan: WorkflowPlan,
    continued_item: PlanItem,
    now: Clock,
) -> ExecutionState:
    """Persist a worker's continue decision until its completion hooks finish."""
    if continued_item.loop_continue is None or continued_item.owner != "agent":
        raise StateError("request_loop_continue requires a continue-enabled agent step")
    return replace(state, loop_continue_item_id=continued_item.id, updated_at=now())


def finish_loop_continue(
    state: ExecutionState, plan: WorkflowPlan, now: Clock
) -> ExecutionState:
    """Restart the enclosing loop after a continue step's completion lifecycle."""
    if state.loop_continue_item_id is None:
        return state
    continued_index = next(
        index
        for index, item in enumerate(plan.items)
        if item.id == state.loop_continue_item_id
    )
    entry = enclosing_loop_entry_index(plan, continued_index)
    repeat = next(
        index
        for index in range(continued_index, len(plan.items))
        if (loop := loop_control(plan.items[index])) is not None
        and loop.boundary == "repeat"
        and loop.loop_id == _required_loop_control(plan.items[entry]).loop_id
    )
    continued_item = plan.items[continued_index]
    # The completion hooks are still part of the continued item's lifecycle.
    # Do not reset the body until they have run (or reported their own state).
    if (
        state.cursor < len(plan.items)
        and plan.items[state.cursor].step == continued_item.step
    ):
        return state
    boundary = plan.items[repeat]
    if loop_limit_reached(state, boundary):
        # Match the normal repeat boundary: retain the completed iteration and
        # expose the manager escalation instead of silently starting another.
        return project_steps(
            replace(
                state,
                cursor=repeat,
                status="pending",
                active_item_id=None,
                loop_continue_item_id=None,
                updated_at=now(),
            ),
            plan,
            now,
        )
    records = list(state.item_executions)
    loop_iterations: dict[str, int] = dict(state.loop_iterations)
    iteration = (
        loop_iterations.get(_required_loop_control(plan.items[entry]).loop_id, 1) + 1
    )
    loop_iterations[_required_loop_control(plan.items[entry]).loop_id] = iteration
    scope = loop_operation_scope(state, plan, entry, iteration)
    history = [*state.execution_history, *records[entry + 1 : repeat + 1]]
    for index in range(entry + 1, repeat + 1):
        records[index] = new_item_execution(state.task_id, scope, plan.items[index])
    return project_steps(
        replace(
            state,
            cursor=entry + 1,
            status="pending",
            active_item_id=None,
            item_executions=tuple(records),
            execution_history=tuple(history),
            assignment_item_id=None,
            assignment_token=None,
            loop_iterations=tuple(loop_iterations.items()),
            loop_continue_item_id=None,
            updated_at=now(),
        ),
        plan,
        now,
    )


def finish_loop_exit(
    state: ExecutionState, plan: WorkflowPlan, now: Clock
) -> ExecutionState:
    """Exit after the stopping step's own completion lifecycle has finished.

    A loop is left after its repeat boundary.  A ``break`` in a per-child
    stage skips every remaining per-child stage instead; the caller marks
    the children that never started as skipped.
    """
    if state.loop_exit_item_id is None:
        return state
    completed_index = next(
        (
            index
            for index, item in enumerate(plan.items)
            if item.id == state.loop_exit_item_id
        ),
        None,
    )
    if completed_index is None:
        raise StateError("loop exit references an unknown plan item")
    stopped_item = plan.items[completed_index]
    if (
        state.cursor < len(plan.items)
        and plan.items[state.cursor].step == stopped_item.step
    ):
        return state
    if stopped_item.breaks_children:
        last = max(
            index
            for index, item in enumerate(plan.items)
            if item.child_stage == stopped_item.child_stage
            and item.child_number is not None
        )
        return _skip_to(
            state,
            plan,
            max(state.cursor, last + 1),
            "skipped because a children break gate passed",
            now,
        )
    entry = enclosing_loop_entry_index(plan, completed_index)
    loop_id = _required_loop_control(plan.items[entry]).loop_id
    repeat = next(
        (
            index
            for index in range(state.cursor, len(plan.items))
            if (loop := loop_control(plan.items[index])) is not None
            and loop.boundary == "repeat"
            and loop.loop_id == loop_id
        ),
        None,
    )
    if repeat is None:
        raise StateError(f"loop {loop_id!r} has no repeat boundary")
    return _skip_to(
        state, plan, repeat + 1, "skipped because a loop break gate passed", now
    )


def _skip_to(
    state: ExecutionState, plan: WorkflowPlan, stop: int, result: str, now: Clock
) -> ExecutionState:
    """Complete every item from the cursor up to ``stop`` as skipped by a break."""
    records = list(state.item_executions)
    for index in range(state.cursor, stop):
        records[index] = replace(
            records[index], status="completed", completed_at=now(), result=result
        )
    return project_steps(
        replace(
            state,
            cursor=stop,
            status="pending",
            active_item_id=None,
            item_executions=tuple(records),
            loop_exit_item_id=None,
            updated_at=now(),
        ),
        plan,
        now,
    )


def complete_run(
    state: ExecutionState, plan: WorkflowPlan, now: Clock
) -> ExecutionState:
    """Mark a run complete after its cursor reaches the end of the plan."""
    return project_steps(
        replace(state, status="completed", active_item_id=None, updated_at=now()),
        plan,
        now,
    )


def materialize_item_plan(
    state: ExecutionState,
    snapshot: PlanSnapshot,
    collector: PlanItem,
    items: tuple[WorkItem, ...],
    now: Clock,
) -> tuple[ExecutionState, PlanSnapshot]:
    """Expand one ``items`` pass for the items collected when it completes.

    Only the pass ``collector`` declares is expanded, right after it, from
    its own templates in the template plan; every other pass keeps its
    templates (or its concrete stages) untouched.  The pass's earlier
    stages, from a previous loop round, are replaced: the new round runs
    the items collected now, so an item added since joins it and the
    membership of a running pass never changes.  A pass without stages
    (``items: {steps: []}``), or one that collected no items, expands to
    nothing.  The snapshot is written in the current schema, whose pass
    identity the expanded plan relies on.
    """
    pass_id = collector.item_pass
    if pass_id is None:
        raise StateError(f"items step {collector.name!r} has no item pass")
    template = snapshot.template_plan or snapshot.plan
    templates = tuple(
        entry
        for entry in template.items
        if entry.item_template
        and entry.child_stage is None
        and entry.item_pass == pass_id
    )
    if not templates:
        return state, snapshot
    members = frozenset(
        entry.id
        for entry in snapshot.plan.items
        if entry.item_pass == pass_id
        and entry.child_stage is None
        and (entry.item_template or entry.item_id is not None)
    )
    anchor = next(
        (
            index
            for index, entry in enumerate(snapshot.plan.items)
            if entry.id == collector.id
        ),
        None,
    )
    if anchor is None:
        raise StateError(f"items step {collector.name!r} is not in the plan")
    return _expand_templates(
        state,
        replace(
            snapshot, schema_version=max(snapshot.schema_version, PLAN_SCHEMA_VERSION)
        ),
        templates,
        "{item}",
        tuple(
            (f"item-{number}", f"item:{work_item.id}", {"item_id": work_item.id})
            for number, work_item in enumerate(items, 1)
        ),
        now,
        replaced=members,
        after=collector.id,
        scope=_record_scope(state, anchor),
    )


def _record_scope(state: ExecutionState, index: int) -> str:
    """The operation namespace the record at ``index`` was created in.

    A loop round gives its records a namespace of their own; stages expanded
    in that round share their collection step's.
    """
    record = state.item_executions[index]
    prefix, suffix = f"{state.task_id}:", f":{record.plan_item_id}"
    operation = record.operation_id
    if operation is None or not (
        operation.startswith(prefix) and operation.endswith(suffix)
    ):
        return operation_scope_for(state)
    return operation[len(prefix) : -len(suffix)]


def materialize_child_plan(
    state: ExecutionState,
    snapshot: PlanSnapshot,
    children: tuple[ChildTask, ...],
    now: Clock,
) -> tuple[ExecutionState, PlanSnapshot]:
    """Replace per-child stage templates with one lifecycle per child.

    Each child's stages carry its position in the run's children, which is
    stable: children are only ever appended, and a child keeps its position
    when it binds its own ID.
    """
    templates = tuple(
        entry
        for entry in snapshot.plan.items
        if entry.item_template and entry.child_stage is not None
    )
    if not templates:
        return state, snapshot
    if not children:
        raise StateError(
            "children step completed without recorded children; use add-child"
        )
    return _expand_templates(
        state,
        snapshot,
        templates,
        "{child}",
        tuple(
            (f"child-{number}", f"child:{number}", {"child_number": number})
            for number in range(1, len(children) + 1)
        ),
        now,
    )


def _expand_templates(
    state: ExecutionState,
    snapshot: PlanSnapshot,
    templates: tuple[PlanItem, ...],
    placeholder: str,
    units: tuple[tuple[str, str, dict[str, str | int]], ...],
    now: Clock,
    *,
    replaced: frozenset[str] | None = None,
    after: str | None = None,
    scope: str | None = None,
) -> tuple[ExecutionState, PlanSnapshot]:
    """Expand ``templates`` once per unit at the first template's position.

    Each unit is its path segment (replacing ``placeholder`` in every path),
    its plan-item ID suffix, and the fields binding the copy to its unit.
    ``replaced`` names the plan items the expansion replaces, the templates
    by default; with ``after`` the expansion goes right after that item
    instead.  New records are created in ``scope``, the run's by default.
    """
    template_ids = (
        replaced
        if replaced is not None
        else frozenset(template.id for template in templates)
    )

    def concrete_path(value: str, segment: str) -> str:
        return value.replace(placeholder, segment)

    def expand(
        template: PlanItem, segment: str, suffix: str, bind: dict[str, str | int]
    ) -> PlanItem:
        operation = template.operation
        if isinstance(operation, LoopBoundary):
            operation = replace(
                operation, loop_id=concrete_path(operation.loop_id, segment)
            )
        return replace(
            template,
            id=f"{template.id}:{suffix}",
            operation=operation,
            step=concrete_path(template.step, segment),
            parent=(
                concrete_path(template.parent, segment)
                if template.parent is not None
                else None
            ),
            ancestors=tuple(
                concrete_path(path, segment) for path in template.ancestors
            ),
            artifact_dependency=(
                concrete_path(template.artifact_dependency, segment)
                if template.artifact_dependency is not None
                else None
            ),
            loop_id=(
                concrete_path(template.loop_id, segment)
                if template.loop_id is not None
                else None
            ),
            assessment_parent=(
                concrete_path(template.assessment_parent, segment)
                if template.assessment_parent is not None
                else None
            ),
            item_template=False,
            item_id=str(bind["item_id"]) if "item_id" in bind else template.item_id,
            child_number=(
                int(bind["child_number"])
                if "child_number" in bind
                else template.child_number
            ),
        )

    expansion = [
        expand(template, segment, suffix, bind)
        for segment, suffix, bind in units
        for template in templates
    ]
    concrete: list[PlanItem] = []
    expanded_templates = False
    for entry in snapshot.plan.items:
        if entry.id in template_ids:
            if after is None and not expanded_templates:
                concrete.extend(expansion)
                expanded_templates = True
            continue
        concrete.append(entry)
        if entry.id == after:
            concrete.extend(expansion)

    plan = replace(
        snapshot.plan,
        items=number_step_paths(
            tuple(
                replace(entry, position=index)
                for index, entry in enumerate(concrete, 1)
            )
        ),
    )
    old_records = {record.plan_item_id: record for record in state.item_executions}
    records = tuple(
        replace(old_records[entry.id], position=entry.position)
        if entry.id in old_records
        else new_item_execution(
            state.task_id, scope or operation_scope_for(state), entry
        )
        for entry in plan.items
    )
    revised_snapshot = replace(
        snapshot, plan=plan, plan_revision=snapshot.plan_revision + 1
    )
    revised_state = replace(
        state,
        item_executions=records,
        steps=build_step_projection(plan, state.steps),
        plan_revision=revised_snapshot.plan_revision,
        plan_digest=revised_snapshot.plan_digest,
    )
    return project_steps(revised_state, plan, now), revised_snapshot


def finish_selection(state: ExecutionState, target: str, now: Clock) -> ExecutionState:
    """Complete a handoff-selection run without performing coordination I/O."""
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor], status="completed", completed_at=now()
    )
    return replace(
        state,
        status="completed",
        active_item_id=None,
        cursor=state.cursor + 1,
        item_executions=tuple(records),
        workflow_values=tuple(
            {
                **dict(state.workflow_values),
                "summary": f"handed off to {target}",
            }.items()
        ),
        updated_at=now(),
    )


def project_steps(
    state: ExecutionState, plan: WorkflowPlan, now: Clock
) -> ExecutionState:
    """Rebuild human-facing step status from authoritative item records."""
    by_path: dict[str, list[str]] = {}
    for item, record in zip(plan.items, state.item_executions, strict=True):
        by_path.setdefault(item.step, []).append(
            "in_progress"
            if record.repair_pending and state.status != "failed"
            else record.status
        )

    def refresh(node: StepProgress) -> StepProgress:
        children = tuple(refresh(child) for child in node.children)
        statuses = [*by_path.get(node.path, []), *(child.status for child in children)]
        status: StepStatus
        if any(value == "failed" for value in statuses):
            status = "failed"
        elif statuses and all(value == "completed" for value in statuses):
            status = "completed"
        elif any(
            value in {"in_progress", "interrupted", "awaiting_input", "completed"}
            for value in statuses
        ):
            status = "in_progress"
        else:
            status = "pending"
        started = node.started_at or (
            now() if status in {"in_progress", "completed", "failed"} else None
        )
        completed = (
            now()
            if status == "completed" and node.completed_at is None
            else node.completed_at
        )
        return replace(
            node,
            status=status,
            started_at=started,
            completed_at=completed,
            children=children,
        )

    return replace(state, steps=tuple(refresh(node) for node in state.steps))


def _required_loop_control(item: PlanItem) -> LoopBoundary:
    """Read a loop descriptor where the persisted state already guarantees one."""
    loop = loop_control(item)
    if loop is None:  # pragma: no cover - caller invariant
        raise StateError("loop control item has no loop capability")
    return loop


def select_assessment_outcome(
    state: ExecutionState, plan: WorkflowPlan, outcome: str | None, now: Clock
) -> ExecutionState:
    """Apply the answer to a completed assessment before its work is dispatched.

    An outcome with steps moves the cursor to them and skips the others; one
    that stops the workflow, including the compact form's ``negative``, skips
    everything left, so the run completes.
    """
    pending = pending_assessment(state, plan)
    if pending is None:
        if outcome is not None:
            raise StateError("--outcome is only valid when selecting a pending assess")
        return state
    if outcome is None:
        raise StateError(
            "pending assess requires --outcome <" + "|".join(pending.labels) + ">"
        )
    chosen = pending.outcome(outcome)
    if chosen is None:
        raise StateError(
            f"unknown assessment outcome {outcome!r}; expected "
            + ", ".join(pending.labels)
        )
    records = list(state.item_executions)
    records[pending.index] = replace(records[pending.index], assessment_outcome=outcome)
    skipped = f"skipped: assessment selected {outcome}"
    if chosen.stops:
        for index in range(state.cursor, len(records)):
            records[index] = replace(
                records[index], status="completed", completed_at=now(), result=skipped
            )
        return replace(state, cursor=len(records), item_executions=tuple(records))
    if not pending.declared:
        return replace(state, item_executions=tuple(records))
    group = outcome_region(plan, pending.index)
    for index in group:
        if plan.items[index].assessment_outcome != outcome:
            records[index] = replace(
                records[index], status="completed", completed_at=now(), result=skipped
            )
    # An undeclared standard outcome has no work of its own: continue after
    # every outcome's work.
    selected = next(
        (index for index in group if plan.items[index].assessment_outcome == outcome),
        max(group) + 1,
    )
    return replace(state, cursor=selected, item_executions=tuple(records))
