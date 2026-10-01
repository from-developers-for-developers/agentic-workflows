# SPDX-License-Identifier: GPL-3.0-or-later
"""Presentation helpers for command-line workflow results."""

from __future__ import annotations

import json
import shutil
import textwrap

from ww import BETA_NOTICE
from ww.actions import InstructionContent, InstructionContext, PlannedAction, actions
from ww.instructions import Instruction
from ww.operations import ChildWorkflowRun, LoopBoundary, WorkflowHandoff
from ww.output_adapters import (
    JsonOutputAdapter,
    MarkdownOutputAdapter,
    OutputAdapter,
)
from ww.output_adapters.markdown import requested_setting_lines
from ww.plan import PlanItem, WorkflowPlan
from ww.results import InitializationResult, ItemUpdateResult, ResetResult, TaskStatus

_MARKDOWN = MarkdownOutputAdapter()
_JSON = JsonOutputAdapter()


def _adapter_for(json_output: bool) -> OutputAdapter:
    """Choose the built-in output adapter for a CLI invocation."""
    return _JSON if json_output else _MARKDOWN


def render(instruction: Instruction, json_output: bool) -> str:
    """Render an instruction using the selected adapter."""
    return _adapter_for(json_output).render_instruction(instruction)


def render_status(status: TaskStatus, json_output: bool) -> str:
    """Render the small status projection for people or machines."""
    if json_output:
        return json.dumps(status.to_dict(), indent=2)
    values = status.to_dict()
    return (
        "\n".join(f"{key.replace('_', ' ')}: {value}" for key, value in values.items())
        + "\n"
    )


def render_reset(result: ResetResult, json_output: bool) -> str:
    """Render a reset outcome using the selected adapter."""
    return _adapter_for(json_output).render_reset(result)


def render_initialization(result: InitializationResult, json_output: bool) -> str:
    """Render an initialization outcome using the selected adapter."""
    return _adapter_for(json_output).render_initialization(result)


# The wordmark is 101 columns wide. Below that a terminal wraps every row of
# it into unreadable halves, so a narrow one gets a compact mark instead.
WORDMARK_COLUMNS = 101
COMPACT_WELCOME = (
    "  ██  ██   ██  ██\n  ██  ██   ██  ██   ww · agentic workflows\n  ░█████░ ░█████░\n"
)


def render_initialization_welcome(json_output: bool, columns: int | None = None) -> str:
    """Render the interactive init welcome before collecting project choices."""
    if json_output:
        return ""
    width = columns if columns is not None else shutil.get_terminal_size((80, 24))[0]
    if width < WORDMARK_COLUMNS:
        return COMPACT_WELCOME + "\n" + _beta_notice(width)
    return _WORDMARK + _beta_notice(width)


def _beta_notice(width: int) -> str:
    """The beta notice under the welcome, wrapped to the terminal."""
    lines = textwrap.wrap(
        BETA_NOTICE,
        max(40, width - 2),
        break_long_words=False,
        break_on_hyphens=False,
    )
    return "\n".join(lines) + "\n\n"


