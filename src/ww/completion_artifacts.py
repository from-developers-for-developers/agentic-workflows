# SPDX-License-Identifier: GPL-3.0-or-later
"""Persist agent completion artifacts without changing workflow state."""

from __future__ import annotations

from ww.actions import PlannedAction, actions
from ww.artifacts import RuleOutcome, RulesSummary, RuleStatus, render_step_artifact
from ww.contracts import CheckStatus
from ww.execution_models import CheckReport, ExecutionState, PlanSnapshot
from ww.plan import PlanItem, step_label
from ww.rule_checks import item_reports
from ww.storage_adapters.base import ArtifactAddress, TaskArtifactStorage

_REPORTED: dict[CheckStatus, RuleStatus] = {
    "passed": "passed",
    "failed": "failed",
    "not_applicable": "not applicable",
    "unavailable": "check unavailable",
}


def rule_outcomes(
    state: ExecutionState, item: PlanItem, report: CheckReport | None
) -> RulesSummary | None:
    """What the completing step's artifact says about its rules and checks.

    A rule checked by a derived check reports that check's result; one a
    verifier judged reports the verdict and the verification item, and the
    reason when it was judged because its check was unavailable; one whose
    scope selected none of the changed files is not applicable, as a check
    that did not run is; any other rule without a check is self-declared:
    the worker states in its result how it followed it. A check reports its
    result in ``report``, or, when the operator waived it, in the last
    report that ran it; one that could not run says why. Rejections an
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

    def reported(check_id: str, hook: bool = False) -> RuleOutcome:
        """A check's own line: its result, or the verdict that stood in."""
        result = results.get(check_id)
        verdict = held.verdict(check_id) if held is not None else None
        if result is None:
            return RuleOutcome(check_id, "not applicable", hook=hook)
        if result.status == "unavailable" and verdict and verdict.verdict == "pass":
            return RuleOutcome(
                check_id,
                "verified pass",
                hook=hook,
                detail=f"by `{verdict.by}`; check unavailable: {result.output}",
            )
        detail = result.output if result.status == "unavailable" else None
        return RuleOutcome(check_id, _REPORTED[result.status], hook=hook, detail=detail)

    outcomes = []
    for rule in item.rules:
        if rule.id in checked:
            continue
        verdict = held.verdict(rule.id) if held is not None else None
        if rule.id in derived:
            name = derived[rule.id]
            result = results.get(name)
            if result is None:
                outcomes.append(
                    RuleOutcome(rule.id, "not applicable", detail=f"check `{name}`")
                )
            elif result.status != "unavailable":
                outcomes.append(
                    RuleOutcome(
                        rule.id, _REPORTED[result.status], detail=f"check `{name}`"
                    )
                )
            elif verdict is not None and verdict.verdict == "pass":
                outcomes.append(
                    RuleOutcome(
                        rule.id,
                        "verified pass",
                        detail=f"by `{verdict.by}`; check `{name}` unavailable: "
                        f"{result.output}",
                    )
                )
            else:
                outcomes.append(
                    RuleOutcome(
                        rule.id,
                        "check unavailable",
                        detail=f"check `{name}`: {result.output}",
                    )
                )
        elif verdict is not None and verdict.verdict == "pass":
            outcomes.append(
                RuleOutcome(rule.id, "verified pass", detail=f"by `{verdict.by}`")
            )
        elif rule.id in record.rules_not_applicable:
            outcomes.append(
                RuleOutcome(
                    rule.id, "not applicable", detail="no changed file in scope"
                )
            )
        else:
            outcomes.append(RuleOutcome(rule.id, "self-declared"))
    outcomes.extend(
        reported(check.id, hook=check.source == "hook") for check in item.checks
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
    artifact carries none. The artifact of the completion rejected before
    this one, when there was one, follows the result as the item's previous
    attempt: a hold carries the draft it displaced, else the record does.
    """
    if artifact is None:
        return None, None
    record = state.item_executions[state.cursor]
    held = record.held_completion
    previous = held.previous_artifact if held is not None else record.draft_artifact

    def attribution(plan_item: PlanItem) -> str:
        if not isinstance(plan_item.operation, PlannedAction):
            return "auto"
        action = actions.get(plan_item.kind)
        return action.traits(plan_item.operation.payload).artifact_attribution

    def rendered(plan_item: PlanItem) -> str:
        step_number, step_total = step_label(
            plan_item.step_ordinals, snapshot.plan.items
        )
        return render_step_artifact(
            task_id=task_id,
            workflow=state.workflow,
            step=plan_item.step,
            step_number=step_number,
            step_total=step_total,
            skill=attribution(plan_item),
            result=artifact,
            rules=rules if plan_item is item else None,
            previous_attempt=previous if plan_item is item else None,
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
