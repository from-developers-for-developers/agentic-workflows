# SPDX-License-Identifier: GPL-3.0-or-later
"""Compact collection guidance shared by every agent continuation."""

from __future__ import annotations

import shlex

from ww.contracts import CallerRole
from ww.executable import ww_command
from ww.execution_models import ExecutionState
from ww.item_collections import collection_failures, missing_fields, promised_fields
from ww.items import WorkItem
from ww.plan import PlanItem, WorkflowPlan


def collection_guidance(
    state: ExecutionState,
    plan: WorkflowPlan,
    step: PlanItem,
    items: tuple[WorkItem, ...],
    caller_role: CallerRole | None,
) -> str:
    context = str(step.item_context)
    task = shlex.quote(state.task_id)
    scope = f"{task} --context {shlex.quote(context)} --run {shlex.quote(state.run_id)}"
    role = caller_role or ("manager" if step.role == "manager" else "worker")
    mutation = scope + f" --role {role}"
    if role == "worker" and state.assignment_token:
        mutation += f" --assignment {shlex.quote(state.assignment_token)}"
    command = ww_command()
    owned = tuple(item for item in items if item.context == context)
    lines = [
        f"\n\nYou are working within items context `{context}` "
        f"({len(owned)} item{'' if len(owned) == 1 else 's'}).",
        "Choose how to group and perform the work.",
        f"List: `{command} items {scope}`.",
        f"Inspect: `{command} item {scope} --id <item-id>`.",
    ]
    if step.item_identity:
        lines.append(
            f"Every new item needs source identity field `{step.item_identity}`."
        )
    if step.item_unique:
        lines.append(
            "Unique values within this context: " + ", ".join(step.item_unique) + "."
        )
    if step.shared_items:
        lines.append(
            "Reconcile the persistent records against the source; "
            "this run has fresh outcomes."
        )
    if step.verifies is None:
        lines.extend(
            [
                f"Resolve: `{command} resolve-item {mutation} --id <item-id>`.",
                f"After reporting succeeds: `{command} report-item "
                f"{mutation} --id <item-id>`.",
                "Every item must be resolved and reported before "
                "this context completes. "
                "These commands only record transitions; they do not fix or "
                "send anything.",
            ]
        )
        if step.item_operation == "collect":
            lines.append(
                f"Register: `{command} add-item {mutation} --id <item-id> "
                '--text "<work>"'
                + (
                    f" --field {step.item_identity}=<value>"
                    if step.item_identity
                    else ""
                )
                + "`."
            )
    else:
        lines.append(
            "Verification is read-only. Do not change the collection's records."
        )
    required = promised_fields(plan, state, context, step)
    missing = missing_fields(owned, required)
    if step.item_operation == "complete_collection":
        missing = collection_failures(plan, state, context, items)
    if missing:
        lines.append("Pending bookkeeping: " + "; ".join(missing) + ".")
    if step.verifies is None:
        for field in required:
            lines.append(
                f"For every item, save `{field}` with `{command} update-item "
                f"{mutation} --id <item-id> --field {field}=<value>`."
            )
    references = tuple(
        dict.fromkeys(name for name in step.dependencies if name.startswith("ww.item."))
    )
    for name in references:
        field = name.removeprefix("ww.item.")
        lines.append(
            f"`{{{{{name}}}}}` is a field reference, not a scalar substitution. "
            f"Choose an ID and look it up with `{command} item {scope} "
            f"--id <item-id> --get {field}`."
        )
    return "\n".join(lines)
