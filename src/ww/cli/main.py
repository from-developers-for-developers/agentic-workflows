# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point: parse arguments, run one service command, render and log it."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import uuid
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from ww.config import load_configuration
from ww.config.composition import compose_configuration
from ww.config_files import (
    LEGACY_FILES,
    SETTINGS_FILE,
    WORKFLOWS_FILE,
    check_legacy_files,
    display_path,
    rename_legacy_files,
)
from ww.errors import StateError, WwError
from ww.executable import printed_executable, ww_command
from ww.extensions import ExtensionContext, ExtensionRegistry
from ww.hooks import (
    HOOK_EVENTS,
    HookAgent,
    answer_hook,
    hook_agent,
    hook_snippet,
    hooks_file,
    install_hooks,
    is_project_root,
    registered_elsewhere,
    uninstall_hooks,
)
from ww.hooks.notices import interruption_notice
from ww.instructions import Instruction
from ww.items import WorkItem
from ww.operator_ui import run_operator_page
from ww.operator_ui.server import operator_wait_seconds
from ww.output import (
    render,
    render_initialization,
    render_initialization_welcome,
    render_item_update,
    render_plan,
    render_reset,
    render_status,
)
from ww.plan import PlanCompilationOptions, compile_workflow_plan
from ww.project_config import compose_settings, load_project_config
from ww.service import WorkflowService
from ww.storage import Storage

from .audit import _log_record
from .catalogs import (
    _catalog_agents,
    _catalog_extensions,
    _catalog_modes,
    _catalog_projects,
    _catalog_runtimes,
    _catalog_workflows,
)
from .discover import render_discover
from .initialization import (
    _finish_initialization,
    _initialization_options,
    _link_agent_instructions,
    install_agent_hooks,
)
from .lookup import render_lookup
from .parser import _metadata_values, _named_values, _variables, build_parser
from .prompts import _confirm_force_next, confirm_interrupted_retry
from .updates import announce, render_updates

_MANAGER_ONLY_COMMANDS = frozenset({"start", "next"})
# Discovery, linting, and planning are side-effect free: no task state, artifacts,
# commands, or execution-log records are created.
_READ_ONLY_COMMANDS = frozenset(
    {
        "discover",
        "lookup",
        "lint",
        "plan",
        "documents",
        "interactions",
        "updates",
        "interrupted",
    }
)
# Commands whose stdout is consumed by a program rather than read, whether or
# not ``--json`` was passed. An update notice goes to stderr for these, so it
# never lands in the middle of a document someone is parsing.
_MACHINE_READABLE_COMMANDS = frozenset(
    {
        "add-child",
        "add-item",
        "agents",
        "artifacts",
        "documents",
        "extension",
        "extensions",
        "item",
        "items",
        "metadata",
        "modes",
        "projects",
        "remove-item",
        "runtimes",
        "workflows",
    }
)


@dataclass(frozen=True)
class _Context:
    args: argparse.Namespace
    storage: Storage
    extensions: ExtensionRegistry
    service: WorkflowService
    # Former configuration names ``init`` renamed, as (old, new) pairs.
    renamed: tuple[tuple[str, str], ...] = ()

    @property
    def task_id(self) -> str:
        return cast(str, self.args.task_id)


@dataclass(frozen=True)
class _Outcome:
    """What one command printed and what its audit record should say."""

    text: str
    workflow: str | None = None
    task_id: str | None = None
    error: str | None = None
    exit_code: int = 0


def _json(value: object) -> str:
    return json.dumps(value, indent=2) + "\n"


def _instruction_outcome(instruction: Instruction, json_output: bool) -> _Outcome:
    failed = instruction.status in {"failed", "interrupted"}
    return _Outcome(
        render(instruction, json_output) + "\n",
        instruction.workflow,
        instruction.task_id,
        error=instruction.error if failed else None,
        exit_code=1 if failed else 0,
    )


