# SPDX-License-Identifier: GPL-3.0-or-later
"""Markdown for the rule commands: ``check``, ``rule`` and ``rules``.

``ww check`` reads like the fix page a completion would produce, so a worker
sees the same failures in the same shape before it completes.
"""

from __future__ import annotations

from ww.instructions.commands import dispute_command
from ww.instructions.models import CheckPreview
from ww.output_adapters.markdown import (
    Lines,
    _append_section,
    _document,
    _fix_failures,
    _waivers,
)
from ww.rule_store import CheckEntry, RuleAutomation, describe_command
from ww.rule_views import ListedRule, Orphans, RulesListing, RuleView


def _ids(ids: tuple[str, ...]) -> str:
    return ", ".join(f"`{item}`" for item in ids)


def render_check_preview(preview: CheckPreview) -> str:
    lines: Lines = [f"# {preview.task_id} · check `{preview.step}`", ""]
    if not preview.checks and not preview.judged:
        lines.append(
            "This step has no checks to run and no rules for a verifier to judge."
        )
        return _document(lines)
    if preview.failures:
        lines.extend(
            [
                f"## {len(preview.failures)} of {preview.checks} checks failed",
                "",
                "Completing now would be rejected for these checks, on the files "
                "this step changed so far:",
            ]
        )
        _fix_failures(lines, preview.failures)
        lines.extend(
            [
                "",
                "Fix the causes, or, when a check that already rejected a "
                "completion is wrong for this change, dispute it:",
                "",
                "```console",
                dispute_command(preview.task_id, assignment="<your-assignment-token>"),
                "```",
            ]
        )
    elif preview.checks:
        lines.append(f"All checks pass ({preview.checks} run).")
    others = [
        (label, ids)
        for label, ids in (
            ("Passed", preview.passed),
            (
                "Not applicable (their globs match no changed file)",
                preview.not_applicable,
            ),
        )
        if ids
    ]
    if others:
        lines.append("")
        lines.extend(f"{label}: {_ids(ids)}." for label, ids in others)
    if preview.judged:
        lines.extend(
            [
                "",
                f"Judged at completion: {_ids(preview.judged)}. A verifier, not "
                "you, judges these rules after you complete.",
            ]
        )
    for reason, waived in _waivers(preview.waived):
        lines.extend(["", f"Waived by the operator ({_ids(tuple(waived))}): {reason}."])
    if preview.all_files:
        lines.extend(
            [
                "",
                "There is no change set (no git), so the checks saw every file "
                "of the project their globs select.",
            ]
        )
    lines.extend(
        [
            "",
            "Nothing was recorded: this does not count as an attempt, and the "
            "checks' output was not kept.",
        ]
    )
    return _document(lines)


def render_rule_view(view: RuleView) -> str:
    kind = {
        "rule": "Rule",
        "hook": "Hook check",
        "derived": "Derived check",
    }.get(view.kind, "Check")
    lines: Lines = [f"# {kind} `{view.id}`", "", view.text]
    facts: Lines = []
    if view.paths:
        facts.append(f"- Applies to: {', '.join(view.paths)}")
    if view.source:
        facts.append(f"- Rule file: {view.source}")
    elif view.kind == "rule":
        facts.append("- Written in the step's own `rules` list.")
    elif view.kind == "hook":
        facts.append("- A `before_complete` hook with `on_failure: fix`.")
    elif view.kind == "derived":
        facts.append(
            "- An approved check from the rule-automation store, covering "
            + _ids(view.covers)
            + "."
        )
    if view.steps:
        facts.append(f"- Steps of this task: {_ids(view.steps)}")
    if view.command:
        facts.append(f"- Command: `{view.command}`")
        if view.assertion:
            facts.append(f"- {view.assertion}")
        if view.max_fixes is not None:
            facts.append(f"- Fix rounds allowed: {view.max_fixes}")
    elif view.kind == "rule":
        facts.append("- No command: a verifier judges it when the step completes.")
    if view.store_status:
        store = f"- Rule-automation store: {view.store_status}"
        if view.store_check:
            store += f" (check `{view.store_check}`)"
        facts.append(store)
    elif view.kind == "rule" and not view.command:
        facts.append("- Rule-automation store: nothing yet")
    if view.interpretation:
        facts.append(f"- Interpretation: {view.interpretation}")
    if view.resolution:
        facts.append(f"- Enforced in the current step as: {view.resolution}")
    if view.text_hash:
        facts.append(f"- Wording hash: {view.text_hash[:12]}")
    lines.extend(["", *facts])
    return _document(lines)


