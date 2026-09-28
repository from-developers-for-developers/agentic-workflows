# SPDX-License-Identifier: GPL-3.0-or-later
"""Argument parser for the ww command line."""

from __future__ import annotations

import argparse
from pathlib import Path

from ww import STAGE, __version__
from ww.contracts import CALLER_ROLES
from ww.errors import StateError
from ww.runtimes import RUNTIME_INSTRUCTIONS


def _shared(*add: str) -> argparse.ArgumentParser:
    """Build a parent parser carrying the named shared options."""
    parent = argparse.ArgumentParser(add_help=False)
    if "json" in add:
        parent.add_argument("--json", action="store_true", dest="json_output")
    if "role" in add:
        parent.add_argument(
            "--role",
            choices=CALLER_ROLES,
            default=None,
            help="Responsibility of the caller for this command.",
        )
    if "run" in add:
        parent.add_argument("--run", dest="run_id")
    if "selection" in add:
        parent.add_argument("--selected-agent", default=None)
        parent.add_argument("--selected-model", default=None)
        parent.add_argument("--selected-reasoning", default=None)
    return parent


def _true_false(value: str) -> bool:
    if value not in {"true", "false"}:
        raise argparse.ArgumentTypeError("choose 'true' or 'false'")
    return value == "true"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ww-agentic-workflows", description="Resumable agentic workflows."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ww-agentic-workflows {__version__} ({STAGE})",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root (default: detect the primary project checkout).",
    )
    json_output = _shared("json")
    json_and_role = _shared("json", "role")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser(
        "init", parents=[json_output], help="Initialize ww in the current project."
    )
    init.add_argument(
        "--no-input", action="store_true", help="Use defaults without prompting."
    )
    init.add_argument(
        "--link-instructions",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Add @WW_AGENT_INSTRUCTIONS.md to agent instruction files.",
    )
    init.add_argument(
        "--task-id-format", choices=("digit", "timestamp", "uuid"), default=None
    )
    init.add_argument(
        "--worktrees", action=argparse.BooleanOptionalAction, default=None
    )
    init.add_argument("--worktree-dir", default=None)
    init.add_argument(
        "--branch-format",
        action="append",
        default=[],
        metavar="WORKFLOW=FORMAT",
        help="Add a workflow-specific Git branch format; repeat as needed.",
    )
    init.add_argument(
        "--update-gitignore",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Add exactly .ww/ to .gitignore.",
    )
    init.add_argument(
        "--skills",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Install the ww and noww skills into every agent directory found "
            "in the project."
        ),
    )

    lookup = subparsers.add_parser(
        "lookup",
        parents=[json_output],
        help=(
            "Find the task a change outside every workflow belongs to, and "
            "what to do next."
        ),
    )
    lookup.add_argument(
        "reference",
        nargs="?",
        help="What the operator called the task, such as 12345 or FORMS-12345.",
    )
    lookup.add_argument("-a", "--agent", required=True)
    subparsers.add_parser(
        "discover",
        parents=[json_output],
        help="Show the workflows, modes, options, and commands for starting a task.",
    )
    for name, help_text in (
        ("modes", "List configured modes as JSON."),
        ("workflows", "List configured workflows as JSON."),
        ("runtimes", "List supported runtimes as JSON."),
        ("agents", "List supported agents as JSON."),
        ("projects", "List configured projects as JSON."),
        ("extensions", "List discovered extensions as JSON."),
    ):
        subparsers.add_parser(name, help=help_text)
    extension = subparsers.add_parser(
        "extension", help="Run a command an extension provides."
    )
    extension.add_argument("extension_id", metavar="VENDOR/NAME")
    extension.add_argument("extension_command", metavar="COMMAND")
    extension.add_argument(
        "--project",
        default=None,
        metavar="NAME",
        help="Use the extension settings a task in this configured project sees.",
    )
    extension.add_argument("extension_arguments", metavar="ARG", nargs="*", default=[])

    plan = subparsers.add_parser(
        "plan",
        parents=[json_output],
        help="Compile a workflow into its complete execution plan.",
    )
    plan.add_argument("-w", "--workflow", required=True)
    plan.add_argument("-a", "--agent", required=True)
    plan.add_argument("--task-id", default=None)
    plan.add_argument(
        "--project",
        default=None,
        metavar="NAME",
        help="Compile the plan as a task working in this configured project.",
    )
    subparsers.add_parser(
        "lint",
        help="Validate ww-agentic-workflows.yaml without compiling a "
        "workflow plan.",
    )

    start = subparsers.add_parser(
        "start", parents=[json_and_role], help="Start a task workflow."
    )
    start.add_argument("task_id", nargs="?")
    start.add_argument("-w", "--workflow", required=True)
    start.add_argument(
        "--fresh-items",
        action="store_true",
        dest="fresh_items",
        help=(
            "Forget the items the task shares across runs, so the collection "
            "step splits anew."
        ),
    )
    start.add_argument(
        "--init-artifact",
        required=True,
        help=(
            "Requirements from the user prompt, normalized for grammar and style. "
            "Saved immediately as the built-in init artifact."
        ),
    )
    start.add_argument("--mode", action="append", default=[], dest="modes")
    start.add_argument("-a", "--agent", required=True)
    start.add_argument(
        "--branch-strategy",
        "--branch-naming-strategy",
        dest="branch_naming_strategy",
        default=None,
        metavar="NAME",
        help=(
            "Select a configured Git branch naming strategy for this run "
            "(default: use the workflow/configured default)."
        ),
    )
    start.add_argument(
        "--project",
        default=None,
        metavar="NAME",
        help="Work in this configured project directory instead of the root.",
    )
    start.add_argument(
        "-r",
        "--runtime",
        dest="workflow_runtime",
        default=None,
        choices=tuple(RUNTIME_INSTRUCTIONS),
        help=(
            "Agent orchestration guidance persisted with the run "
            "(default: the project's configured runtime, else single)."
        ),
    )
    start.add_argument(
        "--model", default="auto", help="Model used by the managing session."
    )
    start.add_argument(
        "--reasoning", default="auto", help="Reasoning used by the managing session."
    )

    next_step = subparsers.add_parser(
        "next", parents=[json_and_role], help="Start the next pending step."
    )
    next_step.add_argument("task_id")
    next_step.add_argument(
        "--model", default="auto", help="Model used for this agent-owned item."
    )
    next_step.add_argument(
        "--selected-agent", default=None, help="Agent selected for this assignment."
    )
    next_step.add_argument(
        "--reasoning", default="auto", help="Reasoning used for this agent-owned item."
    )
    next_actions = next_step.add_mutually_exclusive_group()
    next_actions.add_argument(
        "--force",
        action="store_true",
        help="Skip a failed or interrupted item after operator confirmation.",
    )
    next_actions.add_argument(
        "--retry",
        action="store_true",
        help="Replay an interrupted automatic operation after operator confirmation.",
    )
    next_step.add_argument(
        "--force-reason",
        help="Required explanation for --force; retained with the skipped item.",
    )
    next_step.add_argument(
        "--outcome", help="Selected outcome for the pending assess step."
    )

    completion = _shared("json", "role", "selection")
    loop = subparsers.add_parser(
        "loop",
        parents=[completion],
        help="Control the enclosing loop from an authorized worker step.",
    )
    loop.add_argument("task_id")
    loop_control = loop.add_mutually_exclusive_group(required=True)
    loop_control.add_argument("--break", action="store_true", dest="break_loop")
    loop_control.add_argument("--continue", action="store_true", dest="continue_loop")
    loop.add_argument("--variable", action="append", default=[])
    loop.add_argument("--metadata", action="append", default=[])
    loop.add_argument(
        "--artifact",
        default=None,
        help="Full Markdown result produced by the break-enabled step worker.",
    )
    loop.add_argument(
        "--summary-for-next-step",
        default=None,
        help="One or two sentences the next step reads; required for a step.",
    )
    complete = subparsers.add_parser(
        "complete", parents=[completion], help="Complete the current workflow step."
    )
    complete.add_argument("task_id")
    complete.add_argument("--variable", action="append", default=[])
    complete.add_argument(
        "--metadata",
        action="append",
        default=[],
        help="A declared task or project metadata value, as name=value.",
    )
    complete.add_argument(
        "--artifact",
        default=None,
        help="Full Markdown result produced by the step worker.",
    )
    complete.add_argument(
        "--summary-for-next-step",
        default=None,
        help="One or two sentences the next step reads; required for a step.",
    )
    interact = subparsers.add_parser(
        "interact",
        parents=[json_and_role],
        help="Record one exchange of an interactive step, or end its interaction.",
    )
    interact.add_argument("task_id")
    interact.add_argument("--operator", default=None, help="What the operator said.")
    interact.add_argument("--agent", default=None, help="What you said or proposed.")
    interact.add_argument(
        "--choice",
        default=None,
        help="The option the operator picked: label or number.",
    )
    interact.add_argument(
        "--end-interaction",
        action="store_true",
        dest="end_interaction",
        help="The operator said the conversation is finished.",
    )
    interact.add_argument(
        "--pause",
        action="store_true",
        help="The operator said they are done for now; the conversation stays open.",
    )
    interact.add_argument(
        "--await",
        action="store_true",
        dest="await_operator",
        help=(
            "Serve the operator page for the current item stage, wait until the "
            "operator has answered or left, then apply the answers."
        ),
    )
    interactions = subparsers.add_parser(
        "interactions", help="Print the task's whole record of operator interactions."
    )
    interactions.add_argument("task_id")
    fail = subparsers.add_parser(
        "fail",
        parents=[json_and_role],
        help="Mark the current agent-owned workflow step as failed.",
    )
    fail.add_argument("task_id")
    fail.add_argument(
        "--error",
        required=True,
        help="Functional error that prevented the agent-owned work from completing.",
    )

    with_run = _shared("json", "role", "run")
    status = subparsers.add_parser(
        "status", parents=[with_run], help="Show a compact current-task summary."
    )
    status.add_argument("task_id")
    instruction = subparsers.add_parser(
        "instruction",
        parents=[with_run],
        help="Show the current detailed workflow instruction.",
    )
    instruction.add_argument("task_id")
    metadata = subparsers.add_parser(
        "metadata", help="Show task or project metadata as JSON."
    )
    metadata.add_argument("task_id", nargs="?")
    metadata.add_argument(
        "--project",
        action="store_true",
        help="Show metadata shared by every task in this project.",
    )

    documents = subparsers.add_parser(
        "documents", help="List the declared documents, their files and last updates."
    )
    documents.add_argument("task_id", nargs="?")

    run_only = _shared("run")
    items = subparsers.add_parser(
        "items", parents=[run_only], help="List the latest workflow run's items."
    )
    items.add_argument("task_id")
    item = subparsers.add_parser(
        "item", parents=[run_only], help="Show one workflow item as JSON."
    )
    item.add_argument("task_id")
    item.add_argument("--id", dest="item_id")
    item.add_argument(
        "--by",
        metavar="NAME=VALUE",
        help="Find the item whose custom field has this value.",
    )
    artifacts = subparsers.add_parser(
        "artifacts",
        parents=[run_only],
        help="List one workflow run's artifacts as JSON.",
    )
    artifacts.add_argument("task_id")
    add_item = subparsers.add_parser(
        "add-item", help="Add an item to the active workflow run."
    )
    add_item.add_argument("task_id")
    add_item.add_argument("--id", required=True)
    add_item.add_argument("--item", required=True)
    add_item.add_argument("--reference-to-id")
    add_item.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="A custom field of the item; repeat for several.",
    )
    update_item = subparsers.add_parser(
        "update-item",
        parents=[json_and_role],
        help="Update an active workflow item and show its completion command.",
    )
    update_item.add_argument("task_id")
    update_item.add_argument("--id", dest="item_id", required=True)
    update_item.add_argument(
        "--item",
        help="New item text; allowed only while the collection step is in progress.",
    )
    update_item.add_argument("--processed-item")
    update_item.add_argument("--proposed-solution")
    update_item.add_argument("--actual-solution")
    update_item.add_argument("--resolved", type=_true_false, metavar="{true,false}")
    update_item.add_argument("--reported", type=_true_false, metavar="{true,false}")
    update_item.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Set a custom field of the item; repeat for several in one call.",
    )
    remove_item = subparsers.add_parser(
        "remove-item",
        parents=[json_and_role],
        help="Remove an item while the collection step is in progress.",
    )
    remove_item.add_argument("task_id")
    remove_item.add_argument("--id", dest="item_id", required=True)
    add_child = subparsers.add_parser("add-child", help="Add a child task.")
    add_child.add_argument("task_id")
    add_child.add_argument(
        "--id",
        help="Optional child ID; defaults to the configured task ID format.",
    )
    add_child.add_argument("--description", required=True)
    add_child.add_argument(
        "--project",
        default=None,
        metavar="NAME",
        help="Run the child in this configured project directory.",
    )
    child = subparsers.add_parser("child", help="Operate a child task.")
    child_subparsers = child.add_subparsers(dest="child_command", required=True)
    child_start = child_subparsers.add_parser(
        "start", parents=[json_output], help="Start one child task."
    )
    child_start.add_argument("parent_task_id")
    child_start.add_argument("child_id")
    reset = subparsers.add_parser(
        "reset", parents=[json_output], help="Delete a task's state and artifacts."
    )
    reset.add_argument("task_id")
    reset.add_argument(
        "--yes",
        action="store_true",
        help="Confirm deletion of the task workspace.",
    )
    subparsers.add_parser(
        "cleanup", parents=[json_output], help="Remove inactive ww lock sidecar files."
    )
    updates = subparsers.add_parser(
        "updates",
        parents=[json_output],
        help="Show whether the ww checkout is behind its remote.",
    )
    updates.add_argument(
        "--check",
        action="store_true",
        help="Look again now instead of waiting for the next scheduled check.",
    )
    return parser


def _variables(values: list[str]) -> tuple[tuple[str, str], ...]:
    return _named_values(values, "--variable")


def _metadata_values(values: list[str]) -> tuple[tuple[str, str], ...]:
    return _named_values(values, "--metadata")


def _named_values(values: list[str], option: str) -> tuple[tuple[str, str], ...]:
    parsed = []
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or not name or not item:
            raise StateError(f"{option} must use name=value")
        parsed.append((name, item))
    return tuple(parsed)
