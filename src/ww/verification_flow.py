# SPDX-License-Identifier: GPL-3.0-or-later
"""Ordinary repair lifecycles and exact messages for persistent rule verifiers."""

from __future__ import annotations

from dataclasses import replace

from ww.actions import PlannedAction, Prompt
from ww.execution_models import (
    ExecutionState,
    PlanSnapshot,
    build_step_projection,
    new_item_execution,
    operation_scope_for,
)
from ww.plan import PlanItem, WorkflowPlan, number_step_paths
from ww.rule_verification import index_of
from ww.transitions import Clock
from ww.workflow_config import FIX_RULE_VIOLATIONS, FIX_RULE_VIOLATIONS_PROMPT


def source_id(item: PlanItem) -> str | None:
    if item.verifies is not None:
        return item.verifies.item_id
    return item.repair_of


def verifier_lifecycle(source: PlanItem, verifier: PlanItem) -> tuple[PlanItem, ...]:
    policy = source.verification_plan
    if policy is None or not policy.verifier:
        return (verifier,)
    result = []
    for number, template in enumerate(policy.verifier, 1):
        if template.phase == "step":
            result.append(verifier)
        else:
            result.append(
                replace(
                    template,
                    id=f"{verifier.id}:hook:{number}",
                    step=source.step,
                    parent=source.parent,
                    ancestors=source.ancestors,
                    step_ordinals=source.step_ordinals,
                    item_context=source.item_context,
                    child_stage=source.child_stage,
                    child_number=source.child_number,
                    verifies=None,
                    verification_plan=None,
                    repair_of=None,
                    verification_hook=verifier.id,
                    role="worker",
                    rules=(),
                    checks=(),
                    repeat_until=None,
                )
            )
    return tuple(result)


def decision_due(state: ExecutionState, plan: WorkflowPlan) -> int | None:
    """A verifier is settled only once its own hooks have completed."""
    for index, item in enumerate(plan.items):
        record = state.item_executions[index]
        if (
            item.verifies is None
            or record.status != "completed"
            or not record.verification
            or not record.verification_results
            or record.verification_decided
        ):
            continue
        lifecycle = [
            position
            for position, entry in enumerate(plan.items)
            if entry.id == item.id or entry.verification_hook == item.id
        ]
        if state.cursor > max(lifecycle) and all(
            state.item_executions[position].status == "completed"
            for position in lifecycle
        ):
            return index
    return None


def _replace_plan(
    state: ExecutionState, snapshot: PlanSnapshot, items: tuple[PlanItem, ...]
) -> tuple[ExecutionState, PlanSnapshot]:
    items = number_step_paths(
        tuple(replace(item, position=index) for index, item in enumerate(items, 1))
    )
    previous = {record.plan_item_id: record for record in state.item_executions}
    plan = replace(snapshot.plan, items=items)
    snapshot = replace(snapshot, plan=plan, plan_revision=snapshot.plan_revision + 1)
    scope = operation_scope_for(state)
    state = replace(
        state,
        item_executions=tuple(
            replace(previous[item.id], position=item.position)
            if item.id in previous
            else new_item_execution(state.task_id, scope, item)
            for item in items
        ),
        steps=build_step_projection(plan, state.steps),
        plan_revision=snapshot.plan_revision,
        plan_digest=snapshot.plan_digest,
    )
    return state, snapshot


def default_repair(item: PlanItem) -> PlanItem:
    """A focused repair prompt with the source's execution shape, no side effects."""
    return replace(
        item,
        id=f"{item.id}:verification-fix:1",
        step=f"{item.step}/__verification_fix",
        parent=item.step,
        ancestors=(*item.ancestors, item.step),
        name=FIX_RULE_VIOLATIONS,
        description=FIX_RULE_VIOLATIONS_PROMPT,
        operation=PlannedAction("prompt", Prompt(FIX_RULE_VIOLATIONS_PROMPT)),
        owner="agent",
        execution="agent_instruction",
        requires_agent_input=False,
        source="internal",
        registered_handler=FIX_RULE_VIOLATIONS,
        role="worker",
        phase="step",
        interactive=False,
        choices=(),
        ui=False,
        provide=(),
        outputs=(),
        save_metadata=(),
        update_document=(),
        update_item=(),
        artifact=True,
        artifact_dependency=None,
        rules=(),
        checks=(),
        verifies=None,
        selects_rules_for=(),
        verification_plan=None,
        repeat_until=None,
        assessment_question=None,
        assessment_attached=False,
        assessment_outcomes=(),
        assessment_stops=(),
        assessment_parent=None,
        assessment_outcome=None,
        child_operation=None,
        child_stage=None,
        child_identity=False,
        item_operation=None,
        child_template=False,
        shared_items=False,
        item_identity=None,
        item_unique=(),
        split_instruction=None,
        repair_of=item.id,
        verification_hook=None,
        summary=False,
        on_failure="operator",
    )


