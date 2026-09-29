# SPDX-License-Identifier: GPL-3.0-or-later
"""Rendering for durable agent-produced workflow step artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RuleStatus = Literal[
    "passed", "failed", "not applicable", "self-declared", "verified pass"
]


@dataclass(frozen=True)
class RuleOutcome:
    """One rule or check of a completed step, as its artifact reports it.

    ``detail`` names what decided a rule without a command of its own: the
    derived check that ran, or the verification item that gave the verdict.
    """

    id: str
    status: RuleStatus
    hook: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class RulesSummary:
    """The ``## Rules`` section of a step artifact.

    ``waived`` names the checks the step completed without, each with the
    operator's reason; ``fix_attempts`` counts the completions ww rejected
    before.
    """

    outcomes: tuple[RuleOutcome, ...]
    fix_attempts: int = 0
    waived: tuple[tuple[str, str], ...] = ()


def render_step_artifact(
    *,
    task_id: str,
    workflow: str,
    step: str,
    step_number: int,
    step_total: int,
    skill: str,
    result: str,
    rules: RulesSummary | None = None,
) -> str:
    """Wrap an agent result in the built-in, stable Markdown artifact format."""
    body = _normalize_result(result).rstrip()
    return (
        f"# {task_id} — {step}\n\n"
        "## Workflow context\n\n"
        f"- Workflow: {workflow}\n"
        f"- Step: {step_number} of {step_total}\n"
        f"- Skill: {skill}\n\n"
        "## Result\n\n"
        f"{body}\n" + (_rules_section(rules) if rules is not None else "")
    )


def _rules_section(rules: RulesSummary) -> str:
    lines = ["", "## Rules", ""]
    lines.extend(
        f"- `{outcome.id}`{' (hook)' if outcome.hook else ''}: {outcome.status}"
        + (f" ({outcome.detail})" if outcome.detail else "")
        for outcome in rules.outcomes
    )
    lines.append("")
    reasons: dict[str, list[str]] = {}
    for check_id, reason in rules.waived:
        reasons.setdefault(reason, []).append(check_id)
    for reason, waived in reasons.items():
        names = ", ".join(f"`{check_id}`" for check_id in waived)
        lines.append(f"Checks waived by the operator ({names}): {reason}")
    attempts = rules.fix_attempts
    lines.append(
        f"Completions rejected before this one: {attempts}."
        if attempts
        else "No completion was rejected."
    )
    return "\n".join(lines) + "\n"


def _normalize_result(result: str) -> str:
    """Restore line endings when Markdown arrived as one escaped CLI argument."""
    if "\n" in result or "\r" in result:
        return result
    return result.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
