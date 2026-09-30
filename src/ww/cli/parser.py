# SPDX-License-Identifier: GPL-3.0-or-later
"""Argument parser for the ww command line."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from ww import STAGE, __version__
from ww.contracts import CALLER_ROLES
from ww.errors import StateError
from ww.hooks import HOOK_AGENTS, HOOK_EVENTS
from ww.hooks.notices import RECENT_INTERRUPTION_DAYS
from ww.runtimes import RUNTIME_INSTRUCTIONS

HOOK_SETUP_ACTIONS = ("install", "uninstall", "show")
# Flags that were renamed, by command: argparse refuses the old one and the
# error names the new one, so an agent's old command fails with its fix.
RENAMED_FLAGS: dict[str, dict[str, str]] = {
    "init": {"--task-id-format": "--task-format"},
    "start": {
        "--init-artifact": "--requirements",
        "--branch-naming-strategy": "--branch-strategy",
    },
    "next": {"--force-reason": "--reason"},
    "complete": {"--summary-for-next-step": "--summary"},
    "loop": {"--summary-for-next-step": "--summary"},
    "interact": {
        "--operator": "--operator-said",
        "--agent": "--agent-said",
        "--end-interaction": "--end",
    },
    "add-item": {"--item": "--text", "--reference-to-id": "--refers-to"},
    "update-item": {"--item": "--text"},
    "add-child": {"--description": "--text"},
    "updates": {"--check": "--now"},
}
# Commands that were renamed; ``child start`` became ``start-child``.
RENAMED_COMMANDS = {"child": "start-child <parent> <child>"}


class _Parser(argparse.ArgumentParser):
    """An argument parser whose errors name the replacement of a renamed flag.

    Abbreviated flags are off, so an old flag that prefixes its new name
    (``--operator`` of ``--operator-said``) is refused rather than accepted.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)
        self._arguments: list[str] = []

    def parse_known_args(  # type: ignore[override]
        self, args: Sequence[str] | None = None, namespace: Any = None
    ) -> tuple[argparse.Namespace, list[str]]:
        if args is not None:
            self._arguments = list(args)
        return super().parse_known_args(args, namespace)

    def error(self, message: str) -> NoReturn:
        # A subcommand's parser is named "<prog> <command>"; unrecognized
        # arguments are reported by the top-level parser, which finds the
        # command among the arguments it was given.
        command = self.prog.split()[-1] if " " in self.prog else next(
            (
                argument
                for argument in self._arguments
                if argument in RENAMED_FLAGS or argument in RENAMED_COMMANDS
            ),
            "",
        )
        hints = [
            f"{old} was renamed to {new}"
            for old, new in RENAMED_FLAGS.get(command, {}).items()
            if any(
                argument == old or argument.startswith(f"{old}=")
                for argument in self._arguments
            )
        ]
        if "invalid choice: 'child'" in message:
            hints.append(f"child start was renamed to {RENAMED_COMMANDS['child']}")
        if hints:
            message = message + "\n" + "; ".join(hints)
        super().error(message)


