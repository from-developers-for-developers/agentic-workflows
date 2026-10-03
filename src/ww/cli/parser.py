# SPDX-License-Identifier: GPL-3.0-or-later
"""Argument parser for the ww command line."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ww import STAGE, __version__
from ww.contracts import CALLER_ROLES
from ww.errors import StateError
from ww.hooks import HOOK_AGENTS, HOOK_EVENTS
from ww.inspect import DEFAULT_COMMITS
from ww.runtimes import RUNTIME_INSTRUCTIONS

HOOK_SETUP_ACTIONS = ("install", "uninstall", "show")


CHECK_ARGV = "--check-argv"


class _Parser(argparse.ArgumentParser):
    """An argument parser that accepts only complete flag names.

    ``--check-argv -- <arg>...`` takes every argument after ``--`` as the
    check's argv, options of the checked tool included (``ruff check
    --select E``), so it goes last on the command line.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def parse_known_args(  # type: ignore[override]
        self,
        args: Sequence[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> tuple[argparse.Namespace, list[str]]:
        arguments = list(sys.argv[1:] if args is None else args)
        split = _check_argv_after_separator(arguments)
        if split is None:
            return super().parse_known_args(arguments, namespace)
        head, argv = split
        # A stand-in keeps argparse's own checks (a required command, the
        # mutually exclusive --check-shell); the real argv replaces it.
        parsed, extras = super().parse_known_args([*head, CHECK_ARGV, "-"], namespace)
        parsed.check_argv = argv
        return parsed, extras


def _check_argv_after_separator(
    arguments: list[str],
) -> tuple[list[str], list[str]] | None:
    """The arguments before ``--check-argv --`` and the argv after it."""
    for index, argument in enumerate(arguments[:-2]):
        if argument == CHECK_ARGV and arguments[index + 1] == "--":
            return arguments[:index], arguments[index + 2 :]
    return None


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


def _positive(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError("choose a whole number of at least 1")
    return int(value)


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
        help="Keep .ww out of .gitignore but for the shared learning files.",
    )
    init.add_argument(
        "--skills",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Install the bundled ww skills (ww, noww, ww-rule, ww-setup and "
            "the skills it guides through) into every agent directory found "
            "in the project."
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
        help="What the operator called the task, such as 12345 or FOOBAR-12345.",
    )
    lookup.add_argument("-a", "--agent", required=True)
    subparsers.add_parser(
        "discover",
        parents=[json_output],
        help="Show the workflows, modes, options, and commands for starting a task.",
    )
    inspect = subparsers.add_parser(
        "inspect",
        parents=[json_output],
        help=(
            "Print a read-only profile of the checkout: branches, activity, "
            "fixes, hot paths, layout and conventions, each with its evidence."
        ),
    )
    inspect.add_argument(
        "--commits",
        type=_positive,
        default=DEFAULT_COMMITS,
        metavar="N",
        help=f"How many recent commits to read (default {DEFAULT_COMMITS}).",
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
        help="Validate ww.yaml without compiling a workflow plan.",
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
    next_actions.add_argument(
        "--replan",
        action="store_true",
        help=(
            "At a plan_changed stop: take the workflow's new definition from "
            "the first changed item on; rerunning finished items is confirmed."
        ),
    )
    next_actions.add_argument(
        "--keep-plan",
        action="store_true",
        help="At a plan_changed stop: carry on with the run's saved plan.",
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
            "Confirm --retry, --force or --replan without the y/N "
            "prompt. Only for carrying out a decision the operator stated."
        ),
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
        help="A verification's verdict on one rule, as a JSON object. Repeatable.",
    )
    feedback = subparsers.add_parser(
        "feedback",
        parents=[json_and_role],
        help="List feedback candidates, show evidence, or record agent analysis.",
    )
    feedback.add_argument(
        "feedback_action", choices=["list", "show", "record"], nargs="?", default="list"
    )
    feedback.add_argument("task_id", nargs="?")
    feedback.add_argument("--analysis", metavar="PATH")
    interact = subparsers.add_parser(
        "interact",
        parents=[json_and_role],
        help="Record the conversation of an interactive step, or end its interaction.",
    )
    interact.add_argument("task_id")
    interact.add_argument(
        "--transcript",
        default=None,
        metavar="PATH",
        help=(
            "The whole conversation, both sides: lines starting with `Agent:` "
            "or `Operator:`, read from PATH, or from stdin with `-`."
        ),
    )
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

    onboarding = subparsers.add_parser(
        "onboarding",
        parents=[json_output],
        help=(
            "Show how far ww is set up for the operator and this project, or "
            "record a key with --set."
        ),
    )
    onboarding.add_argument(
        "--set",
        dest="assignments",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Record one key; repeatable: explain=true|false, "
            "setup.done=true|false, learned.me|team|company|project|myrole=now|<ISO>."
        ),
    )

    _setup_parser(subparsers, json_output)

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
        metavar="DAYS",
        help=(
            "Only interruptions of the last DAYS days "
            "(default: the agent_hooks.recent_days setting, 3)."
        ),
    )
    interrupted.add_argument(
        "--all", action="store_true", help="Every interruption, however old."
    )
    return parser


def _setup_parser(
    subparsers: argparse._SubParsersAction[_Parser],
    json_output: _Parser,
) -> None:
    """``setup apply``: place a proposed configuration fragment for me or the team."""
    setup = subparsers.add_parser(
        "setup",
        parents=[json_output],
        help="Place configuration a setup skill proposes, after asking.",
    )
    actions = setup.add_subparsers(dest="setup_action", required=True)
    after = _Parser(add_help=False)
    after.add_argument(
        "--json", action="store_true", dest="json_output", default=argparse.SUPPRESS
    )
    apply = actions.add_parser(
        "apply",
        parents=[after],
        help=(
            "Validate a YAML fragment (workflows, modes, profiles, documents, "
            "handlers, hooks, rules, settings) and write it to ww-setup.yaml "
            "(--for team) or ww-setup.local.yaml (--for me)."
        ),
    )
    apply.add_argument("fragment", type=Path, metavar="FILE")
    apply.add_argument(
        "--for",
        dest="audience",
        required=True,
        choices=("me", "team"),
        help="me: local files kept out of Git; team: files shared in the repo.",
    )
    apply.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and show the change, then put every file back.",
    )
    apply.add_argument(
        "--yes",
        action="store_true",
        help="Write without the y/N prompt, on the operator's word.",
    )


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
    convert = actions.add_parser(
        "convert",
        parents=[after],
        help=(
            "Record an approved check in the rule-automation store, covering "
            "the rules named, after showing it and asking."
        ),
    )
    convert.add_argument("check_name", metavar="CHECK")
    convert.add_argument(
        "--covers",
        nargs="+",
        required=True,
        metavar="RULE",
        help="The IDs of the rules the check covers, as `rules` lists them.",
    )
    convert_command = convert.add_mutually_exclusive_group(required=True)
    convert_command.add_argument("--check-shell", metavar="SCRIPT", default=None)
    convert_command.add_argument(
        CHECK_ARGV,
        nargs="+",
        metavar="ARG",
        default=None,
        help=(
            "The check's command as argv; write `--check-argv -- <arg>...`, "
            "last, when an argument starts with `-`."
        ),
    )
    convert.add_argument(
        "--assert",
        dest="assertion",
        action="append",
        metavar="empty|equals:VALUE",
        help="A condition the check's output must meet; repeat for several.",
    )
    convert.add_argument(
        "--config",
        nargs="+",
        default=[],
        metavar="PATH",
        help=(
            "The project files holding the check's logic; the check runs only "
            "where they all exist."
        ),
    )
    convert.add_argument(
        "--proven",
        action="store_true",
        help="The check failed on a deliberate violation and passed otherwise.",
    )
    convert.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be recorded and change nothing.",
    )
    convert.add_argument(
        "--yes",
        action="store_true",
        help="Record without the y/N prompt, on the operator's word.",
    )
    decline = actions.add_parser(
        "decline",
        parents=[after],
        help=(
            "Record rules as not convertible, after asking: judged from then "
            "on and never proposed for scriptizing again."
        ),
    )
    decline.add_argument("rule_ids", nargs="+", metavar="RULE")
    decline.add_argument("--reason", required=True, help="Why; recorded.")
    decline.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be recorded and change nothing.",
    )
    decline.add_argument(
        "--yes",
        action="store_true",
        help="Record without the y/N prompt, on the operator's word.",
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
    command.add_argument(
        CHECK_ARGV,
        nargs="+",
        metavar="ARG",
        default=None,
        help=(
            "The check's command as argv; write `--check-argv -- <arg>...`, "
            "last, when an argument starts with `-`."
        ),
    )
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