def render_rules_listing(listing: RulesListing) -> str:
    lines: Lines = ["# Rules"]
    if not listing.groups and not listing.steps:
        lines.extend(["", "No rules are declared."])
        return _document(lines)
    for group in listing.groups:
        _append_section(lines, f"Group `{group.name}`")
        filters = [
            f"{label} {_ids(names.listed)}"
            for label, names in (("workflows", group.workflows), ("steps", group.steps))
            if not names.admits_all
        ]
        if group.workflows.admits_none or group.steps.admits_none:
            scope = "only where a step names it"
        elif filters:
            scope = "; ".join(filters)
        else:
            scope = "every step"
        lines.append(f"Applies to: {scope}.")
        if group.origin != "configuration":
            lines.append(f"Shipped by: {group.origin}.")
        if group.hints:
            lines.append(
                "Verifier hints: "
                + ", ".join(f"{key} {value}" for key, value in group.hints.items())
                + "."
            )
        _rule_lines(lines, group.rules)
    for step in listing.steps:
        _append_section(lines, f"Step `{step.step}`")
        if step.groups:
            lines.append(f"Names the groups {_ids(step.groups)}.")
        _rule_lines(lines, step.rules)
    return _document(lines)


def _rule_lines(lines: Lines, rules: tuple[ListedRule, ...]) -> None:
    if not rules:
        return
    lines.append("")
    for rule in rules:
        scope = f" — {', '.join(rule.paths)}" if rule.paths else ""
        check = (
            " (checked by its command)"
            if rule.has_check
            else f" (checked by store check `{rule.store_check}`)"
            if rule.store_check
            else ""
        )
        disputed = (
            f" (disputed {rule.disputes} time{'s' if rule.disputes != 1 else ''})"
            if rule.disputes
            else ""
        )
        lines.append(f"- `{rule.id}`{scope} — {rule.summary}{check}{disputed}")


def render_orphans(listed: Orphans, automation: RuleAutomation) -> str:
    """The store entries ``ww rules prune`` would delete."""
    if not listed:
        return "The rule-automation store has no orphan entries.\n"
    lines = ["Orphan entries in the rule-automation store:"]
    lines.extend(
        f"- rule {key[:12]} ({automation.rules[key].status}): "
        f"{automation.rules[key].text}"
        for key in listed.rules
    )
    lines.extend(
        f"- check {name} ({automation.checks[name].status})" for name in listed.checks
    )
    return "\n".join(lines) + "\n"


def render_revoke_preview(name: str, check: CheckEntry) -> str:
    """The check ``ww rules revoke`` would reject, as the operator reads it."""
    spec = check.pending or check.spec
    lines = [
        f"Check {name} ({check.status}): {describe_command(spec.command)}",
        f"- covers {len(spec.covers)} rule(s)",
    ]
    if check.approved_by is not None:
        lines.append(f"- approved by {check.approved_by}")
    if spec.config:
        lines.append("- config files: " + ", ".join(spec.config))
    return "\n".join(lines) + "\n"


def render_revoked(name: str, rules: tuple[str, ...], config: tuple[str, ...]) -> str:
    """What ``ww rules revoke`` changed, and what it left for the operator."""
    listed = " (" + ", ".join(key[:12] for key in rules) + ")" if rules else ""
    files = f" and the check's config files ({', '.join(config)})" if config else ""
    lines = [
        f"Revoked check {name}: it is rejected in the rule-automation store "
        "and no longer runs.",
        f"Rules rejected with it, judged by a verifier from now on: "
        f"{len(rules)}{listed}",
        f"Nothing else changed: the YAML and rule files{files} stay as they "
        "are, for the operator to keep or remove.",
    ]
    return "\n".join(lines) + "\n"