def open_repair(
    state: ExecutionState, snapshot: PlanSnapshot, source: PlanItem, now: Clock
) -> tuple[ExecutionState, PlanSnapshot]:
    """Install or reset a frozen ordinary repair lifecycle before its source."""
    plan = snapshot.plan
    indexes = [
        index for index, item in enumerate(plan.items) if item.repair_of == source.id
    ]
    if not indexes:
        templates = (
            source.verification_plan.on_failure if source.verification_plan else ()
        )
        if not templates:
            templates = (default_repair(source),)
        fixed = []
        source_record = state.item_executions[index_of(plan, source.id)]
        for number, template in enumerate(templates, 1):
            suffix = template.step.partition("/__verification_fix")[2]
            template_source = template.step.partition("/__verification_fix")[0]

            def rebase(
                path: str | None, *, template_source: str = template_source
            ) -> str | None:
                if path == template_source:
                    return source.step
                if path is not None and path.startswith(f"{template_source}/"):
                    return source.step + path[len(template_source) :]
                return path

            model = (
                source_record.model
                if (template.requested_model or "auto")
                == (source.requested_model or "auto")
                else template.model
            )
            reasoning = (
                source_record.reasoning
                if (template.requested_reasoning or "auto")
                == (source.requested_reasoning or "auto")
                and (template.requested_model or "auto")
                == (source.requested_model or "auto")
                else template.reasoning
            )
            fixed.append(
                replace(
                    template,
                    id=f"{source.id}:verification-fix:{number}",
                    step=f"{source.step}/__verification_fix{suffix}",
                    parent=rebase(template.parent),
                    ancestors=tuple(
                        rebase(path) or path for path in template.ancestors
                    ),
                    artifact_dependency=rebase(template.artifact_dependency),
                    item_context=source.item_context,
                    child_stage=source.child_stage,
                    child_number=source.child_number,
                    child_template=False,
                    repair_of=source.id,
                    verification_hook=None,
                    verification_plan=None,
                    role="worker",
                    model=model or template.model,
                    reasoning=reasoning or template.reasoning,
                    requested_model=model or template.requested_model,
                    requested_reasoning=reasoning or template.requested_reasoning,
                    requested_agent=(
                        source_record.selected_agent or template.requested_agent
                        if template.requested_agent == source.requested_agent
                        else template.requested_agent
                    ),
                )
            )
        at = index_of(plan, source.id)
        state, snapshot = _replace_plan(
            state, snapshot, (*plan.items[:at], *fixed, *plan.items[at:])
        )
        plan = snapshot.plan
        indexes = [
            index
            for index, item in enumerate(plan.items)
            if item.repair_of == source.id
        ]
    records = list(state.item_executions)
    history = list(state.execution_history)
    scope = operation_scope_for(state)
    source_record = records[index_of(plan, source.id)]
    failed_id = source_record.verification_failed_by
    assert failed_id is not None
    failed_verifier = records[index_of(plan, failed_id)]
    for index in indexes:
        earlier = records[index]
        if earlier.attempts or earlier.status != "pending":
            history.append(earlier)
            generation = earlier.goto_round + 1
        else:
            generation = earlier.goto_round
        records[index] = replace(
            new_item_execution(
                state.task_id,
                f"{scope}:verification-fix-{generation}",
                plan.items[index],
            ),
            goto_round=generation,
            repeat_session=failed_verifier.repeat_session
            or failed_verifier.operation_id,
            repeat_round=failed_verifier.repeat_round,
            verification_failed_by=failed_id,
        )
    at = index_of(plan, source.id)
    records[at] = replace(
        records[at], verification_repair_pending=False, status="pending"
    )
    held = records[at].held_completion
    values = dict(state.workflow_values)
    local_names = {
        name
        for position in indexes
        for name in (
            *(value.name for value in plan.items[position].provide),
            *plan.items[position].outputs,
        )
    }
    values = {name: value for name, value in values.items() if name not in local_names}
    if held is not None:
        values.update(held.variables)
    return replace(
        state,
        item_executions=tuple(records),
        execution_history=tuple(history),
        cursor=min(indexes),
        status="pending",
        active_item_id=None,
        assignment_item_id=None,
        assignment_token=None,
        assignment_model=None,
        assignment_reasoning=None,
        assignment_selected_agent=None,
        assignment_selected_model=None,
        assignment_selected_reasoning=None,
        workflow_values=tuple(values.items()),
        updated_at=now(),
    ), snapshot


