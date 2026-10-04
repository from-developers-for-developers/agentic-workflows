# SPDX-License-Identifier: GPL-3.0-or-later
"""Verification of rules without a command.

A rule without a command is never graded by the worker who did the step. As
the step begins, each such rule resolves against the rule-automation store:
a rule whose wording has a converted check is checked by it, where the
check's configuration files exist; every other rule is judged. When the
step's worker completes and its checks pass, ww holds the completion and
inserts verification items right before the step: ww-generated agent items,
one per distinct worker hint set among the judged rules, each giving a
``pass`` or ``fail`` verdict on its rules. A verifier never writes the store:
turning rules into checks is ``ww-scriptize-rules``'s job, outside tasks.

A failing verdict sends the step back to its worker through the fix loop;
once every rule passes, ww records the held completion.

This module holds the pure parts: resolving a step's rules against the store,
choosing what a round must ask, the verification items and round records,
and parsing verifier results. The service orchestrates them inside
``complete`` and ``next``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ww.actions import PlannedAction, Prompt, actions
from ww.contracts import Verdict
from ww.errors import StateError
from ww.execution_models import (
    CheckReport,
    CheckResult,
    ExecutionState,
    PlanSnapshot,
    build_step_projection,
    new_item_execution,
    operation_scope_for,
)
from ww.execution_models.records import (
    HeldCompletion,
    PlanItemExecution,
    RuleResolution,
    RuleVerdict,
    VerificationRule,
)
from ww.plan import (
    PlanItem,
    PlannedCheck,
    PlannedRule,
    VerificationTarget,
    WorkflowPlan,
    number_step_paths,
)
from ww.rule_store import CheckEntry, CheckSpec, RuleAutomation, is_config_path
from ww.transitions import Clock, project_steps
from ww.workflow_config import RuleHints

SKIPPED_ROUND = "skipped: nothing to verify in this round"


def judged_rules(item: PlanItem) -> tuple[PlannedRule, ...]:
    """The step's rules without a command of their own."""
    return tuple(rule for rule in item.rules if not rule.has_command)


def to_verify(item: PlanItem, record: PlanItemExecution) -> tuple[PlannedRule, ...]:
    """The step's rules without a command that the operator did not waive."""
    waived = {key for key, _ in record.checks_waived}
    return tuple(rule for rule in judged_rules(item) if rule.id not in waived)


def resolve_rules(
    item: PlanItem,
    automation: RuleAutomation,
    *,
    directory: Path | None = None,
) -> tuple[tuple[RuleResolution, ...], tuple[PlannedCheck, ...]]:
    """How each rule without a command is enforced, as the step begins.

    A rule whose wording has a converted check is checked by it; several
    rules sharing one check get one planned check that covers them all.
    Every other rule is judged by a verifier, whatever else the store says
    about it. With ``directory``, the one the step's checks run in, a
    converted check whose configuration files are not all there, such as one
    built on a branch not merged yet, does not apply: its rules are judged,
    naming the missing file.
    """
    resolutions: list[RuleResolution] = []
    covered: dict[str, list[PlannedRule]] = {}
    specs: dict[str, CheckEntry] = {}
    for rule in judged_rules(item):
        entry = automation.rules.get(rule.text_hash)
        interpretation = entry.interpretation if entry else None
        converted = automation.converted_check(rule.text_hash)
        if converted is not None:
            name, check = converted
            missing = _missing_config(check.spec, directory)
            if missing is not None:
                resolutions.append(
                    RuleResolution(
                        id=rule.id,
                        status="judged",
                        check=name,
                        interpretation=interpretation,
                        missing=missing,
                    )
                )
                continue
            covered.setdefault(name, []).append(rule)
            specs[name] = check
            resolutions.append(
                RuleResolution(rule.id, "converted", name, interpretation)
            )
            continue
        resolutions.append(RuleResolution(rule.id, "judged", None, interpretation))
    checks = tuple(
        derived_check(name, specs[name].spec, rules) for name, rules in covered.items()
    )
    return tuple(resolutions), checks


def _missing_config(spec: CheckSpec, directory: Path | None) -> str | None:
    """The first of a check's configuration files ``directory`` lacks.

    A path that would reach outside ``directory`` (absolute, or with a ``..``
    part, as only a hand-edited store holds) counts as missing.
    """
    if directory is None:
        return None
    return next(
        (
            path
            for path in spec.config
            if not is_config_path(path) or not (directory / path).exists()
        ),
        None,
    )


