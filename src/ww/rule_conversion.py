# SPDX-License-Identifier: GPL-3.0-or-later
"""Record scriptized checks outside a task: ``rules convert`` and ``decline``.

A rule without a command of its own is enforced by the rule-automation
store. These operations write the store directly, on the operator's
confirmation, so a check built once for the project (by
``ww-scriptize-rules``, or by hand) is recorded without a task's verifier.
``scriptize_state`` says, for each declared rule, where it stands, and
``scriptize_notice`` tells the operator about the rules no check covers yet.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Literal

from ww.builtin_workflows import missing_lane
from ww.changes import is_scoped
from ww.errors import StateError
from ww.rule_store import (
    CheckEntry,
    CheckSpec,
    RuleAutomation,
    RuleEntry,
    is_check_name,
    is_config_path,
)
from ww.workflow_config import RuleDefinition, WorkflowConfiguration, every_step

# The built-in workflow that turns rules into checks, outside any task.
SCRIPTIZE_WORKFLOW = "ww-scriptize-rules"

# Where a declared rule stands: checked by its own command, by a converted
# store check, declined or rejected (judged by a verifier), or not scriptized
# yet. The interim statuses of an old store, from when verifiers proposed
# checks inside tasks, count as not scriptized.
ScriptizeState = Literal[
    "command", "converted", "not_convertible", "rejected", "unscriptized"
]


def scriptize_state(automation: RuleAutomation, rule: RuleDefinition) -> ScriptizeState:
    if rule.check is not None:
        return "command"
    if automation.converted_check(rule.text_hash) is not None:
        return "converted"
    entry = automation.rules.get(rule.text_hash)
    if entry is not None and entry.status in {"not_convertible", "rejected"}:
        return entry.status
    return "unscriptized"


def unscriptized_rules(
    configuration: WorkflowConfiguration, automation: RuleAutomation
) -> tuple[RuleDefinition, ...]:
    """The declared rules not scriptized yet, each once, in declaration order."""
    found: dict[str, RuleDefinition] = {}
    for rule in every_rule(configuration):
        if rule.id not in found and scriptize_state(automation, rule) == "unscriptized":
            found[rule.id] = rule
    return tuple(found.values())


def unscoped_judged_rules(
    configuration: WorkflowConfiguration, automation: RuleAutomation
) -> tuple[RuleDefinition, ...]:
    """The declared rules a verifier judges after every step that changes
    anything: no command, no converted check, and no ``paths`` or strings to
    narrow them; each once, in declaration order.
    """
    found: dict[str, RuleDefinition] = {}
    for rule in every_rule(configuration):
        if (
            rule.id not in found
            and not is_scoped(rule)
            and scriptize_state(automation, rule) not in {"command", "converted"}
        ):
            found[rule.id] = rule
    return tuple(found.values())


def scriptize_notice(
    configuration: WorkflowConfiguration, automation: RuleAutomation
) -> str | None:
    """What ``discover`` and ``start`` say about rules without a check yet.

    ``None`` when there are none, or while ``ww-scriptize-rules`` is switched
    off. It never blocks anything: such rules are judged by verifiers.
    """
    workflow = configuration.workflows_by_name.get(SCRIPTIZE_WORKFLOW)
    if workflow is None:
        return None
    count = len(unscriptized_rules(configuration, automation))
    if not count:
        return None
    notice = (
        f"{count} declared rule{'s have' if count != 1 else ' has'} no check "
        "yet, so a verifier judges "
        f"{'them' if count != 1 else 'it'} in every step. The `ww-scriptize` "
        f"skill starts `{SCRIPTIZE_WORKFLOW}`, which builds "
        f"{'checks for them' if count != 1 else 'a check for it'} with the "
        "operator."
    )
    if missing_lane(workflow) is not None:
        notice += (
            " It first needs the lane it works in: `hooks_from` in its "
            "ww.yaml definition."
        )
    return notice


def declared_rules(
    configuration: WorkflowConfiguration, rule_ids: tuple[str, ...]
) -> tuple[RuleDefinition, ...]:
    """The declared rules with these IDs, in the order given.

    An unknown ID, a repeated one, and a rule with a command of its own,
    which the store never covers, are refused.
    """
    if not rule_ids:
        raise StateError("name at least one rule ID; `rules` lists them")
    if len(set(rule_ids)) != len(rule_ids):
        raise StateError("a rule ID is named twice")
    by_id = {rule.id: rule for rule in every_rule(configuration)}
    rules = []
    for rule_id in rule_ids:
        rule = by_id.get(rule_id)
        if rule is None:
            raise StateError(
                f"no rule {rule_id!r} is declared; `rules` lists every rule ID"
            )
        if rule.check is not None:
            raise StateError(
                f"rule `{rule_id}` has a command of its own; the store does not "
                "cover it"
            )
        rules.append(rule)
    return tuple(rules)


@dataclass(frozen=True)
class StoreChange:
    """The store after ``convert`` or ``decline``, and what else it changed.

    ``unscriptized`` are the text hashes of rules returned to unscriptized;
    ``moved`` pairs a rule hash with the other check that covered it before;
    ``dropped_checks`` are checks left covering nothing, removed with their
    pending revisions; ``dropped_revisions`` names the checks whose pending
    revision was dropped while the check stays.
    """

    automation: RuleAutomation
    unscriptized: tuple[str, ...] = ()
    moved: tuple[tuple[str, str], ...] = ()
    dropped_checks: tuple[str, ...] = ()
    dropped_revisions: tuple[str, ...] = ()


# Rule entries a check's own coverage stands for (``proposed`` only in an old
# store); any other entry naming the check, such as a rejection, is the
# operator's record and is kept when the check stops covering the rule.
_COVERED_STATUSES = frozenset({"converted", "proposed"})


def config_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """A check's configuration files, normalised, relative to its directory.

    An empty path, an absolute one, and one with a ``..`` part, which would
    reach outside the directory the step's checks run in, are refused.
    """
    normalised = []
    for path in paths:
        if not is_config_path(path.strip()):
            raise StateError(
                f"--config {path!r} must be a relative path inside the project, "
                "without `..`"
            )
        normalised.append(PurePosixPath(path.strip()).as_posix())
    return tuple(dict.fromkeys(normalised))


def convert(
    automation: RuleAutomation,
    name: str,
    spec: CheckSpec,
    rules: tuple[RuleDefinition, ...],
    now: str,
) -> StoreChange:
    """Record ``name`` as a converted check covering ``rules``.

    ``spec.covers`` is replaced by the rules' text hashes and ``spec.config``
    is normalised. A new name creates the check; an existing one has its
    command, configuration, proof and coverage replaced, and any pending
    revision dropped. Rules the check covered before and covers no more
    return to unscriptized: their store entries are removed, except a
    rejection or a decline, which stays. A rule another check covered moves
    to this one; a check left covering nothing is removed.
    """
    if not is_check_name(name):
        raise StateError(
            f"check name {name!r} must be kebab-case, at most 40 characters"
        )
    spec = replace(spec, config=config_paths(spec.config))
    hashes = tuple(dict.fromkeys(rule.text_hash for rule in rules))
    previous = automation.checks.get(name)
    before = (
        set(previous.spec.covers)
        | set(previous.pending.covers if previous.pending else ())
        if previous is not None
        else set()
    )
    unscriptized = tuple(
        key
        for key in sorted(before - set(hashes))
        if (entry := automation.rules.get(key)) is not None
        and entry.check == name
        and entry.status in _COVERED_STATUSES
    )
    moved = tuple(
        (key, check_name)
        for key in hashes
        for check_name, check in automation.checks.items()
        if check_name != name and key in _coverage(check)
    )
    automation = replace(
        automation,
        rules={k: v for k, v in automation.rules.items() if k not in unscriptized},
    )
    automation, dropped_checks, dropped_revisions = _uncover(
        automation, set(hashes), keep=name
    )
    if previous is not None and previous.pending is not None:
        dropped_revisions = (name, *dropped_revisions)
    automation = automation.with_check(
        name,
        CheckEntry(
            spec=replace(spec, covers=hashes),
            status="converted",
            proposed_at=previous.proposed_at if previous else now,
            approved_at=now,
            approved_by="operator",
        ),
    )
    for rule in rules:
        entry = automation.rules.get(rule.text_hash)
        automation = automation.with_rule(
            rule.text_hash,
            RuleEntry(
                rule.text,
                "converted",
                interpretation=entry.interpretation if entry else None,
                check=name,
                approved_by="operator",
            ),
        )
    return StoreChange(
        automation,
        unscriptized=unscriptized,
        moved=moved,
        dropped_checks=dropped_checks,
        dropped_revisions=dropped_revisions,
    )


def decline(
    automation: RuleAutomation, rules: tuple[RuleDefinition, ...], reason: str
) -> StoreChange:
    """Record ``rules`` as ``not_convertible``: judged, never proposed again."""
    reason = reason.strip()
    if not reason:
        raise StateError("rules decline needs --reason")
    hashes = {rule.text_hash for rule in rules}
    moved = tuple(
        (rule.text_hash, check_name)
        for rule in rules
        for check_name, check in automation.checks.items()
        if rule.text_hash in _coverage(check)
    )
    automation, dropped_checks, dropped_revisions = _uncover(automation, hashes)
    for rule in rules:
        entry = automation.rules.get(rule.text_hash)
        automation = automation.with_rule(
            rule.text_hash,
            RuleEntry(
                rule.text,
                "not_convertible",
                interpretation=entry.interpretation if entry else None,
                reason=reason,
                approved_by="operator",
            ),
        )
    return StoreChange(
        automation,
        moved=moved,
        dropped_checks=dropped_checks,
        dropped_revisions=dropped_revisions,
    )


def _coverage(check: CheckEntry) -> set[str]:
    """The rules a check covers, approved or in its pending revision."""
    return set(check.spec.covers) | set(check.pending.covers if check.pending else ())


def _uncover(
    automation: RuleAutomation, hashes: set[str], keep: str | None = None
) -> tuple[RuleAutomation, tuple[str, ...], tuple[str, ...]]:
    """Drop ``hashes`` from every check's coverage but ``keep``'s.

    A check it changes loses any pending revision (only an old store has
    one); a check, other than a rejected one, left covering nothing is
    removed. Returns the store, the checks removed and the kept checks whose
    revision was dropped.
    """
    dropped_checks: list[str] = []
    dropped_revisions: list[str] = []
    checks = dict(automation.checks)
    for check_name, check in automation.checks.items():
        if check_name == keep or not hashes & _coverage(check):
            continue
        spec = _without(check.spec, hashes)
        if not spec.covers and check.status != "rejected":
            del checks[check_name]
            dropped_checks.append(check_name)
            continue
        if check.pending is not None:
            dropped_revisions.append(check_name)
        checks[check_name] = replace(check, spec=spec, pending=None)
    return (
        replace(automation, checks=checks),
        tuple(dropped_checks),
        tuple(dropped_revisions),
    )


def _without(spec: CheckSpec, hashes: set[str]) -> CheckSpec:
    if not hashes & set(spec.covers):
        return spec
    return replace(spec, covers=tuple(key for key in spec.covers if key not in hashes))


def every_rule(configuration: WorkflowConfiguration) -> Iterator[RuleDefinition]:
    for group in configuration.rule_groups:
        yield from group.rules
    for step in every_step(configuration):
        for entry in step.rules:
            if isinstance(entry, RuleDefinition):
                yield entry
