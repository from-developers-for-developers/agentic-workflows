# SPDX-License-Identifier: GPL-3.0-or-later
"""The "Rules converted in this run" report, built by ww from the store.

A run's verifiers may turn rules without a command into checks. What the
operator approved in the run is read from the rule-automation store entries
whose ``approved_in`` is the run. ww renders the report on
the completion page and appends it to the workflow summary artifact, so no
agent writes or summarises it.
"""

from __future__ import annotations

from ww.plan import WorkflowPlan
from ww.rule_store import RuleAutomation, describe_command

from .models import ConvertedCheck, CoveredRule, RuleConversions

HASH_LENGTH = 64
_WORDING_LIMIT = 80


def run_reference(task_id: str, run_id: str | None, workflow: str) -> str:
    """How the store names a run: ``<task>/<run>``."""
    return f"{task_id}/{run_id or workflow}"


def rule_conversions(
    automation: RuleAutomation, run: str, plan: WorkflowPlan
) -> RuleConversions:
    """The checks approved in ``run``."""
    declared = {rule.text_hash: rule for item in plan.items for rule in item.rules}

    def covered(hashes: tuple[str, ...]) -> tuple[CoveredRule, ...]:
        result = []
        for text_hash in hashes:
            rule = declared.get(text_hash)
            entry = automation.rules.get(text_hash)
            if rule is not None:
                result.append(CoveredRule(rule.id, _wording(rule.summary)))
            elif entry is not None:
                result.append(CoveredRule(_short(text_hash), _wording(entry.text)))
        return tuple(result)

    converted = tuple(
        ConvertedCheck(
            name=name,
            status=check.status,
            rules=covered(check.spec.covers),
            command=describe_command(check.spec.command),
            config=check.spec.config,
            proven=check.spec.proven,
            approved_by=check.approved_by,
        )
        for name, check in automation.checks.items()
        if check.approved_in == run
    )
    return RuleConversions(converted)


def conversions_markdown(conversions: RuleConversions, heading: str = "##") -> str:
    """The report as a Markdown section; empty when there is nothing to report."""
    if not conversions:
        return ""
    lines = [f"{heading} Rules converted in this run", ""]
    for check in conversions.converted:
        approver = check.approved_by or "unknown"
        state = "" if check.status == "converted" else f", now {check.status}"
        lines.append(f"- Check `{check.name}` (approved by {approver}{state})")
        lines.extend(f"  - Rule `{rule.id}`: {rule.wording}" for rule in check.rules)
        lines.append(f"  - Command: `{check.command}`")
        if check.config:
            lines.append(
                "  - Config files: " + ", ".join(f"`{path}`" for path in check.config)
            )
        lines.append(f"  - Proven: {'yes' if check.proven else 'no'}")
    return "\n".join(lines).rstrip() + "\n"


def _wording(text: str) -> str:
    first = text.strip().splitlines()[0].strip() if text.strip() else ""
    if len(first) <= _WORDING_LIMIT:
        return first
    return first[: _WORDING_LIMIT - 1] + "…"


def _short(text_hash: str) -> str:
    return text_hash[:12] if len(text_hash) == HASH_LENGTH else text_hash
