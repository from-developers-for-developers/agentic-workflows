# SPDX-License-Identifier: GPL-3.0-or-later
"""Verification of rules without a command, and the operator's approval gate.

A rule without a command is never graded by the worker who did the step. When
the step's worker completes and its checks pass, ww holds the completion and
inserts verification items right before the step: ww-generated agent items,
one per distinct worker hint set among the rules, each asked about its rules:

- an ``unresolved`` rule (the store knows nothing, or the operator picked its
  reading) gets an interpretation and an approach: which command or tool
  would check it, and which check it would join (stage A);
- an ``approach-approved`` rule gets its check prepared, proven, and reported
  (stage B);
- a ``judged`` rule, and one the verifier could not convert, gets a verdict.

What the verifiers report goes into the rule-automation store as proposals,
never as approved checks. A failing verdict sends the step back to its worker
through the fix loop. A proposal stops the task for the operator
(``check_proposed``), who approves, rewrites, picks, or rejects; ww then
re-verifies what is left and finally records the held completion, running
any newly approved check on it first. Nothing is reasoned about twice: a
wording with an approved check is checked by it in every later step.

This module holds the pure parts: resolving a step's rules against the store,
choosing what a round must ask, the verification items and round records,
parsing and applying verifier results, and applying operator decisions. The
service orchestrates them inside ``complete`` and ``next``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from ww.actions import Commands, PlannedAction, Prompt, actions
from ww.contracts import (
    RuleResolutionStatus,
    RuleResultStatus,
    Verdict,
    VerificationState,
)
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
from ww.rule_store import (
    UNDECIDED_RULE_STATUSES,
    CheckEntry,
    CheckSpec,
    RuleAutomation,
    RuleEntry,
    is_check_name,
    parse_command,
)
from ww.transitions import Clock, project_steps
from ww.workflow_config import RuleHints

# The shortest rule-hash prefix the operator may type for a decision.
MIN_HASH_PREFIX = 8
HASH_LENGTH = 64
SKIPPED_ROUND = "skipped: nothing to verify in this round"
MIN_CANDIDATES = 2


def judged_rules(item: PlanItem) -> tuple[PlannedRule, ...]:
    """The step's rules without a command of their own."""
    return tuple(rule for rule in item.rules if not rule.has_command)


def to_verify(item: PlanItem, record: PlanItemExecution) -> tuple[PlannedRule, ...]:
    """The step's rules without a command that the operator did not waive."""
    waived = {key for key, _ in record.checks_waived}
    return tuple(rule for rule in judged_rules(item) if rule.id not in waived)


def resolve_rules(
    item: PlanItem, automation: RuleAutomation
) -> tuple[tuple[RuleResolution, ...], tuple[PlannedCheck, ...]]:
    """How each rule without a command is enforced, as the step begins.

    A rule whose wording has an approved check is checked by it; several
    rules sharing one check get one planned check that covers them all. A
    rule whose approach was approved, or whose reading the operator picked,
    is still unresolved: a verifier prepares or proposes its check.
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
            covered.setdefault(name, []).append(rule)
            specs[name] = check
            resolutions.append(
                RuleResolution(rule.id, "converted", name, interpretation)
            )
            continue
        status: RuleResolutionStatus
        if entry is None or entry.status in {"interpreted", "approach-approved"}:
            status = "unresolved"
        elif entry.status in UNDECIDED_RULE_STATUSES:
            status = "pending_operator"
        else:
            status = "judged"
        resolutions.append(RuleResolution(rule.id, status, None, interpretation))
    checks = tuple(
        derived_check(name, specs[name].spec, rules) for name, rules in covered.items()
    )
    return tuple(resolutions), checks


def derived_check(
    name: str, spec: CheckSpec, rules: list[PlannedRule]
) -> PlannedCheck:
    """One approved store check, planned for the step rules it covers.

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
    item: PlanItem, record: PlanItemExecution, automation: RuleAutomation
) -> tuple[VerificationRule, ...]:
    """The rules of a completing step that a verifier must still look at.

    Rules checked by a resolved derived check, rules with a verdict in the
    current hold, rules whose proposal from this step awaits the operator,
    and rules the operator waived for this step are done for now.
    """
    covered = {rule_id for check in record.resolved_checks for rule_id in check.covers}
    covered.update(key for key, _ in record.checks_waived)
    held = record.held_completion
    waiting = set(record.open_proposals)
    needs: list[VerificationRule] = []
    for rule in judged_rules(item):
        if rule.id in covered or (held is not None and held.verdict(rule.id)):
            continue
        entry = automation.rules.get(rule.text_hash)
        if rule.text_hash in waiting or (
            entry is not None and entry.check is not None and entry.check in waiting
        ):
            continue
        interpretation = entry.interpretation if entry else None
        if entry is None or entry.status == "interpreted":
            state: VerificationState = "unresolved"
        elif entry.status == "approach-approved":
            state = "approach-approved"
        else:
            state = "judged"
        needs.append(
            VerificationRule(
                id=rule.id,
                text=rule.text,
                text_hash=rule.text_hash,
                state=state,
                interpretation=interpretation,
                approach=entry.approach if entry and state != "unresolved" else None,
                check=entry.check if entry and state == "approach-approved" else None,
                pending_operator=(
                    state == "judged"
                    and entry is not None
                    and entry.status in UNDECIDED_RULE_STATUSES
                ),
            )
        )
    return tuple(needs)


