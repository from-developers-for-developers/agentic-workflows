# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-rule statistics: how often each rule applied, was evaluated, and failed.

Every settled completion of a step counts once per rule it evaluated: one
accepted, or one rejected because a check or a verifier's verdict failed. A
rule's check reports its result, a verdict in the held completion judges its
rule, a waiver counts as waived, and the change set's narrowing counts as not
applicable. The counts live in ``.ww/rules/stats.json``, ww-owned and local
to the checkout, keyed by rule ID with the hash of the wording last
evaluated alongside, so ``ww rules`` and ``ww rules stats`` show which
checked rules fail often and which rules are never applied. A count that
cannot be written never fails the completion that produced it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from ww.errors import LockError
from ww.execution_models import CheckReport, PlanItemExecution
from ww.plan import PlanItem
from ww.rule_conversion import every_rule
from ww.workflow_config import RuleDefinition, WorkflowConfiguration

if TYPE_CHECKING:
    from ww.storage import Storage

STATS_FILE = ".ww/rules/stats.json"
STATS_SCHEMA = 1
# A rule left out or waived this often without ever applying is worth a
# second look: ``lint`` hints at it.
NEVER_APPLIED_THRESHOLD = 5

Outcome = Literal[
    "check passed",
    "check failed",
    "verdict pass",
    "verdict fail",
    "waived",
    "not applicable",
]
FailureKind = Literal["check", "verdict"]


@dataclass(frozen=True)
class RuleEvaluation:
    """What one settled completion decided about one rule."""

    id: str
    text_hash: str
    outcome: Outcome


@dataclass(frozen=True)
class RuleFailure:
    """Where a rule last failed."""

    task: str
    step: str
    kind: FailureKind


@dataclass(frozen=True)
class RuleStats:
    """One rule's counters, zero until a completion evaluates it.

    ``applied`` counts the evaluations that applied the rule, checked or
    judged; ``waived`` and ``not_applicable`` are the evaluations that did
    not. ``text_hash`` is the wording last evaluated: the counts follow the
    rule's ID across rewordings.
    """

    applied: int = 0
    checked: int = 0
    check_failures: int = 0
    judged: int = 0
    judged_failures: int = 0
    waived: int = 0
    not_applicable: int = 0
    last_applied_at: str | None = None
    last_failed_at: str | None = None
    last_failure: RuleFailure | None = None
    text_hash: str | None = None

    @property
    def failures(self) -> int:
        return self.check_failures + self.judged_failures

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> RuleStats:
        if not isinstance(data, dict):
            raise ValueError("rule stats must be a mapping")
        failure = data.get("last_failure")
        return cls(
            **{
                name: _count(data.get(name))
                for name in (
                    "applied",
                    "checked",
                    "check_failures",
                    "judged",
                    "judged_failures",
                    "waived",
                    "not_applicable",
                )
            },
            last_applied_at=_optional_string(data.get("last_applied_at")),
            last_failed_at=_optional_string(data.get("last_failed_at")),
            last_failure=(
                RuleFailure(
                    _string(failure.get("task")),
                    _string(failure.get("step")),
                    _failure_kind(failure.get("kind")),
                )
                if isinstance(failure, dict)
                else None
            ),
            text_hash=_optional_string(data.get("text_hash")),
        )

    def counting(
        self, evaluation: RuleEvaluation, task_id: str, step: str, now: str
    ) -> RuleStats:
        """These counters with one more evaluation."""
        stats = replace(self, text_hash=evaluation.text_hash)
        outcome = evaluation.outcome
        if outcome == "waived":
            return replace(stats, waived=stats.waived + 1)
        if outcome == "not applicable":
            return replace(stats, not_applicable=stats.not_applicable + 1)
        stats = replace(stats, applied=stats.applied + 1, last_applied_at=now)
        if outcome.startswith("check"):
            stats = replace(stats, checked=stats.checked + 1)
        else:
            stats = replace(stats, judged=stats.judged + 1)
        if outcome == "check passed" or outcome == "verdict pass":
            return stats
        kind: FailureKind = "check" if outcome == "check failed" else "verdict"
        return replace(
            stats,
            check_failures=stats.check_failures + (kind == "check"),
            judged_failures=stats.judged_failures + (kind == "verdict"),
            last_failed_at=now,
            last_failure=RuleFailure(task_id, step, kind),
        )