def _init(context: _Context) -> _Outcome:
    welcome = render_initialization_welcome(context.args.json_output)
    if welcome:
        if (
            sys.stdout.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
        ):
            welcome = f"\033[36m{welcome}\033[0m"
        sys.stdout.write(welcome)
        sys.stdout.flush()
        print(f"Setting up ww in: {context.storage.root}\n", flush=True)
    workflows, project_config, ignore_runtime, skill_installs = (
        _initialization_options(context.storage, context.args)
    )
    result = context.service.initialize(
        workflows=workflows,
        project_config=project_config,
        ignore_runtime=ignore_runtime,
        skill_installs=skill_installs,
    )
    if context.renamed:
        result = replace(
            result,
            created=(
                *(f"{new} (renamed from {old})" for old, new in context.renamed),
                *result.created,
            ),
        )
    if context.args.link_instructions:
        result = _link_agent_instructions(context.storage, result)
    result = install_agent_hooks(context.storage, context.args, result)
    result = _finish_initialization(
        context.storage, result, shown=not context.args.json_output
    )
    return _Outcome(render_initialization(result, context.args.json_output) + "\n")


def _plan(context: _Context) -> _Outcome:
    args = context.args
    plan = compile_workflow_plan(
        load_configuration(context.storage.config_path, context.extensions),
        context.storage.root,
        args.workflow,
        args.agent,
        args.task_id,
        context.extensions,
        PlanCompilationOptions(task_id=args.task_id, project=args.project),
        context.extensions.config,
    )
    rendered = render_plan(plan, args.json_output)
    if not args.json_output:
        rendered += f"\n{_configuration_files(context.storage, context.extensions)}"
    return _Outcome(rendered)


def _lint(context: _Context) -> _Outcome:
    load_configuration(context.storage.config_path, context.extensions)
    for project in context.extensions.config.projects:
        context.extensions.validate_configuration(project.name)
    notices = "".join(
        f"Notice: {notice}\n"
        for notice in compose_configuration(context.storage.config_path).notices
    )
    return _Outcome(
        f"{WORKFLOWS_FILE} is valid.\n"
        f"{_configuration_files(context.storage, context.extensions)}"
        f"{notices}"
    )


def _configuration_files(storage: Storage, extensions: ExtensionRegistry) -> str:
    """The configuration files this project reads, from machine to local.

    A configured project's own settings files follow on their own line, so an
    extension setting that applies only there is visible where it was read.
    """
    workflows = compose_configuration(storage.config_path).sources
    _, settings = compose_settings(storage.project_config_path)
    files = (
        *workflows,
        *(display_path(path, storage.root) for path in settings),
    )
    rendered = "Configuration files: " + ", ".join(files) + "\n"
    for project in extensions.config.projects:
        sources = extensions.project_settings(project.name).sources
        if sources:
            rendered += (
                f"Project {project.name} extension settings: "
                + ", ".join(display_path(path, storage.root) for path in sources)
                + "\n"
            )
    return rendered


def _start(context: _Context) -> _Outcome:
    args = context.args
    return _instruction_outcome(
        context.service.start(
            args.workflow,
            args.task_id,
            tuple(args.modes),
            args.agent,
            args.model,
            args.reasoning,
            args.workflow_runtime,
            args.init_artifact,
            caller_role=args.role,
            branch_naming_strategy=args.branch_naming_strategy,
            project=args.project,
            fresh_items=args.fresh_items,
        ),
        args.json_output,
    )


def _next(context: _Context) -> _Outcome:
    args = context.args
    return _with_interruption(
        context,
        _instruction_outcome(
            context.service.next(
                context.task_id,
                args.model,
                args.reasoning,
                args.force,
                retry=args.retry,
                force_reason=args.force_reason,
                outcome=args.outcome,
                selected_agent=args.selected_agent,
                caller_role=args.role,
            ),
            args.json_output,
        ),
    )


def _loop(context: _Context) -> _Outcome:
    args = context.args
    return _instruction_outcome(
        context.service.loop(
            context.task_id,
            _variables(args.variable),
            args.artifact,
            _metadata_values(args.metadata),
            selected_agent=args.selected_agent,
            selected_model=args.selected_model,
            selected_reasoning=args.selected_reasoning,
            continue_loop=args.continue_loop,
            summary_for_next=args.summary_for_next_step,
            caller_role=args.role,
        ),
        args.json_output,
    )


def _complete(context: _Context) -> _Outcome:
    args = context.args
    return _instruction_outcome(
        context.service.complete(
            context.task_id,
            _variables(args.variable),
            args.artifact,
            _metadata_values(args.metadata),
            selected_agent=args.selected_agent,
            selected_model=args.selected_model,
            selected_reasoning=args.selected_reasoning,
            summary_for_next=args.summary_for_next_step,
            caller_role=args.role,
        ),
        args.json_output,
    )