_WORDMARK = (
    "█░░░█ █░░░█\n"
    "█░░░█ █░░░█\n"
    "█░░░█ █░░░█\n"
    "█░█░█ █░█░█\n"
    "█░█░█ █░█░█\n"
    "█░█░█ █░█░█\n"
    "░█░█░ ░█░█░\n\n"
    "░███░ ░████ █████ █░░░█ █████ █████ ░████ ░░░░░ "
    "█░░░█ ░███░ ████░ █░░░█ █████ █░░░░ ░███░ █░░░█ ░████\n"
    "█░░░█ █░░░░ █░░░░ ██░░█ ░░█░░ ░░█░░ █░░░░ ░░░░░ "
    "█░░░█ █░░░█ █░░░█ █░░█░ █░░░░ █░░░░ █░░░█ █░░░█ █░░░░\n"
    "█░░░█ █░░░░ █░░░░ ██░░█ ░░█░░ ░░█░░ █░░░░ ░░░░░ "
    "█░░░█ █░░░█ █░░░█ █░█░░ █░░░░ █░░░░ █░░░█ █░░░█ █░░░░\n"
    "█████ █░███ ████░ █░█░█ ░░█░░ ░░█░░ █░░░░ ░░░░░ "
    "█░█░█ █░░░█ ████░ ██░░░ ████░ █░░░░ █░░░█ █░█░█ ░███░\n"
    "█░░░█ █░░░█ █░░░░ █░░██ ░░█░░ ░░█░░ █░░░░ ░░░░░ "
    "█░█░█ █░░░█ █░█░░ █░█░░ █░░░░ █░░░░ █░░░█ █░█░█ ░░░░█\n"
    "█░░░█ █░░░█ █░░░░ █░░██ ░░█░░ ░░█░░ █░░░░ ░░░░░ "
    "█░█░█ █░░░█ █░░█░ █░░█░ █░░░░ █░░░░ █░░░█ █░█░█ ░░░░█\n"
    "█░░░█ ░████ █████ █░░░█ ░░█░░ █████ ░████ ░░░░░ "
    "░█░█░ ░███░ █░░░█ █░░░█ █░░░░ █████ ░███░ ░█░█░ ████░\n\n"
)


def render_item_update(result: ItemUpdateResult, json_output: bool) -> str:
    """Render an item update acknowledgement and its immediate next action."""
    if json_output:
        return json.dumps(
            {
                "item": result.item.to_dict(),
                "continuation_command": result.continuation_command,
            },
            indent=2,
        )
    lines = [f"> Item `{result.item.id}` updated successfully."]
    if result.continuation_command:
        lines.extend(
            [
                "",
                "### Continue with completion",
                "",
                "Run:",
                "",
                "```console",
                result.continuation_command,
                "```",
            ]
        )
    return "\n".join(lines) + "\n"