def derived_check(name: str, spec: CheckSpec, rules: list[PlannedRule]) -> PlannedCheck:
    """One converted store check, planned for the step rules it covers.

    Its globs are the union of its rules' globs, or none when any covered
    rule applies to every file; it may fail as often as the most lenient of
    its rules allows.
    """
    paths: tuple[str, ...] = (
        ()
        if any(not rule.paths for rule in rules)
        else tuple(dict.fromkeys(path for rule in rules for path in rule.paths))
    )
    return PlannedCheck(
        id=name,
        source="derived",
        summary=f"check {name}",
        command=spec.command,
        paths=paths,
        max_fixes=max(rule.max_fixes for rule in rules),
        covers=tuple(rule.id for rule in rules),
    )


def verification_needs(
    item: PlanItem, record: PlanItemExecution
) -> tuple[VerificationRule, ...]:
    """The rules of a completing step that a verifier must still judge.

    Rules checked by a resolved derived check, rules with a verdict in the
    current hold, and rules the operator waived for this step are done for
    now. What the step began with says the rest: a rule's reading, and the
    converted check whose configuration is missing here.
    """
    covered = {rule_id for check in record.resolved_checks for rule_id in check.covers}
    covered.update(key for key, _ in record.checks_waived)
    held = record.held_completion
    began = {resolution.id: resolution for resolution in record.rule_resolutions}
    needs: list[VerificationRule] = []
    for rule in judged_rules(item):
        if rule.id in covered or (held is not None and held.verdict(rule.id)):
            continue
        resolution = began.get(rule.id)
        needs.append(
            VerificationRule(
                id=rule.id,
                text=rule.text,
                text_hash=rule.text_hash,
                interpretation=resolution.interpretation if resolution else None,
                check=resolution.check if resolution and resolution.missing else None,
                missing=resolution.missing if resolution else None,
            )
        )
    return tuple(needs)


def effective_hints(rule: PlannedRule, item: PlanItem) -> RuleHints:
    """The worker a rule is verified by: the rule's hints, else the step's."""
    return RuleHints(
        rule.hints.agent or item.requested_agent,
        rule.hints.model or item.requested_model,
        rule.hints.reasoning or item.requested_reasoning,
    )


def verification_item(item: PlanItem, ordinal: int, hints: RuleHints) -> PlanItem:
    """A ww-generated agent item that verifies ``item``'s rules for ``hints``.

    It sits in the step's ``before_complete`` phase, which is when it runs:
    the step's completion is held until it is done. It carries nothing of the
    step's own work, and always allows a subagent, so the verifier is never
    the worker who did the step unless one session performs every step.
    """
    text = (
        f"Verify the rules of the `{item.name}` step: judge its work from the "
        "change set and the rules alone, and do not change it. The "
        "Verification section lists the rules, the change set, and what to "
        "report for each rule."
    )
    prompt = actions.get("prompt")

    def concrete(value: str | None, fallback: str | None) -> str | None:
        return value if value is not None and value != "auto" else fallback

    return replace(
        item,
        id=f"{item.workflow}:{item.step}:verify:{ordinal}",
        name=f"{item.name}-verify-{ordinal}",
        description=text,
        operation=PlannedAction("prompt", Prompt(text)),
        owner=prompt.owner,
        execution=prompt.execution,
        requires_agent_input=False,
        phase="before_complete",
        source="internal",
        registered_handler=None,
        provide=(),
        save_metadata=(),
        update_document=(),
        update_item=(),
        outputs=(),
        dependencies=(),
        requested_agent=hints.agent,
        requested_model=hints.model,
        requested_reasoning=hints.reasoning,
        role="worker",
        interactive=False,
        choices=(),
        ui=False,
        model=concrete(hints.model, item.model),
        reasoning=concrete(hints.reasoning, item.reasoning),
        profile=None,
        profile_instruction=None,
        profile_path=None,
        summary=False,
        item_operation=None,
        item_template=False,
        item_collect_only=False,
        shared_items=False,
        item_identity=None,
        item_unique=(),
        split_instruction=None,
        artifact=True,
        child_operation=None,
        child_identity=False,
        artifact_dependency=None,
        loop_break=None,
        loop_continue=None,
        assessment_question=None,
        assessment_outcomes=(),
        assessment_stops=(),
        assessment_parent=None,
        assessment_outcome=None,
        rules=(),
        checks=(),
        modes=(),
        verifies=VerificationTarget(item.id, ordinal, hints),
    )


