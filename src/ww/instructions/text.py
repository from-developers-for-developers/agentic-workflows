# SPDX-License-Identifier: GPL-3.0-or-later
"""Instruction wording and continuation command construction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ww.actions import InstructionContext, PlannedAction, actions
from ww.plan import PlanItem
from ww.variables import PROJECTS

from .commands import (
    TASK_PLACEHOLDER,
    add_child_command,
)

NO_SUBAGENTS = (
    "This step allows no subagents (`subagents: false`): whoever performs it "
    "does all of its work alone and spawns no subagent for anything, not for "
    "research, tests, or review."
)

READONLY = (
    "This run is readonly (`readonly: true`): read files and run commands, "
    "such as tests, but create, change, delete, or commit nothing, and do "
    "not switch branches. ww refuses a completion that changed a file git "
    "does not ignore; findings go into the artifact, not into the code."
)


@dataclass(frozen=True)
class ContainerArtifact:
    """The artifact ``artifact_from`` naming a group or an assessment found.

    ``step`` and ``artifact`` (an absolute path) name the latest step inside
    the container that saved one in its current round, or the assessment
    itself when its outcome saved none; both are ``None`` when none has.
    """

    step: str | None = None
    artifact: str | None = None


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
    container_artifacts: Mapping[str, ContainerArtifact] | None = None,
) -> str:
    """Render the work text for an ordinary action, plus item or child guidance.

    ``container_artifacts`` holds what each ``artifact_from`` path naming a
    group or an assessment resolved to; a path naming a single step is absent.
    """
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
            + "\n\nFollow the splitting prompt and register each source obligation. "
            "Related fixes may be shared, but each source item keeps its own "
            "resolution and report."
        )
        if item.split_instruction:
            result += f"\n\nHow to split: {item.split_instruction}"
        return _with_artifact_dependencies(result, item, container_artifacts)
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
        return _with_artifact_dependencies(result, item, container_artifacts)
    text = content.text
    return _with_artifact_dependencies(text, item, container_artifacts)


def _with_artifact_dependencies(
    text: str, item: PlanItem, containers: Mapping[str, ContainerArtifact] | None
) -> str:
    """``text`` and one paragraph per ``artifact_from`` step, in their order."""
    return "\n\n".join(
        (
            text,
            *(
                _artifact_dependency(path, (containers or {}).get(path))
                for path in item.artifact_dependencies
            ),
        )
    )


def _artifact_dependency(path: str, container: ContainerArtifact | None) -> str:
    if container is not None and container.artifact is None:
        return (
            f"No artifact is available from `{path}`: no step inside it that "
            "saves one completed in this run. Continue without it."
        )
    if container is not None and container.step == path:
        return (
            f"Use the artifact produced by the `{container.step}` step as input "
            f"to this work: `{container.artifact}`."
        )
    if container is not None:
        return (
            f"Use the artifact produced by the `{container.step}` step, the latest "
            f"saved inside `{path}`, as input to this work: `{container.artifact}`."
        )
    return f"Use the artifact produced by the `{path}` step as input to this work."