def _shared(*add: str) -> _Parser:
    """Build a parent parser carrying the named shared options."""
    parent = _Parser(add_help=False)
    if "json" in add:
        parent.add_argument("--json", action="store_true", dest="json_output")
    if "role" in add:
        parent.add_argument(
            "--role",
            choices=CALLER_ROLES,
            default=None,
            help="Responsibility of the caller for this command.",
        )
        parent.add_argument(
            "--assignment",
            default=None,
            help="The open assignment's token, which every worker command of "
            "the auto runtime carries.",
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
    parser = _Parser(
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
        "--force",
        action="store_true",
        help=(
            "Ask every question again, ignoring the saved answers; only adds, "
            "never removes what is already set up."
        ),
    )
    init.add_argument(
        "--link-instructions",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Add @WW_AGENT_INSTRUCTIONS.md to agent instruction files.",
    )
    init.add_argument(
        "--task-format",
        dest="task_id_format",
        choices=("digit", "timestamp", "uuid"),
        default=None,
        help="Generated task IDs: TASK-{{digit}}, TASK-{{timestamp}} or TASK-{{uuid}}",
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
            "Install the ww, noww and ww-rule skills into every agent "
            "directory found in the project."
        ),
    )

    init.add_argument(
        "--hooks",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Install ww's hooks for every hook-capable agent set up in the "
            "project (Claude Code, Codex, Cursor, Antigravity)."
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
        "--requirements",
        required=True,
        help=(
            "What the user asked, restated and normalized for grammar and style. "
            "Saved immediately as the built-in init artifact."
        ),
    )
    start.add_argument("--mode", action="append", default=[], dest="modes")
    start.add_argument("-a", "--agent", required=True)
    start.add_argument(
        "--branch-strategy",
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
        "--reason",
        dest="force_reason",
        help="Required explanation for --force; retained with the skipped item.",
    )
    next_step.add_argument(
        "--reassign",
        action="store_true",
        help=(
            "Give the open assignment a new token and dispatch it again; the "
            "worker holding the old token can no longer act."
        ),
    )
    next_step.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Confirm --retry, --force or --approve without the y/N prompt. Only "
            "for carrying out a decision the operator stated."
        ),
    )
    next_step.add_argument(
        "--outcome", help="Selected outcome for the pending assess step."
    )
    next_step.add_argument(
        "--approve",
        action="append",
        default=[],
        metavar="HASH_OR_CHECK",
        help=(
            "At a rules_proposed stop: approve a rule's proposed approach (by "
            "rule hash) or a proposed check (by name). Repeatable."
        ),
    )
    next_step.add_argument(
        "--approach",
        action="append",
        default=[],
        nargs=2,
        metavar=("HASH", "TEXT"),
        help=(
            "At a rules_proposed stop: approve your own approach for a rule "
            "instead of the verifier's. Repeatable."
        ),
    )
    next_step.add_argument(
        "--pick",
        action="append",
        default=[],
        metavar="HASH=NUMBER",
        help=(
            "At a rules_proposed stop: choose the reading of an ambiguous rule "
            "by its number. Repeatable."
        ),
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
        "--summary",
        default=None,
        help=(
            "One or two sentences for whoever performs the next step; required "
            "for a step."
        ),
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
        help=(
            "A declared metadata value, as path=value with the path written as "
            "in saves (project_metadata.<path> for a project value)."
        ),
    )
    complete.add_argument(
        "--artifact",
        default=None,
        help="Full Markdown result produced by the step worker.",
    )
    complete.add_argument(
        "--summary",
        default=None,
        help=(
            "One or two sentences for whoever performs the next step; required "
            "for a step."
        ),
    )
    complete.add_argument(
        "--rule-result",
        action="append",
        default=[],
        metavar="JSON",
        help="A verification's result for one rule, as a JSON object. Repeatable.",
    )
    complete.add_argument(
        "--check-result",
        action="append",
        default=[],
        metavar="JSON",
        help="A check a verification prepared, as a JSON object. Repeatable.",
    )
    interact = subparsers.add_parser(
        "interact",
        parents=[json_and_role],
        help="Record one exchange of an interactive step, or end its interaction.",
    )
    interact.add_argument("task_id")
    interact.add_argument(
        "--operator-said", dest="operator", default=None, help="What the operator said."
    )
    interact.add_argument(
        "--agent-said", dest="agent", default=None, help="What you said or proposed."
    )
    interact.add_argument(
        "--choice",
        default=None,
        help="The option the operator picked: label or number.",
    )
    interact.add_argument(
        "--end",
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

    check = subparsers.add_parser(
        "check",
        parents=[json_output],
        help=(
            "Run the active step's checks now, without completing it; nothing "
            "is recorded."
        ),
    )
    check.add_argument("task_id")
    dispute = subparsers.add_parser(
        "dispute",
        parents=[json_and_role],
        help="Ask the operator to overrule a check that rejected the completion.",
    )
    dispute.add_argument("task_id")
    dispute.add_argument(
        "--rule",
        required=True,
        dest="check_id",
        metavar="ID",
        help="The rule or check ID the fix page names.",
    )
    dispute.add_argument(
        "--reason",
        required=True,
        help="Why the check is wrong for this change, with the evidence.",
    )
    rule = subparsers.add_parser(
        "rule",
        parents=[json_output],
        help="Show one rule or check of a task in full.",
    )
    rule.add_argument("task_id")
    rule.add_argument("rule_id", metavar="ID")
    _rules_parser(subparsers, json_output)

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
    add_item.add_argument("--text", required=True, help="The item's wording.")
    add_item.add_argument(
        "--refers-to", metavar="ID", help="The ID of the item this one refers to."
    )
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
        "--text",
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
    add_child.add_argument("--text", required=True, help="The child's text.")
    add_child.add_argument(
        "--project",
        default=None,
        metavar="NAME",
        help="Run the child in this configured project directory.",
    )
    add_child.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="A custom field of the child; repeat for several.",
    )
    update_child = subparsers.add_parser(
        "update-child", help="Change a child task's text, project, or fields."
    )
    update_child.add_argument("task_id")
    update_child.add_argument("child_id")
    update_child.add_argument("--text", help="The child's new text.")
    update_child.add_argument(
        "--project",
        default=None,
        metavar="NAME",
        help="Run the child in this configured project directory.",
    )
    update_child.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Set a custom field of the child; allowed at any time.",
    )
    start_child = subparsers.add_parser(
        "start-child", parents=[json_output], help="Start one child task."
    )
    start_child.add_argument("parent_task_id")
    start_child.add_argument("child_id")
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
        "--now",
        action="store_true",
        help="Look again now instead of waiting for the next scheduled check.",
    )
    hook = subparsers.add_parser(
        "hook",
        help=(
            "Answer an agent hook, or install, remove, or show ww's hooks for "
            "one agent."
        ),
    )
    hook.add_argument("hook_action", choices=(*HOOK_EVENTS, *HOOK_SETUP_ACTIONS))
    hook.add_argument("--agent", required=True, choices=tuple(HOOK_AGENTS))
    hook.add_argument(
        "--local",
        action="store_true",
        help=(
            "Use the agent's project file kept out of version control "
            "(Claude Code: .claude/settings.local.json)."
        ),
    )
    interrupted = subparsers.add_parser(
        "interrupted",
        parents=[json_output],
        help="List tasks whose last agent session stopped mid-step.",
    )
    interrupted.add_argument(
        "--since",
        type=int,
        default=RECENT_INTERRUPTION_DAYS,
        metavar="DAYS",
        help=(
            f"Only interruptions of the last DAYS days "
            f"(default {RECENT_INTERRUPTION_DAYS})."
        ),
    )
    interrupted.add_argument(
        "--all", action="store_true", help="Every interruption, however old."
    )
    return parser


