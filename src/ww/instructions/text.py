# SPDX-License-Identifier: GPL-3.0-or-later
"""Instruction wording and continuation command construction."""

from __future__ import annotations

from ww.actions import InstructionContext, PlannedAction, actions
from ww.plan import PlanItem
from ww.variables import PROJECTS

from .commands import (
    TASK_PLACEHOLDER,
    add_child_command,
    add_item_command,
    item_command,
    update_item_command,
)

NO_SUBAGENTS = (
    "This step allows no subagents (`subagents: false`): whoever performs it "
    "does all of its work alone and spawns no subagent for anything, not for "
    "research, tests, or review."
)


def _stage(item: PlanItem | None) -> str | None:
    if item is None:
        return None
    if item.phase == "step":
        return "Nested workflow step" if item.parent else "Workflow step"
    if item.phase == "before_start_workflow":
        return "Before Workflow Start hook"
    if item.phase == "before_complete_workflow":
        return "Before Workflow Completion hook"
    return item.phase.replace("_", " ").title() + " hook"


def action_text(
    item: PlanItem,
    task_values: dict[str, str] | None = None,
    task_id: str | None = None,
) -> str:
    """Render the work text for an ordinary action, plus item or child guidance."""
    if not isinstance(item.operation, PlannedAction):
        raise ValueError(f"core operation {item.kind!r} has no ordinary action text")
    content = actions.get(item.operation.identifier).instruction(
        item.operation.payload,
        InstructionContext(item.description, item.name, task_values or {}),
    )
    task_reference = task_id or TASK_PLACEHOLDER
    if item.item_operation == "collect":
        result = (
            content.text
            + "\n\nSplit the source into complete, reportable items. Preserve every "
            "source comment; use a stable source ID when one exists, otherwise choose "
            f"a unique ID. Use `{add_item_command()}` for "
            "each item. Combine related work under one canonical item only when that "
            "avoids duplicate analysis or fixes; retain each related source item and "
            "link it with `--refers-to <canonical-id>` so it can still be "
            "reported."
        )
        if item.split_instruction:
            result += f"\n\nHow to split: {item.split_instruction}"
        return _with_artifact_dependency(result, item)
    if item.child_operation == "collect":
        projects = [
            name for name in (task_values or {}).get(PROJECTS, "").split(",") if name
        ]
        result = (
            content.text
            + "\n\nRecord each child task with:\n\n```console\n"
            + add_child_command(
                task_reference, project=bool(projects), identity=item.child_identity
            )
            + "\n```\n\nRun this command once for every independent child task."
        )
        if item.split_instruction:
            result += f"\n\nHow to split: {item.split_instruction}"
        if item.child_identity:
            result += (
                "\n\nDo not pass `--id`: each child receives its ID from the first "
                "step of its own workflow, which runs when the child starts."
            )
        if projects:
            result += (
                "\n\nConfigured projects: "
                + ", ".join(f"`{name}`" for name in projects)
                + ". Pass `--project` with the project a child works in; omit it "
                "for a child that works in the root."
            )
        return _with_artifact_dependency(result, item)
    text = content.text
    if content.include_item_context and item.item_id:
        text += (
            f"\n\nActive item: `{item.item_id}`. Inspect it with "
            f"`{item_command(task_reference, item.item_id)}`."
            " Save progress with:\n\n```console\n"
            + update_item_command(task_reference, item.item_id, item.item_operation)
            + "\n```\n\nThis command confirms the update and returns the worker "
            "completion command."
        )
    return _with_artifact_dependency(text, item)


def _with_artifact_dependency(text: str, item: PlanItem) -> str:
    if item.artifact_dependency is None:
        return text
    return (
        text
        + f"\n\nUse the artifact produced by the `{item.artifact_dependency}` step "
        "as input to this work."
    )