def render_plan(plan: WorkflowPlan, json_output: bool) -> str:
    """Render a compiled plan for either people or programmatic consumers."""
    if json_output:
        return json.dumps(plan.to_dict(), indent=2) + "\n"
    lines = [f"# Workflow plan — `{plan.workflow}`", ""]
    if plan.workflow_description:
        lines.extend([f"> {plan.workflow_description}", ""])
    context = [f"**Agent:** `{plan.agent}`"]
    if plan.task_id:
        context.append(f"**Task ID:** `{plan.task_id}`")
    if plan.modes:
        context.append("**Modes:** " + ", ".join(f"`{mode}`" for mode in plan.modes))
    lines.extend([" · ".join(context), ""])
    if plan.handoff:
        lines.extend(
            [
                "> **Handoff workflow:** its final workflow transition starts the "
                "next workflow and does not return here.",
                "",
            ]
        )
    for item in plan.items:
        lines.extend([f"## {item.position}. {item.name}", ""])
        lines.extend(
            [
                f"**Stage:** {_stage_label(item.phase)}  ",
                f"**Workflow step:** `{item.step}`  ",
                f"**Owner:** `{item.owner}`  ",
                f"**Execution:** `{item.execution}`",
                "",
            ]
        )
        if item.parent:
            lines.extend([f"**Parent step:** `{item.parent}`", ""])
        lines.extend(_item_flow_lines(item))
        lines.extend(
            [
                "**Requested execution settings**",
                "",
                f"- Agent: `{item.requested_agent or plan.agent}`",
                *requested_setting_lines(
                    item.requested_model, item.requested_reasoning
                ),
                "",
            ]
        )
        if item.owner == "ww" and not item.requires_agent_input:
            lines.extend(
                [
                    "> Execution settings are retained for inspection and ignored "
                    "for this ww-owned operation.",
                    "",
                ]
            )
        if isinstance(item.operation, PlannedAction):
            implementation = (
                actions.get(item.operation.identifier)
                if actions.contains(item.operation.identifier)
                else None
            )
            content = (
                implementation.instruction(
                    item.operation.payload,
                    InstructionContext(item.description, item.name, {}),
                )
                if implementation is not None
                else InstructionContent(
                    item.description or item.name,
                    (
                        f"**Unavailable action:** `{item.operation.identifier}`",
                        "",
                        "**Saved payload**",
                        "",
                        *(
                            "    " + line
                            for line in json.dumps(
                                item.operation.payload, indent=2, sort_keys=True
                            ).splitlines()
                        ),
                        "",
                    ),
                )
            )
        else:
            markdown: tuple[str, ...]
            if isinstance(item.operation, LoopBoundary):
                markdown = (
                    "**Loop control**",
                    "",
                    f"- Boundary: `{item.operation.boundary}`",
                    f"- Maximum rounds: `{item.operation.max_times}`",
                    "",
                )
            elif isinstance(item.operation, WorkflowHandoff):
                markdown = ("**Start workflow**", "", f"`{item.operation.target}`", "")
            elif isinstance(item.operation, ChildWorkflowRun):
                markdown = (
                    "**Child workflow**",
                    "",
                    f"`{item.operation.workflow}`",
                    "",
                )
            else:  # pragma: no cover - PlanItem validates the union
                markdown = ()
            content = InstructionContent(item.description or item.name, markdown)
        if item.description and content.show_context:
            lines.extend(["**Context**", "", item.description, ""])
        lines.extend(content.markdown or ("**Prompt**", "", "", ""))
        if item.provide:
            heading = (
                "**Required input from the agent**"
                if item.requires_agent_input
                else "**Required input**"
            )
            lines.extend([heading, ""])
            lines.extend(
                f"- `{value.name}`"
                + (f" — {value.description}" if value.description else "")
                for value in item.provide
            )
            lines.append("")
            if item.requires_agent_input:
                lines.extend(
                    [
                        "> ww runs this command automatically after these values are "
                        "supplied through the completion flow.",
                        "",
                    ]
                )
        if item.save_metadata:
            scopes = {value.scope for value in item.save_metadata}
            scope_label = (
                f"{next(iter(scopes)).title()} metadata"
                if len(scopes) == 1
                else "Task and project metadata"
            )
            lines.extend([f"**{scope_label} to preserve**", ""])
            lines.extend(
                f"- `{'project_metadata' if value.scope == 'project' else 'metadata'}"
                f".{value.key}`"
                + (f" — {value.description}" if value.description else "")
                for value in item.save_metadata
            )
            lines.append("")
        lines.extend(content.after_shared)
    return "\n".join(lines) + "\n"


def _stage_label(phase: str) -> str:
    return {
        "before_start_workflow": "Before the workflow starts",
        "before_start": "Before this step starts",
        "step": "Perform this workflow step",
        "before_complete": "Before this step is completed",
        "after_complete": "After this step is completed",
        "before_complete_workflow": "Before the workflow is completed",
    }[phase]


def _item_flow_lines(item: PlanItem) -> list[str]:
    """Describe an item's role in an ``items`` step, if it has one."""
    if item.item_operation == "collect":
        lines = ["**Item collection:** splits the work into items"]
        if item.split_instruction:
            lines[0] += "  "
            lines.append(f"**Splitting guidance:** {item.split_instruction}")
        return [*lines, ""]
    if item.child_operation == "collect" and item.phase == "step":
        lines = ["**Child collection:** splits the work into child tasks"]
        if item.split_instruction:
            lines[0] += "  "
            lines.append(f"**Splitting guidance:** {item.split_instruction}")
        return [*lines, ""]
    if item.item_template and item.child_stage is not None:
        return ["**Per-child stage:** repeats for every collected child, in turn", ""]
    if item.item_template:
        return [
            "**Per-item stage:** repeats for every collected item  ",
            f"**Item assignment:** `{item.item_assignment}`",
            "",
        ]
    return []
