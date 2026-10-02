# SPDX-License-Identifier: GPL-3.0-or-later
"""Record scriptized checks outside a task: ``rules convert`` and ``decline``.

A rule without a command of its own is enforced by the rule-automation
store. These operations write the store directly, on the operator's
confirmation, so a check built once for the project (by
``ww-scriptize-rules``, or by hand) is recorded without a task's verifier.
``scriptize_state`` says, for each declared rule, where it stands.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Literal

from ww.errors import StateError
from ww.rule_store import (
    CheckEntry,
    CheckSpec,
    RuleAutomation,
    RuleEntry,
    is_check_name,
)
from ww.workflow_config import RuleDefinition, WorkflowConfiguration, every_step

# Where a declared rule stands: checked by its own command, by a converted
# store check, declined or rejected (judged by a verifier), or not scriptized
# yet. The in-task flow's interim statuses count as not scriptized.
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
    by_id = {rule.id: rule for rule in _every_rule(configuration)}
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


def convert(
    automation: RuleAutomation,
    name: str,
    spec: CheckSpec,
    rules: tuple[RuleDefinition, ...],
    now: str,
) -> tuple[RuleAutomation, tuple[str, ...]]:
    """Record ``name`` as a converted check covering ``rules``.

    ``spec.covers`` is replaced by the rules' text hashes. A new name creates
    the check; an existing one has its command, configuration, proof and
    coverage replaced, and any pending revision dropped. Rules the check
    covered before and covers no more return to unscriptized: their store
    entries are removed. A rule another check covered moves to this one.
    Returns the store and the text hashes of the rules returned to
    unscriptized.
    """
    if not is_check_name(name):
        raise StateError(
            f"check name {name!r} must be kebab-case, at most 40 characters"
        )
    hashes = tuple(dict.fromkeys(rule.text_hash for rule in rules))
    previous = automation.checks.get(name)
    before = (
        set(previous.spec.covers)
        | set(previous.pending.covers if previous.pending else ())
        if previous is not None
        else set()
    )
    dropped = tuple(
        key
        for key in sorted(before - set(hashes))
        if (entry := automation.rules.get(key)) is not None and entry.check == name
    )
    automation = replace(
        automation,
        rules={k: v for k, v in automation.rules.items() if k not in dropped},
    )
    automation = _uncover(automation, set(hashes), keep=name)
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
    return automation, dropped


def decline(
    automation: RuleAutomation, rules: tuple[RuleDefinition, ...], reason: str
) -> RuleAutomation:
    """Record ``rules`` as ``not_convertible``: judged, never proposed again."""
    reason = reason.strip()
    if not reason:
        raise StateError("rules decline needs --reason")
    hashes = {rule.text_hash for rule in rules}
    automation = _uncover(automation, hashes)
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
    return automation


def _uncover(
    automation: RuleAutomation, hashes: set[str], keep: str | None = None
) -> RuleAutomation:
    """Drop ``hashes`` from every check's coverage but ``keep``'s."""
    for check_name, check in automation.checks.items():
        if check_name == keep:
            continue
        spec = _without(check.spec, hashes)
        pending = _without(check.pending, hashes) if check.pending else None
        if spec is not check.spec or pending is not check.pending:
            automation = automation.with_check(
                check_name, replace(check, spec=spec, pending=pending)
            )
    return automation


def _without(spec: CheckSpec, hashes: set[str]) -> CheckSpec:
    if not hashes & set(spec.covers):
        return spec
    return replace(spec, covers=tuple(key for key in spec.covers if key not in hashes))


def _every_rule(configuration: WorkflowConfiguration) -> Iterator[RuleDefinition]:
    for group in configuration.rule_groups:
        yield from group.rules
    for step in every_step(configuration):
        for entry in step.rules:
            if isinstance(entry, RuleDefinition):
                yield entry