def undecided_proposals(
    record: PlanItemExecution, automation: RuleAutomation
) -> tuple[str, ...]:
    """This step's proposals the operator has not decided yet."""
    return tuple(key for key in record.open_proposals if automation.undecided(key))


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
        f"Verify the rules of the `{item.name}` step. Another worker did "
        "that step's work; you judge it and do not change it. The "
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
    known = {
        entry.verifies.hints for entry in existing if entry.verifies is not None
    }
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
    opened: tuple[str, ...],
    now: Clock,
) -> ExecutionState:
    """Add one verifier's verdicts and proposals to the held step's record."""
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
        open_proposals=tuple(dict.fromkeys((*record.open_proposals, *opened))),
    )
    return replace(state, item_executions=tuple(records), updated_at=now())


def stop_for_proposals(
    state: ExecutionState,
    plan: WorkflowPlan,
    index: int,
    keys: tuple[str, ...],
    now: Clock,
) -> ExecutionState:
    """Stop the task for the operator to decide the step's proposals."""
    message = "proposals await the operator: " + ", ".join(_short(key) for key in keys)
    records = list(state.item_executions)
    records[index] = replace(records[index], status="failed", error=message)
    return project_steps(
        _without_assignment(
            replace(
                state,
                status="failed",
                failure_kind="check_proposed",
                cursor=index,
                active_item_id=plan.items[index].id,
                item_executions=tuple(records),
                last_error=message,
                updated_at=now(),
            )
        ),
        plan,
        now,
    )


def decide_proposals(
    state: ExecutionState, index: int, remaining: tuple[str, ...], now: Clock
) -> ExecutionState:
    """Keep only the proposals still undecided on the stopped step's record."""
    records = list(state.item_executions)
    records[index] = replace(records[index], open_proposals=remaining)
    return replace(state, item_executions=tuple(records), updated_at=now())


