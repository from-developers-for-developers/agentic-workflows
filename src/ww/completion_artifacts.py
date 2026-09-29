# SPDX-License-Identifier: GPL-3.0-or-later
"""Persist agent completion artifacts without changing workflow state."""

from __future__ import annotations

from ww.actions import PlannedAction, actions
from ww.artifacts import RuleOutcome, RulesSummary, RuleStatus, render_step_artifact
from ww.contracts import CheckStatus
from ww.execution_models import CheckReport, ExecutionState, PlanSnapshot
from ww.plan import PlanItem
from ww.rule_checks import item_reports
from ww.storage_adapters.base import ArtifactAddress, TaskArtifactStorage

_REPORTED: dict[CheckStatus, RuleStatus] = {
    "passed": "passed",
    "failed": "failed",
    "not_applicable": "not applicable",
}


def rule_outcomes(
    state: ExecutionState, item: PlanItem, report: CheckReport | None
) -> RulesSummary | None:
    """What the completing step's artifact says about its rules and checks.

    A rule checked by a derived check reports that check's result; one a
    verifier judged reports the verdict and the verification item; any other
    rule without a check is self-declared: the worker states in its result
    how it followed it. A check reports its result in ``report``, or, when
    the operator waived it, in the last report that ran it. Rejections an
    operator retry moved into the history still count.
    """
    record = state.item_executions[state.cursor]
    if not item.rules and not item.checks:
        return None
    results = {
        result.id: result
        for ran in (*record.check_reports, *((report,) if report else ()))
        for result in ran.results
    }
    checked = {check.id for check in item.checks}
    derived = {
        rule_id: check.id
        for check in record.resolved_checks
        for rule_id in check.covers
    }
    held = record.held_completion
    outcomes = []
    for rule in item.rules:
        if rule.id in checked:
            continue
        verdict = held.verdict(rule.id) if held is not None else None
        if rule.id in derived:
            result = results.get(derived[rule.id])
            status = _REPORTED[result.status] if result else "not applicable"
            outcomes.append(
                RuleOutcome(rule.id, status, detail=f"check `{derived[rule.id]}`")
            )
        elif verdict is not None and verdict.verdict == "pass":
            outcomes.append(
                RuleOutcome(rule.id, "verified pass", detail=f"by `{verdict.by}`")
            )
        else:
            outcomes.append(RuleOutcome(rule.id, "self-declared"))
    for check in item.checks:
        result = results.get(check.id)
        outcomes.append(
            RuleOutcome(
                check.id,
                _REPORTED[result.status] if result is not None else "not applicable",
                hook=check.source == "hook",
            )
        )
    order = {rule.id: index for index, rule in enumerate(item.rules)}
    outcomes.sort(key=lambda outcome: order.get(outcome.id, len(order)))
    return RulesSummary(
        tuple(outcomes),
        fix_attempts=sum(1 for earlier in item_reports(state) if earlier.failed),
        waived=record.checks_waived,
    )


def write_completion_artifacts(
    storage: TaskArtifactStorage,
    task_id: str,
    state: ExecutionState,
    snapshot: PlanSnapshot,
    item: PlanItem,
    loop_entry: PlanItem | None,
    artifact: str | None,
    *,
    rules: RulesSummary | None = None,
) -> tuple[str | None, str | None]:
    """Write the completed item and optional enclosing-loop artifacts.

    ``rules`` is the completed item's own rule report; a loop wrapper's
    artifact carries none.
    """
    if artifact is None:
        return None, None

    def attribution(plan_item: PlanItem) -> str:
        if not isinstance(plan_item.operation, PlannedAction):
            return "auto"
        action = actions.get(plan_item.kind)
        return action.traits(plan_item.operation.payload).artifact_attribution

    step_paths = tuple(
        dict.fromkeys(
            plan_item.step
            for plan_item in snapshot.plan.items
            if plan_item.phase == "step"
        )
    )

    def rendered(plan_item: PlanItem) -> str:
        return render_step_artifact(
            task_id=task_id,
            workflow=state.workflow,
            step=plan_item.step,
            step_number=step_paths.index(plan_item.step) + 1,
            step_total=len(step_paths),
            skill=attribution(plan_item),
            result=artifact,
            rules=rules if plan_item is item else None,
        )

    def write(plan_item: PlanItem, content: str) -> str:
        return storage.write_execution_artifact(
            ArtifactAddress(
                task_id,
                state.workflow,
                plan_item.step,
                plan_item.position,
                plan_item.name,
                plan_item.phase,
                run_id=state.run_id,
                step_ordinals=plan_item.step_ordinals,
                loop_iterations=tuple(
                    (loop_id, iteration)
                    for loop_id, iteration in state.loop_iterations
                    if loop_id in plan_item.ancestors
                ),
            ),
            content,
        )

    artifact_reference = None
    if item.artifact:
        artifact_reference = write(
            item, rendered(item) if item.phase == "step" else artifact
        )
    wrapper_artifact_reference = None
    if loop_entry is not None and loop_entry.artifact:
        wrapper_artifact_reference = write(loop_entry, rendered(loop_entry))
    return artifact_reference, wrapper_artifact_reference