def communication(
    state: ExecutionState, plan: WorkflowPlan, index: int
) -> dict[str, object] | None:
    if index >= len(plan.items):
        return None
    item = plan.items[index]
    verifier_id = item.id if item.verifies else item.verification_hook
    target_id = source_id(item)
    if verifier_id is not None:
        verifier = plan.items[index_of(plan, verifier_id)]
        target_id = verifier.verifies.item_id if verifier.verifies else None
    if target_id is None:
        return None
    at = index_of(plan, target_id)
    source = plan.items[at]
    source_record = state.item_executions[at]
    if verifier_id is None:
        verifier_id = source_record.verification_failed_by
    if verifier_id is None:
        return None
    verifier_at = index_of(plan, verifier_id)
    record = state.item_executions[verifier_at]
    repair_record = state.item_executions[index] if item.repair_of else None
    failed_id = (
        repair_record.verification_failed_by
        if repair_record
        else source_record.verification_failed_by
    ) or verifier_id
    all_records = (*state.execution_history, *state.item_executions)
    findings = (
        record
        if record.verification_results and repair_record is None
        else next(
            (
                entry
                for entry in reversed(all_records)
                if entry.plan_item_id == failed_id
                and entry.verification_results
                and (
                    repair_record is None
                    or entry.repeat_round == repair_record.repeat_round
                    and (entry.repeat_session or entry.operation_id)
                    == repair_record.repeat_session
                )
            ),
            None,
        )
    )
    repair_ids = {entry.id for entry in plan.items if entry.repair_of == source.id}
    repair_round = (
        repair_record.repeat_round if repair_record else record.repeat_round - 1
    )
    latest = {
        result.plan_item_id: result
        for result in all_records
        if result.plan_item_id in repair_ids and result.repeat_round == repair_round
    }
    repairs = [
        {
            "item_id": result.plan_item_id,
            "text": result.result_text
            if result.result_text is not None
            else result.result,
            "artifact": result.artifact,
        }
        for result in latest.values()
        if result.status == "completed"
        and (result.result_text is not None or result.result is not None)
    ]
    held = source_record.held_completion
    return {
        "kind": "rule_verification",
        "step": plan.items[verifier_at].name,
        "source_step": source.step,
        "session": repair_record.repeat_session
        if repair_record
        else record.repeat_session or record.operation_id,
        "round": (repair_record.repeat_round if repair_record else record.repeat_round)
        + 1,
        "until": "all selected rules pass",
        "phase": "outcome" if item.repair_of else "assessment",
        "findings": findings.result_text if findings else None,
        "findings_artifact": findings.artifact if findings else None,
        "rule_results": list(findings.verification_results) if findings else [],
        "original_instruction": source.description,
        "original_artifact": held.draft_ref if held else source_record.artifact,
        "rules": [
            rule.to_dict()
            for entry, execution in zip(plan.items, state.item_executions, strict=True)
            if entry.verifies is not None and entry.verifies.item_id == source.id
            for rule in execution.verification
        ],
        "check_failures": [
            result.to_dict()
            for report in source_record.check_reports[-1:]
            for result in report.failed
            if result.source != "judged"
        ],
        "results": repairs,
        "delegation_required": state.workflow_runtime == "auto",
    }