def _interact(context: _Context) -> _Outcome:
    args = context.args
    if args.await_operator:
        others = (args.operator, args.agent, args.choice, args.end_interaction)
        if any(others) or args.pause:
            raise StateError(
                "interact --await works the operator page on its own; record "
                "entries with a separate interact call"
            )
        result = run_operator_page(
            context.service,
            context.task_id,
            timeout=operator_wait_seconds(),
            open_browser=webbrowser.open,
            caller_role=args.role,
        )
        instruction = context.service.instruction(
            context.task_id, caller_role=args.role
        )
        outcome = _instruction_outcome(instruction, args.json_output)
        if args.json_output:
            text = _json({**instruction.to_dict(), "operator_page": result.to_dict()})
        else:
            text = outcome.text.rstrip("\n") + "\n\n" + result.render()
        return replace(outcome, text=text)
    return _instruction_outcome(
        context.service.interact(
            context.task_id,
            operator=args.operator,
            agent=args.agent,
            choice=args.choice,
            end=args.end_interaction,
            pause=args.pause,
            caller_role=args.role,
        ),
        args.json_output,
    )


def _interactions(context: _Context) -> _Outcome:
    text = context.service.interactions_text(context.task_id)
    return _Outcome(text if text.endswith("\n") or not text else text + "\n")


def _fail(context: _Context) -> _Outcome:
    args = context.args
    return _instruction_outcome(
        context.service.fail(context.task_id, args.error, caller_role=args.role),
        args.json_output,
    )


def _status(context: _Context) -> _Outcome:
    args = context.args
    status = context.service.task_status(
        context.task_id, args.run_id, caller_role=args.role
    )
    return _with_interruption(
        context,
        _Outcome(
            render_status(status, args.json_output) + "\n",
            status.workflow,
            status.task_id,
        ),
    )


def _instruction(context: _Context) -> _Outcome:
    args = context.args
    return _with_interruption(
        context,
        _instruction_outcome(
            context.service.instruction(
                context.task_id, args.run_id, caller_role=args.role
            ),
            args.json_output,
        ),
    )


def _with_interruption(context: _Context, outcome: _Outcome) -> _Outcome:
    """Lead with a notice while the task's last session stopped mid-step.

    The notice shows until the interrupted attempt completes or fails, so a
    compaction or a new session between reading it and acting keeps it.
    """
    if context.args.json_output:
        return outcome
    interruption = context.service.interruption(context.task_id)
    if interruption is None:
        return outcome
    notice = interruption_notice(interruption, context.task_id)
    return replace(outcome, text=f"> {notice}\n\n{outcome.text}")


def _hook(context: _Context) -> _Outcome:
    """Install, remove, or show ww's hooks for one agent."""
    args = context.args
    agent = hook_agent(args.agent)
    local = bool(args.local)
    target = hooks_file(agent, local)
    if args.hook_action == "show":
        return _Outcome(
            f"ww's hooks for {agent.name} belong in {target}:\n\n"
            + hook_snippet(agent)
            + _duplicate_notice(context, agent, local)
        )
    if args.hook_action == "install":
        installation = install_hooks(context.storage, agent, local=local)
        verb = {
            "installed": "Installed ww's hooks for",
            "unchanged": "ww's hooks are already installed for",
        }[installation.action]
        notice = _duplicate_notice(context, agent, local)
    else:
        installation = uninstall_hooks(context.storage, agent, local=local)
        verb = {
            "removed": "Removed ww's hooks for",
            "absent": "No ww hooks were installed for",
        }[installation.action]
        notice = ""
    return _Outcome(f"{verb} {agent.name} in {installation.path}.\n{notice}")


def _duplicate_notice(context: _Context, agent: HookAgent, local: bool) -> str:
    """Warn that ww's hooks also sit in the agent's other project file."""
    other = registered_elsewhere(context.storage, agent, local=local)
    if other is None:
        return ""
    flag = "" if local else " --local"
    return (
        f"Notice: {other} also registers ww's hooks, so every hook runs twice. "
        f"Remove one copy with `{ww_command()} hook uninstall --agent "
        f"{agent.name}{flag}`.\n"
    )