def add_resolved_checks(
    state: ExecutionState, index: int, checks: tuple[PlannedCheck, ...], now: Clock
) -> ExecutionState:
    """Enforce newly approved checks on the held step, replacing same names."""
    records = list(state.item_executions)
    record = records[index]
    names = {check.id for check in checks}
    covered = {rule_id: check.id for check in checks for rule_id in check.covers}
    records[index] = replace(
        record,
        resolved_checks=(
            *(check for check in record.resolved_checks if check.id not in names),
            *checks,
        ),
        rule_resolutions=tuple(
            replace(entry, status="converted", check=covered[entry.id])
            if entry.id in covered
            else entry
            for entry in record.rule_resolutions
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
    """What a verifier reported about one rule, validated against its state."""

    id: str
    status: RuleResultStatus
    interpretation: str | None = None
    check: str | None = None
    approach: str | None = None
    reason: str | None = None
    candidates: tuple[str, ...] = ()
    verdict: Verdict | None = None
    failures: tuple[JudgedFailure, ...] = ()


@dataclass(frozen=True)
class CheckProposal:
    """A check a verifier prepared for approved approaches (stage B)."""

    name: str
    command: Commands
    config: tuple[str, ...]
    covers: tuple[str, ...]
    proven: bool


_ALLOWED: dict[VerificationState, tuple[RuleResultStatus, ...]] = {
    "unresolved": ("approach", "not-convertible", "ambiguous"),
    "approach-approved": ("approach", "not-convertible"),
    "judged": ("judged",),
}
_RULE_RESULT_KEYS = {
    "id",
    "interpretation",
    "status",
    "check",
    "approach",
    "reason",
    "candidates",
    "verdict",
    "failures",
}
_CHECK_RESULT_KEYS = {
    "name",
    "argv",
    "shell",
    "args",
    "env",
    "assert",
    "config",
    "covers",
    "proven",
}


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
    unknown = set(data) - _RULE_RESULT_KEYS
    if unknown:
        raise StateError(f"{label} has unknown keys: " + ", ".join(sorted(unknown)))
    allowed = _ALLOWED[rule.state]
    status = data.get("status")
    if status not in allowed:
        raise StateError(
            f"{label}: status must be one of {', '.join(allowed)} for a rule that "
            f"is {rule.state}"
        )
    present = {key for key, value in data.items() if value is not None}

    def text(key: str, *, required: bool) -> str | None:
        value = data.get(key)
        if value is None:
            if required:
                raise StateError(f"{label}: {status} requires {key}")
            return None
        if not isinstance(value, str) or not value.strip():
            raise StateError(f"{label}: {key} must be a non-empty string")
        return value.strip()

    def forbid(*keys: str) -> None:
        extra = sorted(present & set(keys))
        if extra:
            raise StateError(f"{label}: {status} takes no {', '.join(extra)}")

    interpretation = text("interpretation", required=False)
    check = approach = reason = None
    candidates: tuple[str, ...] = ()
    if status == "approach":
        check = text("check", required=True)
        assert check is not None
        if not is_check_name(check):
            raise StateError(f"{label}: check must be a short kebab-case name")
        approach = text("approach", required=rule.state == "unresolved")
        forbid("reason", "candidates", "verdict", "failures")
    elif status == "not-convertible":
        reason = text("reason", required=True)
        forbid("check", "approach", "candidates")
    elif status == "ambiguous":
        value = data.get("candidates")
        if (
            not isinstance(value, list)
            or len(value) < MIN_CANDIDATES
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            raise StateError(f"{label}: ambiguous requires two or more candidates")
        candidates = tuple(item.strip() for item in value)
        forbid("check", "approach", "reason", "verdict", "failures")
    else:
        forbid("check", "approach", "reason", "candidates")
    verdict: Verdict | None = None
    failures: tuple[JudgedFailure, ...] = ()
    if status in {"judged", "not-convertible"}:
        value = data.get("verdict")
        if value not in {"pass", "fail"}:
            raise StateError(f"{label}: {status} requires verdict pass or fail")
        verdict = "pass" if value == "pass" else "fail"
        failures = _failures(data.get("failures"), label, verdict)
    return RuleResult(
        id=rule.id,
        status=status,
        interpretation=interpretation,
        check=check,
        approach=approach,
        reason=reason,
        candidates=candidates,
        verdict=verdict,
        failures=failures,
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


def parse_check_results(
    raw: tuple[str, ...],
    rules: tuple[VerificationRule, ...],
    results: tuple[RuleResult, ...],
    item: PlanItem,
) -> tuple[CheckProposal, ...]:
    """One validated check per check a stage-B rule names, and no other."""
    states = {rule.id: rule.state for rule in rules}
    naming: dict[str, list[str]] = {}
    for result in results:
        if result.status == "approach" and states[result.id] == "approach-approved":
            assert result.check is not None
            naming.setdefault(result.check, []).append(result.id)
    judged = {rule.id for rule in judged_rules(item)}
    parsed: dict[str, CheckProposal] = {}
    for text in raw:
        data = _json_object(text, "--check-result")
        unknown = set(data) - _CHECK_RESULT_KEYS
        if unknown:
            raise StateError(
                "--check-result has unknown keys: " + ", ".join(sorted(unknown))
            )
        name = data.get("name")
        if not isinstance(name, str) or not is_check_name(name):
            raise StateError("--check-result requires a short kebab-case name")
        label = f"--check-result {name!r}"
        if name not in naming:
            raise StateError(
                f"{label} is named by no rule prepared in this verification"
            )
        if name in parsed:
            raise StateError(f"{label} is given twice")
        command = parse_command(
            {
                key: data[key]
                for key in ("argv", "shell", "args", "env", "assert")
                if key in data and data[key] is not None
            },
            label,
        )
        config = data.get("config", [])
        if not isinstance(config, list) or not all(
            isinstance(path, str) and path for path in config
        ):
            raise StateError(f"{label}: config must be a list of project paths")
        covers = data.get("covers")
        if (
            not isinstance(covers, list)
            or not covers
            or not all(isinstance(rule_id, str) for rule_id in covers)
        ):
            raise StateError(f"{label}: covers must list the rule IDs it checks")
        strangers = [rule_id for rule_id in covers if rule_id not in judged]
        if strangers:
            raise StateError(
                f"{label} covers {', '.join(strangers)}, which are not rules "
                "without a command of this step"
            )
        uncovered = [rule_id for rule_id in naming[name] if rule_id not in covers]
        if uncovered:
            raise StateError(
                f"{label} must cover the rules that name it: " + ", ".join(uncovered)
            )
        proven = data.get("proven")
        if not isinstance(proven, bool):
            raise StateError(f"{label}: proven must be true or false")
        parsed[name] = CheckProposal(
            name, command, tuple(config), tuple(dict.fromkeys(covers)), proven
        )
    missing = [name for name in naming if name not in parsed]
    if missing:
        raise StateError("--check-result is missing for " + ", ".join(missing))
    return tuple(parsed[name] for name in naming)


def _json_object(text: str, flag: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise StateError(f"{flag} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise StateError(f"{flag} must be a JSON object")
    return data


def verdicts_of(
    results: tuple[RuleResult, ...], by: str
) -> tuple[RuleVerdict, ...]:
    return tuple(
        RuleVerdict(
            result.id,
            result.verdict,
            by,
            tuple(failure.text() for failure in result.failures),
        )
        for result in results
        if result.verdict is not None
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


def record_results(
    automation: RuleAutomation,
    rules: tuple[VerificationRule, ...],
    results: tuple[RuleResult, ...],
    checks: tuple[CheckProposal, ...],
    item: PlanItem,
    by: str,
    now: str,
) -> tuple[RuleAutomation, tuple[str, ...], tuple[str, ...]]:
    """Write a verifier's results into the store; return proposals and notices.

    Stage A writes approaches, unconvertible rules, and ambiguous ones; stage
    B writes each prepared check as ``proposed``, never ``converted``, and
    its rules as ``proposed``. An entry that moved on since the verification
    began, say another task's decision, is not overwritten: a notice says so.
    A prepared extension of an approved check becomes its pending revision,
    so the approved command keeps running until the operator approves it.
    """
    hashes = {rule.id: rule.text_hash for rule in item.rules}
    opened: list[str] = []
    notices: list[str] = []
    for rule, result in zip(rules, results, strict=True):
        if rule.state == "judged":
            continue
        entry = automation.rules.get(rule.text_hash)
        current = entry.status if entry is not None else None
        expected = (
            {None, "interpreted"}
            if rule.state == "unresolved"
            else {"approach-approved"}
        )
        # The verifier's own earlier write, from a completion interrupted
        # before the task state recorded it, is rewritten, not refused.
        own = entry is not None and entry.proposed_in == by
        if current not in expected and not own:
            notices.append(
                f"rule {rule.id}: the store already has it as {current}; not "
                "overwritten"
            )
            continue
        interpretation = result.interpretation or rule.interpretation
        if result.status == "approach" and rule.state == "unresolved":
            updated = RuleEntry(
                text=rule.text,
                status="approach-proposed",
                interpretation=interpretation,
                approach=result.approach,
                check=result.check,
                extends=result.check in automation.checks,
                proposed_in=by,
            )
            opened.append(rule.text_hash)
        elif result.status == "approach":
            assert entry is not None
            updated = replace(
                entry,
                status="proposed",
                interpretation=interpretation,
                approach=result.approach or entry.approach,
                check=result.check,
                proposed_in=by,
            )
        elif result.status == "not-convertible":
            updated = RuleEntry(
                text=rule.text,
                status="not-convertible",
                interpretation=interpretation,
                reason=result.reason,
                proposed_in=by,
            )
        else:
            updated = RuleEntry(
                text=rule.text,
                status="ambiguous",
                interpretation=interpretation,
                candidates=result.candidates,
                proposed_in=by,
            )
            opened.append(rule.text_hash)
        automation = automation.with_rule(rule.text_hash, updated)
    for check in checks:
        spec = CheckSpec(
            check.command,
            check.config,
            tuple(hashes[rule_id] for rule_id in check.covers),
            check.proven,
        )
        existing = automation.checks.get(check.name)
        if existing is not None and existing.status == "converted":
            revised = replace(existing, pending=spec, proposed_in=by)
        else:
            revised = CheckEntry(spec, "proposed", proposed_at=now, proposed_in=by)
        automation = automation.with_check(check.name, revised)
        opened.append(check.name)
    return automation, tuple(dict.fromkeys(opened)), tuple(notices)


# --- The operator's decisions -------------------------------------------------


@dataclass(frozen=True)
class Decisions:
    """What the operator decided at a ``check_proposed`` stop."""

    approve: tuple[str, ...] = ()
    approaches: tuple[tuple[str, str], ...] = ()
    picks: tuple[tuple[str, int], ...] = ()
    reject: str | None = None

    def __bool__(self) -> bool:
        return bool(self.approve or self.approaches or self.picks) or (
            self.reject is not None
        )


def resolve_key(key: str, keys: tuple[str, ...]) -> str:
    """A proposal by check name or rule hash; a hash may be a unique prefix."""
    if key in keys:
        return key
    matches = [
        candidate
        for candidate in keys
        if len(key) >= MIN_HASH_PREFIX and candidate.startswith(key)
    ]
    if len(matches) == 1:
        return matches[0]
    raise StateError(
        f"{key!r} is not an undecided proposal of this stop; choose from "
        + ", ".join(_short(candidate) for candidate in keys)
    )


def apply_decisions(
    automation: RuleAutomation,
    keys: tuple[str, ...],
    decisions: Decisions,
    now: str,
) -> tuple[RuleAutomation, tuple[str, ...], tuple[str, ...]]:
    """Apply the operator's decisions to the stop's undecided proposals.

    Returns the store, the proposals still undecided, and the checks now
    approved. Approving a rule's approach lets a verifier prepare its check;
    approving a check converts it and every rule it covers, or replaces the
    approved command with its pending revision. ``reject`` rejects every
    proposal still undecided.
    """
    approved: list[str] = []
    for key in decisions.approve:
        name = resolve_key(key, keys)
        if name in automation.checks:
            check = automation.checks[name]
            if check.pending is not None:
                check = replace(check, spec=check.pending, pending=None)
            elif check.status != "proposed":
                raise StateError(f"check {name!r} has nothing to approve")
            check = replace(check, status="converted", approved_at=now)
            automation = automation.with_check(name, check)
            for text_hash in check.spec.covers:
                entry = automation.rules.get(text_hash)
                if entry is not None:
                    automation = automation.with_rule(
                        text_hash, replace(entry, status="converted", check=name)
                    )
            approved.append(name)
            continue
        entry = automation.rules[name]
        if entry.status == "ambiguous":
            raise StateError(
                f"rule {_short(name)} is ambiguous: choose a reading with --pick"
            )
        if entry.status != "approach-proposed":
            raise StateError(f"rule {_short(name)} has no approach to approve")
        automation = automation.with_rule(
            name, replace(entry, status="approach-approved")
        )
    for key, text in decisions.approaches:
        name = resolve_key(key, keys)
        entry = automation.rules.get(name)
        if entry is None or entry.status != "approach-proposed":
            raise StateError(f"{_short(name)} is not a rule with a proposed approach")
        if not text.strip():
            raise StateError("--approach needs the approach in words")
        automation = automation.with_rule(
            name, replace(entry, status="approach-approved", approach=text.strip())
        )
    for key, number in decisions.picks:
        name = resolve_key(key, keys)
        entry = automation.rules.get(name)
        if entry is None or entry.status != "ambiguous":
            raise StateError(f"{_short(name)} is not an ambiguous rule")
        if not 1 <= number <= len(entry.candidates):
            raise StateError(
                f"--pick for {_short(name)} takes a reading from 1 to "
                f"{len(entry.candidates)}"
            )
        automation = automation.with_rule(
            name,
            replace(
                entry,
                status="interpreted",
                interpretation=entry.candidates[number - 1],
            ),
        )
    if decisions.reject is not None:
        reason = f"operator: {decisions.reject}"
        for name in keys:
            if not automation.undecided(name):
                continue
            if name in automation.checks:
                check = automation.checks[name]
                proposed = check.pending or check.spec
                automation = automation.with_check(
                    name,
                    replace(check, pending=None)
                    if check.status == "converted"
                    else replace(check, status="rejected"),
                )
                # Its rules were waiting for this check: they are rejected too.
                for text_hash in proposed.covers:
                    entry = automation.rules.get(text_hash)
                    if entry is not None and (entry.status, entry.check) == (
                        "proposed",
                        name,
                    ):
                        automation = automation.with_rule(
                            text_hash,
                            replace(entry, status="rejected", reason=reason),
                        )
            else:
                automation = automation.with_rule(
                    name,
                    replace(automation.rules[name], status="rejected", reason=reason),
                )
    remaining = tuple(key for key in keys if automation.undecided(key))
    return automation, remaining, tuple(approved)


def approved_checks(
    item: PlanItem, automation: RuleAutomation, names: tuple[str, ...]
) -> tuple[PlannedCheck, ...]:
    """Newly approved checks, planned for the step rules they cover."""
    rules = judged_rules(item)
    result = []
    for name in names:
        check = automation.checks[name]
        covered = [rule for rule in rules if rule.text_hash in check.spec.covers]
        if covered:
            result.append(derived_check(name, check.spec, covered))
    return tuple(result)


def _short(key: str) -> str:
    """A rule hash as the operator types it; a check name as is."""
    return key[:12] if len(key) == HASH_LENGTH else key