def verification_items(plan: WorkflowPlan, item_id: str) -> tuple[int, ...]:
    """The plan indexes of the verification items of one agent item."""
    return tuple(
        index
        for index, entry in enumerate(plan.items)
        if entry.verifies is not None and entry.verifies.item_id == item_id
    )


def index_of(plan: WorkflowPlan, item_id: str) -> int:
    for index, entry in enumerate(plan.items):
        if entry.id == item_id:
            return index
    raise StateError(f"plan item {item_id!r} is not in the task snapshot")


def hold_completion(
    state: ExecutionState,
    plan: WorkflowPlan,
    held: HeldCompletion,
    artifact: str | None,
    now: Clock,
) -> ExecutionState:
    """Keep the active item's completion, unrecorded, until its rules are verified.

    Verdicts of an earlier round of the same hold are kept. The worker's
    assignment ends: the verifiers are other workers.
    """
    records = list(state.item_executions)
    record = records[state.cursor]
    earlier = record.held_completion
    records[state.cursor] = replace(
        record,
        held_completion=(
            replace(held, verdicts=earlier.verdicts) if earlier is not None else held
        ),
        draft_artifact=artifact,
        status="pending",
    )
    return project_steps(
        _without_assignment(
            replace(
                state,
                status="pending",
                active_item_id=None,
                item_executions=tuple(records),
                updated_at=now(),
            )
        ),
        plan,
        now,
    )


def open_verification_round(
    state: ExecutionState,
    snapshot: PlanSnapshot,
    item: PlanItem,
    needs: tuple[VerificationRule, ...],
    now: Clock,
) -> tuple[ExecutionState, PlanSnapshot]:
    """Give each hint set of ``needs`` a verification item and start the round.

    A hint set without an item yet gets one, inserted right before the step
    as a new plan revision; items from earlier rounds are reused, their
    previous records kept in the history. An item with nothing to ask this
    round is recorded as skipped. The cursor moves to the first item asked.
    """
    rules = {rule.id: rule for rule in item.rules}
    groups: dict[RuleHints, list[VerificationRule]] = {}
    for need in needs:
        groups.setdefault(effective_hints(rules[need.id], item), []).append(need)
    plan = snapshot.plan
    existing = [plan.items[index] for index in verification_items(plan, item.id)]
    known = {entry.verifies.hints for entry in existing if entry.verifies is not None}
    added = [
        verification_item(item, len(existing) + number, hints)
        for number, hints in enumerate(
            (hints for hints in groups if hints not in known), 1
        )
    ]
    if added:
        at = index_of(plan, item.id)
        items = (*plan.items[:at], *added, *plan.items[at:])
        numbered = tuple(
            replace(entry, position=index) for index, entry in enumerate(items, 1)
        )
        plan = replace(plan, items=number_step_paths(numbered))
        previous = {record.plan_item_id: record for record in state.item_executions}
        scope = operation_scope_for(state)
        snapshot = replace(
            snapshot, plan=plan, plan_revision=snapshot.plan_revision + 1
        )
        state = replace(
            state,
            item_executions=tuple(
                replace(previous[entry.id], position=entry.position)
                if entry.id in previous
                else new_item_execution(state.task_id, scope, entry)
                for entry in plan.items
            ),
            steps=build_step_projection(plan, state.steps),
            plan_revision=snapshot.plan_revision,
            plan_digest=snapshot.plan_digest,
        )
    records = list(state.item_executions)
    history = list(state.execution_history)
    scope = operation_scope_for(state)
    first: int | None = None
    for index in verification_items(plan, item.id):
        entry = plan.items[index]
        assert entry.verifies is not None
        earlier = records[index]
        if earlier.status != "pending" or earlier.verification:
            history.append(earlier)
        fresh = new_item_execution(state.task_id, scope, entry)
        asked = groups.get(entry.verifies.hints)
        if asked:
            records[index] = replace(fresh, verification=tuple(asked))
            first = index if first is None else first
        else:
            records[index] = replace(
                fresh, status="completed", completed_at=now(), result=SKIPPED_ROUND
            )
    if first is None:  # pragma: no cover - callers open a round only with needs
        raise StateError("a verification round needs at least one rule")
    return (
        project_steps(
            _without_assignment(
                replace(
                    state,
                    status="pending",
                    cursor=first,
                    active_item_id=None,
                    item_executions=tuple(records),
                    execution_history=tuple(history),
                    updated_at=now(),
                )
            ),
            plan,
            now,
        ),
        snapshot,
    )


