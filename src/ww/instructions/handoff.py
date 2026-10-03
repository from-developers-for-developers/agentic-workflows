# SPDX-License-Identifier: GPL-3.0-or-later
"""The "Handoff to manager" block, built by ww when an assignment ends.

The facts of a delegated worker's assignment are all in the saved state, so ww
reports them to the manager itself rather than leaving it to the worker's text:
the items performed and how each ended, the artifacts, the files changed,
the checks run and waived, and the fix rounds. The worker's judgment reaches
the manager only through its short ``--summary``, and the
worker returns the block verbatim.
"""

from __future__ import annotations

from pathlib import Path

from ww.execution_models import PlanItemExecution
from ww.plan import PlanItem

from .commands import next_command
from .models import HandoffBlock, HandoffStep

HANDOFF_TITLE = "Handoff to manager"


def handoff_block(
    task_id: str,
    token: str,
    performed: tuple[tuple[PlanItem, PlanItemExecution], ...],
    *,
    root: Path,
    files: tuple[str, ...] | None,
    error: str | None,
    loop_outcome: tuple[str, str] | None = None,
) -> HandoffBlock:
    """Report the agent items of one ended assignment, in plan order.

    ``performed`` pairs each agent item of the assignment with its latest
    record; items the worker never reached are left out. ``error`` is the
    run's failure, such as a handler that failed after the last completion.
    ``loop_outcome`` names the item that broke or continued its loop and
    which it did.
    """
    steps = tuple(
        step
        for item, record in performed
        if (step := _step(item, record, root, loop_outcome)) is not None
    )
    summary = next(
        (
            record.summary_for_next
            for _, record in reversed(performed)
            if record.summary_for_next
        ),
        None,
    )
    if error is not None and any(step.error == error for step in steps):
        # A failed step already carries it.
        error = None
    return HandoffBlock(
        task_id=task_id,
        token=token,
        steps=steps,
        files=files,
        summary=summary,
        error=error,
    )


def _step(
    item: PlanItem,
    record: PlanItemExecution,
    root: Path,
    loop_outcome: tuple[str, str] | None,
) -> HandoffStep | None:
    if record.status == "completed" and not record.attempts:
        # Completed without being performed: skipped by a loop break.
        return None
    if record.status == "completed":
        outcome = "completed"
        if loop_outcome is not None and loop_outcome[0] == item.id:
            outcome = f"loop {loop_outcome[1]}"
    elif record.status == "failed":
        outcome = "failed"
    elif record.held_completion is not None:
        outcome = "held for verification"
    elif record.attempts:
        outcome = "not completed"
    else:
        return None
    last = record.check_reports[-1] if record.check_reports else None
    return HandoffStep(
        name=item.name,
        outcome=outcome,
        artifact=str((root / record.repair_artifacts[-1]).resolve())
        if record.repair_artifacts
        else str((root / record.artifact).resolve())
        if record.artifact
        else None,
        checks=(
            tuple((result.id, result.status) for result in last.results)
            if last is not None
            else ()
        ),
        checks_waived=tuple(check_id for check_id, _ in record.checks_waived),
        fix_rounds=record.repair_failures
        or sum(1 for report in record.check_reports if report.failed),
        error=record.error if record.status == "failed" else None,
    )


def handoff_markdown(block: HandoffBlock) -> str:
    """The block's text, as the worker returns it and the manager reads it."""
    lines = [f"{HANDOFF_TITLE} · assignment {block.token}", ""]
    lines.append("Steps:")
    if not block.steps:
        lines.append("- none completed")
    for step in block.steps:
        lines.append(f"- {step.name}: {step.outcome}")
        if step.artifact:
            lines.append(f"  artifact: {step.artifact}")
        if step.checks:
            lines.append(
                "  checks: "
                + ", ".join(f"{check} {status}" for check, status in step.checks)
            )
        if step.checks_waived:
            lines.append("  waived: " + ", ".join(step.checks_waived))
        if step.fix_rounds:
            lines.append(f"  fix rounds: {step.fix_rounds}")
        if step.error:
            lines.append(f"  error: {_one_line(step.error)}")
    lines.append("")
    if block.files is None:
        lines.append("Files changed: not tracked (no step here has rules or checks)")
    elif block.files:
        lines.extend(["Files changed:", *(f"- {path}" for path in block.files)])
    else:
        lines.append("Files changed: none")
    if block.error:
        lines.extend(["", f"Error: {_one_line(block.error)}"])
    if block.summary:
        lines.extend(["", f"Worker summary: {_one_line(block.summary)}"])
    manager = next_command(block.task_id)
    lines.extend(["", f"Manager: continue with `{manager}`"])
    return "\n".join(lines)


def _one_line(text: str) -> str:
    return " ".join(text.split())