def _count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("a rule stats counter is a non-negative integer")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("a rule stats text is a string")
    return value


def _optional_string(value: Any) -> str | None:
    return None if value is None else _string(value)


def _failure_kind(value: Any) -> FailureKind:
    if value == "check":
        return "check"
    if value == "verdict":
        return "verdict"
    raise ValueError("a rule failure kind is check or verdict")


_CHECKED: dict[str, Outcome] = {
    "passed": "check passed",
    "failed": "check failed",
    "not_applicable": "not applicable",
}


def evaluations(
    item: PlanItem, record: PlanItemExecution, report: CheckReport | None
) -> tuple[RuleEvaluation, ...]:
    """What one settled completion of ``item`` decided about each of its rules.

    ``report`` holds the settlement's check results: the completion's own,
    or, for a verdict, the passing report the hold kept. A waived rule, or
    one whose check the operator waived, counts as waived; a rule's own or
    derived check reports its result; a verdict in the held completion judges
    its rule; and the rules the change set left out are not applicable. A
    judged rule with none of these was not evaluated: the checks rejected
    the completion before a verifier saw it. A check that was unavailable
    decided nothing: its rule counts by the verdict, if one was given.
    """
    results = (
        {
            result.id: result.status
            for result in report.results
            if result.status != "unavailable"
        }
        if report
        else {}
    )
    derived = {
        rule_id: check.id
        for check in record.resolved_checks
        for rule_id in check.covers
    }
    waived = {key for key, _ in record.checks_waived}
    held = record.held_completion
    found = []
    for rule in item.rules:
        check = rule.id if rule.has_command else derived.get(rule.id)
        verdict = held.verdict(rule.id) if held is not None else None
        outcome: Outcome
        if rule.id in waived or check in waived:
            outcome = "waived"
        elif check is not None and check in results:
            outcome = _CHECKED[results[check]]
        elif verdict is not None:
            outcome = "verdict pass" if verdict.verdict == "pass" else "verdict fail"
        elif rule.id in record.rules_not_applicable:
            outcome = "not applicable"
        else:
            continue
        found.append(RuleEvaluation(rule.id, rule.text_hash, outcome))
    return tuple(found)


class RuleStatsStore:
    """The checkout's ``.ww/rules/stats.json``: one entry per rule ID."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.path = storage.runtime_path / "rules" / "stats.json"

    def load(self) -> dict[str, RuleStats]:
        """Every rule's counters; a file that cannot be read counts as empty."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != STATS_SCHEMA:
                raise ValueError("unsupported rule stats schema")
            rules = data.get("rules")
            if not isinstance(rules, dict):
                raise ValueError("rules must be a mapping")
            return {
                _string(rule_id): RuleStats.from_dict(entry)
                for rule_id, entry in rules.items()
            }
        except (OSError, ValueError):
            return {}

    def record_outcomes(
        self, task_id: str, step: str, outcomes: Iterable[RuleEvaluation], now: str
    ) -> None:
        """Count one settled completion of ``step``; a failed write is dropped."""
        outcomes = tuple(outcomes)
        if not outcomes:
            return
        try:
            with self.storage.locks.lock(self.path, purpose="rule statistics"):
                stats = self.load()
                for evaluation in outcomes:
                    stats[evaluation.id] = stats.get(
                        evaluation.id, RuleStats()
                    ).counting(evaluation, task_id, step, now)
                self.storage.locks.atomic_write(
                    self.path,
                    json.dumps(
                        {
                            "schema": STATS_SCHEMA,
                            "rules": {
                                rule_id: entry.to_dict()
                                for rule_id, entry in sorted(stats.items())
                            },
                        },
                        indent=2,
                    )
                    + "\n",
                )
        except (OSError, LockError):
            return


def never_applied_rules(
    configuration: WorkflowConfiguration, stats: dict[str, RuleStats]
) -> tuple[RuleDefinition, ...]:
    """The declared rules evaluated at least ``NEVER_APPLIED_THRESHOLD`` times
    without ever applying: each time waived or left out by the change set."""
    found: dict[str, RuleDefinition] = {}
    for rule in every_rule(configuration):
        entry = stats.get(rule.id)
        if (
            entry is not None
            and rule.id not in found
            and entry.applied == 0
            and entry.waived + entry.not_applicable >= NEVER_APPLIED_THRESHOLD
        ):
            found[rule.id] = rule
    return tuple(found.values())