def round_open(state: ExecutionState, plan: WorkflowPlan, item_id: str) -> bool:
    """Whether a verification item of ``item_id`` is still to be performed."""
    return any(
        state.item_executions[index].status in {"pending", "in_progress"}
        and state.item_executions[index].verification
        for index in verification_items(plan, item_id)
    )


def close_round(
    state: ExecutionState, plan: WorkflowPlan, item_id: str, now: Clock
) -> ExecutionState:
    """End a round early: its verifiers not yet performed are skipped.

    A failing verdict sends the step back to its worker, so what the others
    would judge is judged again on the revised work.
    """
    records = list(state.item_executions)
    for index in verification_items(plan, item_id):
        if records[index].status == "pending" and records[index].verification:
            records[index] = replace(
                records[index],
                status="completed",
                completed_at=now(),
                result="skipped: an earlier verdict of the round failed",
            )
    return replace(state, item_executions=tuple(records), updated_at=now())


def record_round(
    state: ExecutionState,
    index: int,
    verdicts: tuple[RuleVerdict, ...],
    now: Clock,
) -> ExecutionState:
    """Add one verifier's verdicts to the held step's record."""
    records = list(state.item_executions)
    record = records[index]
    held = record.held_completion
    if held is None:
        raise StateError("the verified step has no held completion")
    replaced = {verdict.id for verdict in verdicts}
    records[index] = replace(
        record,
        held_completion=replace(
            held,
            verdicts=(
                *(entry for entry in held.verdicts if entry.id not in replaced),
                *verdicts,
            ),
        ),
    )
    return replace(state, item_executions=tuple(records), updated_at=now())


def resume_held(
    state: ExecutionState, plan: WorkflowPlan, index: int, now: Clock
) -> ExecutionState:
    """Reopen the held step so ww can record its completion as submitted."""
    records = list(state.item_executions)
    records[index] = replace(records[index], status="in_progress", error=None)
    return project_steps(
        _without_assignment(
            replace(
                state,
                status="in_progress",
                cursor=index,
                active_item_id=plan.items[index].id,
                item_executions=tuple(records),
                last_error=None,
                failure_kind=None,
                updated_at=now(),
            )
        ),
        plan,
        now,
    )


def skip_idle_verification(state: ExecutionState, now: Clock) -> ExecutionState:
    """Pass a verification item that no round asked anything, such as after a
    loop reset its record."""
    records = list(state.item_executions)
    records[state.cursor] = replace(
        records[state.cursor],
        status="completed",
        completed_at=now(),
        result=SKIPPED_ROUND,
    )
    return replace(
        state,
        cursor=state.cursor + 1,
        item_executions=tuple(records),
        updated_at=now(),
    )


def _without_assignment(state: ExecutionState) -> ExecutionState:
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


# --- Verifier results -------------------------------------------------------


@dataclass(frozen=True)
class JudgedFailure:
    """One piece of a verifier's evidence for a failing verdict."""

    file: str
    what: str
    line: int | None = None

    def text(self) -> str:
        location = f"{self.file}:{self.line}" if self.line is not None else self.file
        return f"{location} — {self.what}"


@dataclass(frozen=True)
class RuleResult:
    """A verifier's verdict on one rule, with its evidence when it fails."""

    id: str
    verdict: Verdict
    failures: tuple[JudgedFailure, ...] = ()


_RULE_RESULT_KEYS = {"id", "status", "verdict", "failures"}


def parse_rule_results(
    raw: tuple[str, ...], rules: tuple[VerificationRule, ...]
) -> tuple[RuleResult, ...]:
    """One validated result per rule the verification item covers, in order."""
    by_id = {rule.id: rule for rule in rules}
    parsed: dict[str, RuleResult] = {}
    for text in raw:
        data = _json_object(text, "--rule-result")
        rule_id = data.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise StateError("--rule-result requires the rule's id")
        if rule_id not in by_id:
            raise StateError(
                f"--rule-result names {rule_id!r}, which this verification does "
                "not cover; it covers " + ", ".join(by_id)
            )
        if rule_id in parsed:
            raise StateError(f"--rule-result for {rule_id!r} is given twice")
        parsed[rule_id] = _rule_result(data, by_id[rule_id])
    missing = [rule.id for rule in rules if rule.id not in parsed]
    if missing:
        raise StateError("--rule-result is missing for " + ", ".join(missing))
    return tuple(parsed[rule.id] for rule in rules)