def _interrupted(context: _Context) -> _Outcome:
    """List the tasks whose last agent session stopped mid-step."""
    args = context.args
    entries = (
        context.service.interruptions()
        if args.all
        else context.service.hook_records.recent(args.since)
    )
    if args.json_output:
        return _Outcome(
            _json(
                [
                    {"task_id": task_id, **record.to_dict()}
                    for task_id, record in entries
                ]
            )
        )
    if not entries:
        window = "" if args.all else f" in the last {args.since} day(s)"
        return _Outcome(f"No task was interrupted{window}.\n")
    lines = [
        f"- {task_id} · {record.step or record.item_name} (attempt "
        f"{record.attempt}) · {record.at} · {record.agent}"
        + (f" · {record.reason}" if record.reason else "")
        for task_id, record in entries
    ]
    return _Outcome("\n".join(lines) + "\n")


def _metadata(context: _Context) -> _Outcome:
    args = context.args
    if args.project:
        if args.task_id is not None:
            raise StateError("metadata --project does not accept a task ID")
        return _Outcome(_json(context.service.project_metadata()))
    if args.task_id is None:
        raise StateError("metadata requires a task ID or --project")
    return _Outcome(_json(context.service.metadata(args.task_id)), None, args.task_id)


def _documents(context: _Context) -> _Outcome:
    task_id = context.args.task_id
    return _Outcome(_json(context.service.documents_listing(task_id)), None, task_id)


def _items(context: _Context) -> _Outcome:
    items = context.service.items(context.task_id, context.args.run_id)
    return _Outcome(_json([item.to_dict() for item in items]))


def _item(context: _Context) -> _Outcome:
    args = context.args
    if (args.item_id is None) == (args.by is None):
        raise StateError("item takes --id or --by NAME=VALUE")
    if args.by is not None:
        ((name, value),) = _named_values([args.by], "--by")
        item = context.service.find_item(context.task_id, name, value, args.run_id)
    else:
        item = context.service.item(context.task_id, args.item_id, args.run_id)
    return _Outcome(_json(item.to_dict()))


def _artifacts(context: _Context) -> _Outcome:
    artifacts = context.service.artifacts(context.task_id, context.args.run_id)
    return _Outcome(_json(list(artifacts)))


def _add_item(context: _Context) -> _Outcome:
    args = context.args
    item = context.service.add_item(
        context.task_id,
        WorkItem(
            args.id,
            args.item,
            reference_to_id=args.reference_to_id,
            fields=_named_values(args.field, "--field"),
        ),
    )
    return _Outcome(_json(item.to_dict()))


def _update_item(context: _Context) -> _Outcome:
    args = context.args
    changes = {
        name: value
        for name, value in (
            ("item", args.item),
            ("processed_item", args.processed_item),
            ("proposed_solution", args.proposed_solution),
            ("actual_solution", args.actual_solution),
            ("resolved", args.resolved),
            ("reported", args.reported),
        )
        if value is not None
    }
    if args.field:
        changes["fields"] = dict(_named_values(args.field, "--field"))
    result = context.service.update_item(
        context.task_id, args.item_id, caller_role=args.role, **changes
    )
    return _Outcome(
        render_item_update(result, args.json_output) + "\n", None, context.task_id
    )


def _remove_item(context: _Context) -> _Outcome:
    args = context.args
    item = context.service.remove_item(context.task_id, args.item_id)
    return _Outcome(_json(item.to_dict()), None, context.task_id)


def _add_child(context: _Context) -> _Outcome:
    args = context.args
    child = context.service.add_child(
        context.task_id, args.id, args.description, project=args.project
    )
    return _Outcome(_json(child.to_dict()))


def _child(context: _Context) -> _Outcome:
    args = context.args
    return _instruction_outcome(
        context.service.start_child(args.parent_task_id, args.child_id),
        args.json_output,
    )


def _reset(context: _Context) -> _Outcome:
    args = context.args
    if not args.yes:
        raise WwError("reset requires --yes because it deletes task artifacts")
    result = context.service.reset(context.task_id)
    return _Outcome(
        render_reset(result, args.json_output) + "\n", None, context.task_id
    )