def _rules_parser(
    subparsers: argparse._SubParsersAction[_Parser],
    json_output: _Parser,
) -> None:
    """``rules``: list, prune and revoke, and the validated writes the skill uses."""
    rules = subparsers.add_parser(
        "rules",
        parents=[json_output],
        help=(
            "List the rule groups with their filters and rules; its actions "
            "prune the store, revoke a check, and write rule files and groups."
        ),
    )
    actions = rules.add_subparsers(dest="rules_action", required=False)
    # ``--json`` after the action; SUPPRESS keeps one given before it.
    after = _Parser(add_help=False)
    after.add_argument(
        "--json", action="store_true", dest="json_output", default=argparse.SUPPRESS
    )
    prune = actions.add_parser(
        "prune",
        parents=[after],
        help="Delete orphan rule-automation store entries after asking.",
    )
    prune.add_argument(
        "--yes",
        action="store_true",
        help="Delete without the y/N prompt, on the operator's word.",
    )
    revoke = actions.add_parser(
        "revoke",
        parents=[after],
        help=(
            "Reject a converted or proposed store check and the rules it "
            "covers, after asking; they are judged from then on."
        ),
    )
    revoke.add_argument("check_name", metavar="CHECK")
    revoke.add_argument(
        "--reason", default=None, help="Why the check is revoked; recorded."
    )
    revoke.add_argument(
        "--yes",
        action="store_true",
        help="Revoke without the y/N prompt, on the operator's word.",
    )
    dry_run = _Parser(add_help=False)
    dry_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the write and report it, then put every file back.",
    )
    add = actions.add_parser(
        "add",
        parents=[dry_run],
        help=(
            "Add a rule file to a group (`add <group> --text`), or a new root "
            "group (`add --group <name> --dir <path>`)."
        ),
    )
    add.add_argument("group_name", nargs="?", metavar="GROUP")
    add.add_argument("--text", help="The rule: one imperative sentence, then its body.")
    add.add_argument("--paths", nargs="+", metavar="GLOB", default=None)
    command = add.add_mutually_exclusive_group()
    command.add_argument("--check-shell", metavar="SCRIPT", default=None)
    command.add_argument("--check-argv", nargs="+", metavar="ARG", default=None)
    add.add_argument(
        "--assert",
        dest="assertion",
        action="append",
        metavar="empty|equals:VALUE",
        help="A condition the check's output must meet; repeat for several.",
    )
    add.add_argument("--id", dest="stem", metavar="STEM", default=None)
    add.add_argument("--group", dest="new_group", metavar="NAME", default=None)
    add.add_argument("--dir", dest="directory", type=Path, default=None)
    _filter_options(add)
    edit = actions.add_parser(
        "edit",
        parents=[dry_run],
        help="Replace a rule file's body or globs; the rest is kept.",
    )
    edit.add_argument("rule_id", metavar="ID")
    edit.add_argument("--text", default=None)
    edit.add_argument("--paths", nargs="+", metavar="GLOB", default=None)
    move = actions.add_parser(
        "move",
        parents=[dry_run],
        help="Move a rule file into another group's directory.",
    )
    move.add_argument("rule_id", metavar="ID")
    move.add_argument("target_group", metavar="GROUP")
    filters = actions.add_parser(
        "filter",
        parents=[dry_run],
        help="Set the workflows and steps a group in ww-rules.yaml applies to.",
    )
    filters.add_argument("group_name", metavar="GROUP")
    _filter_options(filters)
    filters.add_argument(
        "--all-workflows",
        action="store_true",
        help="Remove the workflows filter: every workflow.",
    )
    filters.add_argument(
        "--all-steps", action="store_true", help="Remove the steps filter: every step."
    )
    promote = actions.add_parser(
        "promote",
        parents=[dry_run],
        help=(
            "Copy an approved store check into the `check` of every rule file "
            "it covers, and remove it from the store."
        ),
    )
    promote.add_argument("check_name", metavar="CHECK")


def _filter_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workflows",
        nargs="*",
        metavar="NAME",
        default=None,
        help=(
            "Only these workflows; '*' for all; with no name, none (only where "
            "a step names it)."
        ),
    )
    parser.add_argument(
        "--steps",
        nargs="*",
        metavar="NAME",
        default=None,
        help=(
            "Only these steps; '*' for all; with no name, none (only where a "
            "step names it)."
        ),
    )


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