def _rule_result(data: dict[str, Any], rule: VerificationRule) -> RuleResult:
    label = f"--rule-result for {rule.id!r}"
    # A verifier only judges: the status says so before anything else.
    if data.get("status") != "judged":
        raise StateError(f"{label}: status must be judged")
    unknown = set(data) - _RULE_RESULT_KEYS
    if unknown:
        raise StateError(f"{label} has unknown keys: " + ", ".join(sorted(unknown)))
    value = data.get("verdict")
    if value not in {"pass", "fail"}:
        raise StateError(f"{label}: judged requires verdict pass or fail")
    verdict: Verdict = "pass" if value == "pass" else "fail"
    return RuleResult(
        id=rule.id,
        verdict=verdict,
        failures=_failures(data.get("failures"), label, verdict),
    )


def _failures(value: Any, label: str, verdict: Verdict) -> tuple[JudgedFailure, ...]:
    if verdict == "pass":
        if value not in (None, []):
            raise StateError(f"{label}: a pass verdict lists no failures")
        return ()
    if not isinstance(value, list) or not value:
        raise StateError(
            f"{label}: a fail verdict needs failures, each {{file, line?, what}}"
        )
    result = []
    for entry in value:
        if not isinstance(entry, dict) or not set(entry) <= {"file", "line", "what"}:
            raise StateError(f"{label}: each failure is an object of file, line, what")
        file, what, line = entry.get("file"), entry.get("what"), entry.get("line")
        if not (isinstance(file, str) and file and isinstance(what, str) and what):
            raise StateError(f"{label}: each failure needs a file and what")
        if line is not None and (
            not isinstance(line, int) or isinstance(line, bool) or line < 1
        ):
            raise StateError(f"{label}: a failure's line is a positive integer")
        result.append(JudgedFailure(file, what, line))
    return tuple(result)


def _json_object(text: str, flag: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise StateError(f"{flag} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise StateError(f"{flag} must be a JSON object")
    return data


def verdicts_of(results: tuple[RuleResult, ...], by: str) -> tuple[RuleVerdict, ...]:
    return tuple(
        RuleVerdict(
            result.id,
            result.verdict,
            by,
            tuple(failure.text() for failure in result.failures),
        )
        for result in results
    )


def judged_report(
    verdicts: tuple[RuleVerdict, ...],
    attempt: int,
    checked_at: str,
    held: HeldCompletion,
) -> CheckReport:
    """A round's verdicts as a check report, so failures join the fix loop."""
    return CheckReport(
        attempt=attempt,
        checked_at=checked_at,
        results=tuple(
            CheckResult(
                verdict.id,
                "judged",
                "passed" if verdict.verdict == "pass" else "failed",
                output="\n".join(verdict.failures),
            )
            for verdict in verdicts
        ),
        mark=held.mark,
        all_files=held.all_files,
    )


# --- Revoking a check ----------------------------------------------------------


def revoke_check(
    automation: RuleAutomation, name: str, reason: str
) -> tuple[RuleAutomation, tuple[str, ...]]:
    """Reject a converted or proposed check and the rules it covers.

    The rules are judged by a verifier from then on. A pending revision is
    dropped with it. Returns the store and the text hashes of the rules
    rejected. Nothing outside the store changes.
    """
    check = automation.checks.get(name)
    if check is None:
        raise StateError(f"the rule-automation store has no check {name!r}")
    if check.status == "rejected":
        raise StateError(f"check {name!r} is already rejected")
    covers = tuple(
        dict.fromkeys(
            (*check.spec.covers, *(check.pending.covers if check.pending else ()))
        )
    )
    automation = automation.with_check(
        name, replace(check, status="rejected", pending=None, reason=reason)
    )
    rejected: list[str] = []
    for text_hash in covers:
        entry = automation.rules.get(text_hash)
        if entry is None or entry.check != name or entry.status == "rejected":
            continue
        automation = automation.with_rule(
            text_hash, replace(entry, status="rejected", reason=reason)
        )
        rejected.append(text_hash)
    return automation, tuple(rejected)