def _cleanup(context: _Context) -> _Outcome:
    result = context.service.cleanup()
    if context.args.json_output:
        return _Outcome(json.dumps({"removed_locks": result.removed_locks}) + "\n")
    return _Outcome(f"Removed {result.removed_locks} inactive lock file(s).\n")


def _extension(context: _Context) -> _Outcome:
    """Run one extension command against that extension's own store."""
    args = context.args
    command = context.extensions.command(args.extension_id, args.extension_command)
    try:
        result = command.run(
            ExtensionContext(
                root=context.storage.root,
                store=context.extensions.store(args.extension_id),
                config=context.extensions.settings(args.extension_id, args.project),
                arguments=tuple(args.extension_arguments),
            )
        )
    except Exception as error:  # noqa: BLE001 - contain trusted extension failures
        raise StateError(
            f"extension command {args.extension_id}/{args.extension_command} failed: "
            f"{type(error).__name__}: {error}"
        ) from error
    if not isinstance(result, str):
        raise StateError("extension command returned a non-string result")
    return _Outcome(result + "\n")


_HANDLERS: dict[str, Callable[[_Context], _Outcome]] = {
    "init": _init,
    "discover": lambda c: _Outcome(
        render_discover(c.storage, c.extensions, c.args.json_output) + "\n"
    ),
    "lookup": lambda c: _Outcome(
        render_lookup(
            c.storage,
            c.extensions,
            c.service.tasks,
            c.args.reference,
            c.args.agent,
            c.args.json_output,
        )
        + "\n"
    ),
    "modes": lambda c: _Outcome(_catalog_modes(c.storage, c.extensions) + "\n"),
    "workflows": lambda c: _Outcome(_catalog_workflows(c.storage, c.extensions) + "\n"),
    "runtimes": lambda c: _Outcome(_catalog_runtimes() + "\n"),
    "agents": lambda c: _Outcome(_catalog_agents() + "\n"),
    "projects": lambda c: _Outcome(_catalog_projects(c.extensions) + "\n"),
    "extensions": lambda c: _Outcome(_catalog_extensions(c.extensions) + "\n"),
    "extension": _extension,
    "plan": _plan,
    "lint": _lint,
    "start": _start,
    "next": _next,
    "loop": _loop,
    "complete": _complete,
    "interact": _interact,
    "interactions": _interactions,
    "fail": _fail,
    "status": _status,
    "instruction": _instruction,
    "metadata": _metadata,
    "documents": _documents,
    "items": _items,
    "item": _item,
    "artifacts": _artifacts,
    "add-item": _add_item,
    "update-item": _update_item,
    "remove-item": _remove_item,
    "add-child": _add_child,
    "child": _child,
    "reset": _reset,
    "cleanup": _cleanup,
    "updates": lambda c: _Outcome(render_updates(c.storage, c.args)),
    "hook": _hook,
    "interrupted": _interrupted,
}


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if _runtime_hook_call(arguments):
        return _answer_hook(arguments)
    args = build_parser().parse_args(arguments)
    args.invocation_id = str(uuid.uuid4())
    if (
        getattr(args, "role", None) == "worker"
        and args.command in _MANAGER_ONLY_COMMANDS
    ):
        print(f"ww error: {args.command} is a manager-role command", file=sys.stderr)
        return 1
    if args.command == "next":
        if args.force and not args.force_reason:
            print("ww error: next --force requires --force-reason", file=sys.stderr)
            return 1
        if args.retry and not confirm_interrupted_retry():
            return 1
    storage = Storage(
        (args.root or Path.cwd()).resolve()
        if args.command == "init"
        else _resolve_project_root(args.root)
    )
    # An available update is reported before the command's own output, so an
    # agent relaying that output shows it to the operator first. ``updates``
    # is exempt: it is the command that reports one.
    if args.command != "updates":
        announce(storage, to_stderr=_machine_readable(args))
    workflow = cast(str | None, getattr(args, "workflow", None))
    task_id = cast(str | None, getattr(args, "task_id", None))
    logged = args.command not in _READ_ONLY_COMMANDS

    def log(outcome: str, error: str | None, *scope: str | None) -> None:
        record_workflow, record_task = scope if scope else (workflow, task_id)
        storage.append_log(
            _log_record(
                args.command, record_workflow, record_task, outcome, error, args
            )
        )

    try:
        # ``init`` renames files still under a former name; everything else
        # stops on one rather than silently running without it.
        renamed = (
            rename_legacy_files(storage.root) if args.command == "init" else ()
        )
        check_legacy_files(storage.root)
        extensions = ExtensionRegistry.discover(storage.root)
        service = WorkflowService(storage, extensions=extensions)
        # Check what the force would do before asking the operator to approve
        # it, so a refused force is reported instead of confirmed and refused.
        if args.command == "next" and args.force:
            effect = service.force_target(args.task_id)
            if not _confirm_force_next(effect):
                return 1
        if logged:
            log("started", None)
        # Every command this invocation prints starts with the project's ww.
        with printed_executable(extensions.config.executable):
            result = _HANDLERS[args.command](
                _Context(args, storage, extensions, service, renamed)
            )
    except WwError as error:
        if logged:
            with contextlib.suppress(OSError, WwError):
                log("error", str(error))
        print(f"ww error: {error}", file=sys.stderr)
        return 1
    if logged:
        log(
            "error" if result.error is not None else "ok",
            result.error,
            result.workflow,
            result.task_id,
        )
    sys.stdout.write(result.text)
    return result.exit_code


def _runtime_hook_call(arguments: list[str]) -> bool:
    """Whether an agent is calling one of ww's hooks, not setting them up."""
    return any(
        value == "hook"
        and index + 1 < len(arguments)
        and arguments[index + 1] in HOOK_EVENTS
        for index, value in enumerate(arguments)
    )


def _answer_hook(arguments: list[str]) -> int:
    """Answer an agent's hook call; this never breaks the agent.

    Any failure, a bad argument included, exits 0 with no output: exit code
    2 means "continue" to some agents' stop hooks, and an unexpected message
    would become part of the agent's context. Every call is still written to
    the audit log, with ww's decision, so a session can be followed there.
    """
    storage: Storage | None = None
    args: argparse.Namespace | None = None
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            args = build_parser().parse_args(arguments)
        args.invocation_id = str(uuid.uuid4())
        root = _resolve_project_root(args.root)
        if not is_project_root(root):
            return 0
        storage = Storage(root)
        payload = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
        config = load_project_config(root / SETTINGS_FILE)
        with printed_executable(config.executable):
            answer = answer_hook(
                storage, hook_agent(args.agent), args.hook_action, payload
            )
    except BaseException as error:  # noqa: BLE001 - a hook must never break the agent
        if isinstance(error, KeyboardInterrupt):
            return 0
        _log_hook(storage, args, "error", type(error).__name__)
        return 0
    _log_hook(storage, args, "ok", answer.decision)
    sys.stdout.write(answer.text)
    return 0


def _log_hook(
    storage: Storage | None,
    args: argparse.Namespace | None,
    outcome: str,
    decision: str,
) -> None:
    """Record one hook call and what ww decided, never the agent's payload."""
    if storage is None or args is None:
        return
    record: dict[str, object] = {
        **_log_record("hook", None, None, outcome, None, args),
        "hook_event": args.hook_action,
        "hook_agent": args.agent,
        "hook_decision": decision,
    }
    with contextlib.suppress(Exception):
        storage.append_log(record)


def _machine_readable(args: argparse.Namespace) -> bool:
    """Whether this invocation's stdout is meant for a program."""
    return bool(getattr(args, "json_output", False)) or (
        args.command in _MACHINE_READABLE_COMMANDS
    )


def _resolve_project_root(explicit_root: Path | None) -> Path:
    """Find the durable project root when invoked from a linked worktree.

    A linked Git worktree has its own checkout but shares the primary
    repository's git directory. ww's task state, configuration, and runtime
    files belong to that primary project, so an omitted ``--root`` follows the
    shared git directory back to its checkout. An explicit ``--root`` is always
    honored for embedded and non-Git projects.
    """
    if explicit_root is not None:
        return explicit_root.resolve()
    current = Path.cwd().resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=current,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        result = None
    if result is not None and result.returncode == 0:
        common_dir = Path(result.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = current / common_dir
        resolved = common_dir.resolve()
        if resolved.name == ".git":
            return resolved.parent
    for candidate in (current, *current.parents):
        if any(
            (candidate / name).is_file() for name in (WORKFLOWS_FILE, *LEGACY_FILES)
        ) or (candidate / ".ww").exists():
            return candidate
    return current
