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
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from ww import rule_conversion, rule_writes, setup_apply
from ww.config import load_configuration
from ww.config.composition import compose_configuration
from ww.config_files import (
    SETTINGS_FILE,
    WORKFLOWS_FILE,
    display_path,
)
from ww.defaults import PROJECT_LAUNCHER
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
from ww.inspect import inspect_checkout, render_markdown
from ww.instructions import Instruction
from ww.items import WorkItem
from ww.onboarding import Onboarding, render_onboarding
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
from ww.output_adapters.rule_pages import (
    render_check_preview,
    render_convert_preview,
    render_decline_preview,
    render_orphans,
    render_revoke_preview,
    render_revoked,
    render_rule_view,
    render_rules_listing,
)
from ww.plan import PlanCompilationOptions, compile_workflow_plan
from ww.project_config import compose_settings, load_project_config
from ww.rule_disputes import DisputeLog
from ww.rule_store import (
    CheckSpec,
    RuleAutomation,
    RuleStore,
    parse_command,
)
from ww.rule_verification import revoke_check
from ww.rule_views import declared_hashes, orphans, prune, rules_listing
from ww.service import WorkflowService
from ww.storage import Storage
from ww.workflow_config import (
    ALL,
    ALL_NAMES,
    NameFilter,
    RuleDefinition,
    WorkflowConfiguration,
    every_step,
)

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
from .prompts import (
    _confirm_force_next,
    confirm_interrupted_retry,
    confirm_operator,
)
from .updates import announce, render_updates

_MANAGER_ONLY_COMMANDS = frozenset({"start", "next"})
# Discovery, linting, and planning are side-effect free: no task state, artifacts,
# commands, or execution-log records are created.
_READ_ONLY_COMMANDS = frozenset(
    {
        "discover",
        "inspect",
        "lookup",
        "lint",
        "plan",
        "documents",
        "interactions",
        "updates",
        "interrupted",
        "check",
        "rule",
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
        "update-child",
        "workflows",
    }
)


@dataclass(frozen=True)
class _Context:
    args: argparse.Namespace
    storage: Storage
    extensions: ExtensionRegistry
    service: WorkflowService

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


def _instruction_outcome(
    instruction: Instruction, json_output: bool, *, completing: bool = False
) -> _Outcome:
    failed = instruction.status in {"failed", "interrupted"}
    repairing = completing and instruction.handler_repair is not None
    # A rejected completion exits non-zero too, so the worker reads the page;
    # showing the fix page again later is no failure of that command.
    rejected = completing and instruction.fix_required is not None and not failed
    return _Outcome(
        render(instruction, json_output) + "\n",
        instruction.workflow,
        instruction.task_id,
        error=(
            instruction.error
            if failed or repairing
            else _rejection(instruction)
            if rejected
            else None
        ),
        exit_code=1 if failed or rejected or repairing else 0,
    )


def _rejection(instruction: Instruction) -> str:
    fix = instruction.fix_required
    assert fix is not None
    return (
        "completion rejected: "
        + ", ".join(failure.id for failure in fix.failures)
        + " failed"
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
    workflows, project_config, ignore_runtime, skill_installs = _initialization_options(
        context.storage, context.args
    )
    result = context.service.initialize(
        workflows=workflows,
        project_config=project_config,
        ignore_runtime=ignore_runtime,
        skill_installs=skill_installs,
    )
    if context.args.link_instructions:
        result = _link_agent_instructions(context.storage, result)
    result = install_agent_hooks(context.storage, context.args, result)
    result = _finish_initialization(
        context.storage,
        result,
        shown=not context.args.json_output,
        force=context.args.force,
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
    configuration = load_configuration(context.storage.config_path, context.extensions)
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
        f"{_launcher_warning(context.storage.root)}"
        f"{_rules_summary(configuration)}"
        f"{_rule_store_summary(RuleStore(context.storage.root), configuration)}"
        f"{_unscriptized_warning(RuleStore(context.storage.root), configuration)}"
        f"{_disputes_summary(DisputeLog(context.storage.root))}"
    )


def _launcher_warning(root: Path) -> str:
    """Warn when ./ww is not the launcher this ww writes.

    The launcher is ww-owned; one an older ww wrote can read old
    configuration names and run the wrong binary, and ``init`` rewrites it.
    """
    launcher = root / "ww"
    if not launcher.is_file():
        return ""
    if launcher.read_text(encoding="utf-8") == PROJECT_LAUNCHER:
        return ""
    return (
        "Warning: ./ww differs from the current launcher; run "
        f"`{ww_command()} init` to rewrite it.\n"
    )


def _disputes_summary(log: DisputeLog) -> str:
    """Rules and checks workers have disputed, most disputed first.

    A check disputed again and again is worth the operator's look: its
    wording or its command may be wrong.
    """
    entries = log.load()
    counts: dict[str, int] = {}
    last: dict[str, str] = {}
    for entry in entries:
        counts[entry.check] = counts.get(entry.check, 0) + 1
        last[entry.check] = f"{entry.task_id} {entry.step}"
    return "".join(
        f"Disputed {check}: {count} time{'s' if count != 1 else ''}, last in "
        f"{last[check]}\n"
        for check, count in sorted(counts.items(), key=lambda pair: -pair[1])
    )


def _rule_store_summary(store: RuleStore, configuration: WorkflowConfiguration) -> str:
    """What ``ww-rule-automation.json`` holds that needs the operator's eye.

    An entry is an orphan when no rule of the composed configuration has its
    wording any more (``ww rules prune`` deletes those). Lint removes nothing.
    """
    if not store.exists():
        return ""
    automation = store.load()
    hashes = declared_hashes(configuration)
    lines = [
        f"Rule store: {len(automation.rules)} rule"
        f"{'s' if len(automation.rules) != 1 else ''}, {len(automation.checks)} "
        f"check{'s' if len(automation.checks) != 1 else ''}\n"
    ]
    for text_hash, entry in automation.rules.items():
        if text_hash not in hashes:
            lines.append(f"Orphan rule {text_hash[:12]}: {entry.text}\n")
    return "".join(lines)


def _unscriptized_warning(
    store: RuleStore, configuration: WorkflowConfiguration
) -> str:
    """Name the declared rules no check covers yet; verifiers judge them.

    A warning only: ``ww-scriptize-rules`` builds checks for them, and is
    suggested only while it is switched on.
    """
    rules = rule_conversion.unscriptized_rules(configuration, store.load())
    if not rules:
        return ""
    warning = (
        f"Warning: {len(rules)} rule{'s have' if len(rules) != 1 else ' has'} no "
        "check yet (" + ", ".join(rule.id for rule in rules) + ")"
    )
    if rule_conversion.SCRIPTIZE_WORKFLOW not in configuration.workflows_by_name:
        return f"{warning}.\n"
    return (
        f"{warning}; `{rule_conversion.SCRIPTIZE_WORKFLOW}` builds checks for them.\n"
    )


def _rules_summary(configuration: WorkflowConfiguration) -> str:
    """``Rules: N groups, M rules``, when the configuration declares any.

    A rule counts once however many groups or steps reach it.
    """
    groups = len(configuration.rule_groups)
    rules = {rule.id for group in configuration.rule_groups for rule in group.rules} | {
        entry.id
        for step in every_step(configuration)
        for entry in step.rules
        if isinstance(entry, RuleDefinition)
    }
    if not groups and not rules:
        return ""
    return (
        f"Rules: {groups} group{'s' if groups != 1 else ''}, "
        f"{len(rules)} rule{'s' if len(rules) != 1 else ''}\n"
    )


def _configuration_files(storage: Storage, extensions: ExtensionRegistry) -> str:
    """The configuration files this project reads, from user to local.

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
            args.requirements,
            caller_role=args.role,
            branch_naming_strategy=args.branch_naming_strategy,
            project=args.project,
            fresh_items=args.fresh_items,
        ),
        args.json_output,
    )


def _confirmation(assume_yes: bool) -> str:
    """How a gated choice was confirmed, for the audit record."""
    return "--yes" if assume_yes else "operator at a terminal"


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
                reassign=args.reassign,
                replan=args.replan,
                keep_plan=args.keep_plan,
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
            summary_for_next=args.summary,
            caller_role=args.role,
            assignment=args.assignment,
        ),
        args.json_output,
        completing=True,
    )


def _check(context: _Context) -> _Outcome:
    """Run the active step's checks now; exits 1 when any would fail."""
    preview = context.service.check(context.task_id)
    text = (
        _json(preview.to_dict())
        if context.args.json_output
        else render_check_preview(preview)
    )
    failed = bool(preview.failures)
    return _Outcome(
        text,
        None,
        context.task_id,
        error=(
            "checks would fail: " + ", ".join(f.id for f in preview.failures)
            if failed
            else None
        ),
        exit_code=1 if failed else 0,
    )


def _dispute(context: _Context) -> _Outcome:
    args = context.args
    return _instruction_outcome(
        context.service.dispute(
            context.task_id,
            args.check_id,
            args.reason,
            caller_role=args.role,
            assignment=args.assignment,
        ),
        args.json_output,
    )


def _rule(context: _Context) -> _Outcome:
    view = context.service.rule(context.task_id, context.args.rule_id)
    return _Outcome(
        _json(view.to_dict()) if context.args.json_output else render_rule_view(view),
        None,
        context.task_id,
    )


def _rules(context: _Context) -> _Outcome:
    """List the declared rules, or prune the store's orphan entries."""
    args = context.args
    configuration = load_configuration(context.storage.config_path, context.extensions)
    if args.rules_action == "prune":
        return _prune(context, configuration)
    if args.rules_action == "revoke":
        return _revoke(context)
    if args.rules_action == "convert":
        return _convert(context, configuration)
    if args.rules_action == "decline":
        return _decline(context, configuration)
    if args.rules_action is not None:
        return _rule_write(context, configuration)
    settings = context.extensions.config
    listing = replace(
        rules_listing(
            configuration,
            context.storage.root,
            DisputeLog(context.storage.root).load(),
            RuleStore(context.storage.root).load(),
        ),
        check_guidance=settings.rule_check_guidance,
    )
    return _Outcome(
        _json(listing.to_dict()) if args.json_output else render_rules_listing(listing)
    )


def _rule_write(context: _Context, configuration: WorkflowConfiguration) -> _Outcome:
    """Write a rule file or group, validated; nothing is committed."""
    args = context.args
    root = context.storage.root
    project = rule_writes.RuleProject(
        root,
        context.storage.config_path,
        configuration,
        compose_configuration(context.storage.config_path),
        context.extensions,
    )
    action = args.rules_action
    if action == "add" and args.new_group is not None:
        if args.group_name is not None or args.text is not None:
            raise StateError(
                "rules add takes either GROUP with --text, or --group with --dir"
            )
        if args.directory is None:
            raise StateError("rules add --group needs --dir")
        write = rule_writes.plan_add_group(
            project,
            args.new_group,
            args.directory,
            workflows=_filter_option(args.workflows, "--workflows"),
            steps=_filter_option(args.steps, "--steps"),
        )
    elif action == "add":
        if args.group_name is None or args.text is None:
            raise StateError(
                "rules add takes GROUP with --text, or --group NAME with --dir"
            )
        if (
            args.directory is not None
            or args.workflows is not None
            or (args.steps is not None)
        ):
            raise StateError("--dir, --workflows and --steps go with --group")
        write = rule_writes.plan_add_rule(
            project,
            args.group_name,
            args.text,
            paths=tuple(args.paths or ()),
            check=rule_writes.check_mapping(
                args.check_shell,
                tuple(args.check_argv) if args.check_argv else None,
                tuple(args.assertion) if args.assertion else None,
            ),
            stem=args.stem,
        )
    elif action == "edit":
        write = rule_writes.plan_edit(
            project,
            RuleStore(root).load(),
            args.rule_id,
            text=args.text,
            paths=tuple(args.paths) if args.paths is not None else None,
        )
    elif action == "move":
        write = rule_writes.plan_move(project, args.rule_id, args.target_group)
    elif action == "filter":
        write = rule_writes.plan_filter(
            project,
            args.group_name,
            workflows=_filter_option(args.workflows, "--workflows"),
            steps=_filter_option(args.steps, "--steps"),
            all_workflows=args.all_workflows,
            all_steps=args.all_steps,
        )
    else:
        write = rule_writes.plan_promote(
            project, RuleStore(root).load(), args.check_name
        )
    return _Outcome(
        rule_writes.apply_write(project, write, dry_run=args.dry_run).render()
    )


def _filter_option(values: list[str] | None, option: str) -> NameFilter | None:
    """``--workflows``/``--steps``: ``'*'`` alone for all, else the names given.

    ``None`` when the option is absent; with no name, an empty filter.
    """
    if values is None:
        return None
    if values == [ALL_NAMES]:
        return ALL
    if ALL_NAMES in values:
        raise StateError(f"{option} takes '*' alone or names, not both")
    return NameFilter.of(values)


def _revoke(context: _Context) -> _Outcome:
    """Reject a store check and its rules after showing it and asking."""
    args = context.args
    store = RuleStore(context.storage.root)
    name = args.check_name
    check = store.load().checks.get(name)
    if check is None:
        raise StateError(f"the rule-automation store has no check {name!r}")
    if check.status == "rejected":
        raise StateError(f"check {name!r} is already rejected")
    reason = (args.reason or "").strip()
    recorded = "revoked by the operator" + (f": {reason}" if reason else "")
    sys.stderr.write(render_revoke_preview(name, check))
    if not confirm_operator(
        "ww rules revoke",
        f"reject check {name} ({check.status}) and the rules it covers, which "
        "a verifier judges from then on",
        "Revoke it?",
        "Revoke",
        assume_yes=args.yes,
    ):
        return _Outcome("", error="revoke cancelled", exit_code=1)
    outcome: list[tuple[str, ...]] = []

    def change(automation: RuleAutomation) -> RuleAutomation:
        updated, rules = revoke_check(automation, name, recorded)
        outcome.append(rules)
        return updated

    store.modify(change)
    rules = outcome[-1]
    if args.json_output:
        return _Outcome(
            _json(
                {
                    "revoked": {
                        "check": name,
                        "rules": list(rules),
                        "reason": recorded,
                        "config": list(check.spec.config),
                    }
                }
            )
        )
    return _Outcome(render_revoked(name, rules, check.spec.config))


def _convert(context: _Context, configuration: WorkflowConfiguration) -> _Outcome:
    """Record an approved check covering the named rules, after asking."""
    args = context.args
    rules = rule_conversion.declared_rules(configuration, tuple(args.covers))
    mapping = rule_writes.check_mapping(
        args.check_shell,
        tuple(args.check_argv) if args.check_argv else None,
        tuple(args.assertion) if args.assertion else None,
    )
    assert mapping is not None  # argparse requires one of the commands
    spec = CheckSpec(
        command=parse_command(mapping, "--check"),
        config=tuple(args.config),
        proven=args.proven,
    )
    store = RuleStore(context.storage.root)
    name = args.check_name
    now = _store_time()

    def change(automation: RuleAutomation) -> rule_conversion.StoreChange:
        return rule_conversion.convert(automation, name, spec, rules, now)

    current = store.load()
    preview = render_convert_preview(name, rules, current, change(current))
    if args.dry_run:
        return _Outcome(preview + "Dry run: nothing was recorded.\n")
    sys.stderr.write(preview)
    if not confirm_operator(
        "ww rules convert",
        f"record check {name} as converted, run for the {len(rules)} rule(s) "
        "it covers wherever its config files exist",
        "Record it?",
        "Convert",
        assume_yes=args.yes,
    ):
        return _Outcome("", error="convert cancelled", exit_code=1)
    result = _modify_store(store, change)
    if args.json_output:
        return _Outcome(
            _json(
                {
                    "converted": {
                        "check": name,
                        "rules": [rule.id for rule in rules],
                        **_change_json(result),
                    }
                }
            )
        )
    text = f"Recorded check {name}, covering {', '.join(rule.id for rule in rules)}.\n"
    if result.unscriptized:
        text += (
            f"No longer covered, so not scriptized: {len(result.unscriptized)} "
            "rule wording(s) ("
            + ", ".join(key[:12] for key in result.unscriptized)
            + ").\n"
        )
    return _Outcome(text + _dropped_text(result))


def _decline(context: _Context, configuration: WorkflowConfiguration) -> _Outcome:
    """Record rules as not convertible, after asking."""
    args = context.args
    rules = rule_conversion.declared_rules(configuration, tuple(args.rule_ids))
    reason = args.reason.strip()
    if not reason:
        raise StateError("rules decline needs --reason")
    store = RuleStore(context.storage.root)

    def change(automation: RuleAutomation) -> rule_conversion.StoreChange:
        return rule_conversion.decline(automation, rules, reason)

    current = store.load()
    preview = render_decline_preview(rules, reason, current, change(current))
    if args.dry_run:
        return _Outcome(preview + "Dry run: nothing was recorded.\n")
    sys.stderr.write(preview)
    if not confirm_operator(
        "ww rules decline",
        f"record {len(rules)} rule(s) as not convertible, judged by a verifier "
        "from then on",
        "Record it?",
        "Decline",
        assume_yes=args.yes,
    ):
        return _Outcome("", error="decline cancelled", exit_code=1)
    result = _modify_store(store, change)
    if args.json_output:
        return _Outcome(
            _json(
                {
                    "declined": {
                        "rules": [rule.id for rule in rules],
                        **_change_json(result),
                    }
                }
            )
        )
    return _Outcome(
        f"Recorded {len(rules)} rule(s) as not convertible: "
        + ", ".join(rule.id for rule in rules)
        + ".\n"
        + _dropped_text(result)
    )


def _store_time() -> str:
    """Now, as the rule-automation store records its times."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _modify_store(
    store: RuleStore,
    change: Callable[[RuleAutomation], rule_conversion.StoreChange],
) -> rule_conversion.StoreChange:
    """Apply ``change`` under the store's lock and return what it did."""
    outcome: list[rule_conversion.StoreChange] = []

    def apply(automation: RuleAutomation) -> RuleAutomation:
        outcome.append(change(automation))
        return outcome[-1].automation

    store.modify(apply)
    return outcome[-1]


def _change_json(change: rule_conversion.StoreChange) -> dict[str, object]:
    return {
        "unscriptized": list(change.unscriptized),
        "moved": [{"rule": key, "from": check} for key, check in change.moved],
        "dropped_checks": list(change.dropped_checks),
        "dropped_revisions": list(change.dropped_revisions),
    }


def _dropped_text(change: rule_conversion.StoreChange) -> str:
    text = ""
    if change.dropped_checks:
        text += (
            "Removed, covering nothing more: check(s) "
            + ", ".join(change.dropped_checks)
            + ".\n"
        )
    if change.dropped_revisions:
        text += (
            "Dropped the pending revision of check(s) "
            + ", ".join(change.dropped_revisions)
            + ".\n"
        )
    return text


def _prune(context: _Context, configuration: WorkflowConfiguration) -> _Outcome:
    """Delete orphan store entries after listing them and asking the operator."""
    store = RuleStore(context.storage.root)
    automation = store.load()
    declared = declared_hashes(configuration)
    listed = orphans(automation, declared)
    if not listed:
        return _Outcome(
            _json({"pruned": {"rules": [], "checks": []}})
            if context.args.json_output
            else render_orphans(listed, automation)
        )
    sys.stderr.write(render_orphans(listed, automation))
    effect = (
        f"delete these {len(listed.rules)} rule and {len(listed.checks)} check "
        "entries from the rule-automation store"
    )
    if not confirm_operator(
        "ww rules prune",
        effect,
        "Delete them?",
        "Prune",
        assume_yes=context.args.yes,
    ):
        return _Outcome("", error="prune cancelled", exit_code=1)
    store.modify(lambda current: prune(current, listed, declared))
    if context.args.json_output:
        return _Outcome(
            _json(
                {"pruned": {"rules": list(listed.rules), "checks": list(listed.checks)}}
            )
        )
    return _Outcome(
        f"Pruned {len(listed.rules)} rule and {len(listed.checks)} check entries "
        "from the rule-automation store.\n"
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
            summary_for_next=args.summary,
            caller_role=args.role,
            rule_results=tuple(args.rule_result),
            assignment=args.assignment,
        ),
        args.json_output,
        completing=True,
    )


def _interact(context: _Context) -> _Outcome:
    args = context.args
    if args.await_operator:
        others = (
            args.operator,
            args.agent,
            args.transcript,
            args.choice,
            args.end_interaction,
        )
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
            context.task_id, caller_role=args.role, assignment=args.assignment
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
            transcript=_transcript(args.transcript),
            choice=args.choice,
            end=args.end_interaction,
            pause=args.pause,
            caller_role=args.role,
            assignment=args.assignment,
        ),
        args.json_output,
    )


def _transcript(source: str | None) -> str | None:
    """The transcript text from a file, or from stdin for ``-``."""
    if source is None:
        return None
    if source == "-":
        return sys.stdin.read()
    try:
        return Path(source).read_text(encoding="utf-8")
    except OSError as error:
        raise StateError(f"cannot read the transcript {source}: {error}") from error


def _feedback(context: _Context) -> _Outcome:
    args = context.args
    store = context.service.feedback
    if args.feedback_action == "list":
        result = store.listing()
        result["enabled"] = load_project_config(
            context.storage.project_config_path
        ).feedback_learning
    elif args.feedback_action == "get":
        if not args.task_id:
            raise StateError("feedback get needs a point ID")
        result = store.get(args.task_id)
    elif args.feedback_action in {"sources", "show"}:
        if not args.task_id:
            raise StateError("feedback sources needs a task ID")
        result = context.service.feedback_sources(args.task_id, args.run_id)
    elif args.feedback_action == "prune":
        result = store.prune(dry_run=args.dry_run, keep=tuple(args.keep))
    else:
        if not args.task_id or not args.analysis:
            raise StateError("feedback record needs a task ID and --analysis PATH")
        try:
            analysis = json.loads(_transcript(args.analysis) or "")
        except json.JSONDecodeError as error:
            raise StateError(f"invalid feedback analysis: {error}") from error
        result = context.service.record_feedback(
            args.task_id,
            analysis,
            run_id=args.run_id,
            caller_role=args.role,
            assignment=args.assignment,
        )
    if args.json_output:
        return _Outcome(_json(result))
    return _Outcome("# Operator feedback candidates\n\n" + _json(result))


def _interactions(context: _Context) -> _Outcome:
    text = context.service.interactions_text(context.task_id)
    return _Outcome(text if text.endswith("\n") or not text else text + "\n")


def _fail(context: _Context) -> _Outcome:
    args = context.args
    return _instruction_outcome(
        context.service.fail(
            context.task_id,
            args.error,
            caller_role=args.role,
            assignment=args.assignment,
        ),
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
                context.task_id,
                args.run_id,
                caller_role=args.role,
                assignment=args.assignment,
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
    since = (
        context.extensions.config.agent_hooks.recent_days
        if args.since is None
        else args.since
    )
    entries = (
        context.service.interruptions()
        if args.all
        else context.service.hook_records.recent(since)
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
        window = "" if args.all else f" in the last {since} day(s)"
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


def _onboarding(context: _Context) -> _Outcome:
    """Show the onboarding keys, after recording any ``--set`` ones."""
    onboarding = Onboarding(context.storage.root, context.storage.project_metadata)
    args = context.args
    state = onboarding.set(args.assignments) if args.assignments else onboarding.read()
    if args.json_output:
        return _Outcome(_json(state.to_dict()))
    return _Outcome(render_onboarding(state, context.storage.root))


def _setup(context: _Context) -> _Outcome:
    """Show a setup fragment's placement, ask, and write it validated."""
    args = context.args
    root = context.storage.root
    config_path = context.storage.config_path
    plan = setup_apply.plan_setup(root, config_path, args.fragment, args.audience)
    # Only a change that would load is shown to the operator.
    setup_apply.validate_setup(root, config_path, plan)
    summary = setup_apply.render_plan(plan, root)
    if args.dry_run:
        if args.json_output:
            return _Outcome(_json(setup_apply.plan_to_dict(plan, root, applied=False)))
        return _Outcome(
            summary + "Dry run: the configuration would be valid; nothing was "
            "written.\n"
        )
    sys.stderr.write(summary)
    if not confirm_operator(
        "ww setup apply",
        f"write these {len(plan.writes)} file(s) for "
        + ("the team" if plan.audience == "team" else "you only"),
        "Apply it?",
        "Setup",
        assume_yes=args.yes,
    ):
        return _Outcome("", error="setup apply cancelled", exit_code=1)
    setup_apply.apply_setup(plan)
    if args.json_output:
        return _Outcome(_json(setup_apply.plan_to_dict(plan, root, applied=True)))
    return _Outcome("Applied.\n" + summary)


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
            args.text,
            reference_to_id=args.refers_to,
            fields=_named_values(args.field, "--field"),
        ),
    )
    return _Outcome(_json(item.to_dict()))


def _update_item(context: _Context) -> _Outcome:
    args = context.args
    changes = {
        name: value
        for name, value in (
            ("item", args.text),
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
        context.task_id,
        args.id,
        args.text,
        project=args.project,
        fields=_named_values(args.field, "--field"),
    )
    return _Outcome(_json(child.to_dict()))


def _update_child(context: _Context) -> _Outcome:
    args = context.args
    child = context.service.update_child(
        context.task_id,
        args.child_id,
        text=args.text,
        project=args.project,
        fields=_named_values(args.field, "--field"),
    )
    return _Outcome(_json(child.to_dict()))


def _start_child(context: _Context) -> _Outcome:
    args = context.args
    return _instruction_outcome(
        context.service.start_child(
            args.parent_task_id,
            args.child_id,
            workflow_name=args.workflow_name,
            workflow_runtime=args.workflow_runtime,
            model=args.model,
            reasoning=args.reasoning,
        ),
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


def _inspect(context: _Context) -> _Outcome:
    profile = inspect_checkout(context.storage.root, context.args.commits)
    if context.args.json_output:
        return _Outcome(json.dumps(profile.to_dict(), indent=2) + "\n")
    return _Outcome(render_markdown(profile))


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
    "inspect": _inspect,
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
    "check": _check,
    "dispute": _dispute,
    "rule": _rule,
    "rules": _rules,
    "interact": _interact,
    "interactions": _interactions,
    "feedback": _feedback,
    "fail": _fail,
    "status": _status,
    "instruction": _instruction,
    "metadata": _metadata,
    "documents": _documents,
    "onboarding": _onboarding,
    "setup": _setup,
    "items": _items,
    "item": _item,
    "artifacts": _artifacts,
    "add-item": _add_item,
    "update-item": _update_item,
    "remove-item": _remove_item,
    "add-child": _add_child,
    "update-child": _update_child,
    "start-child": _start_child,
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
            print("ww error: next --force requires --reason", file=sys.stderr)
            return 1
        if args.retry and not confirm_interrupted_retry(assume_yes=args.yes):
            return 1
        if args.retry:
            args.confirmation = _confirmation(args.yes)
        if args.yes and not (args.retry or args.force or args.replan):
            print(
                "ww error: --yes confirms next --retry, --force or --replan",
                file=sys.stderr,
            )
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
    logged = args.command not in _READ_ONLY_COMMANDS and not (
        (args.command == "rules" and args.rules_action is None)
        or (
            args.command == "feedback"
            and (args.feedback_action not in {"record", "prune"} or args.dry_run)
        )
        or (args.command == "onboarding" and not args.assignments)
    )

    def log(outcome: str, error: str | None, *scope: str | None) -> None:
        record_workflow, record_task = scope if scope else (workflow, task_id)
        storage.append_log(
            _log_record(
                args.command, record_workflow, record_task, outcome, error, args
            )
        )

    try:
        extensions = ExtensionRegistry.discover(storage.root)
        service = WorkflowService(storage, extensions=extensions)
        # Check what the force would do before asking the operator to approve
        # it, so a refused force is reported instead of confirmed and refused.
        if args.command == "next" and args.force:
            effect = service.force_target(args.task_id)
            if not _confirm_force_next(effect, assume_yes=args.yes):
                return 1
            args.confirmation = _confirmation(args.yes)
        # A replan that rewinds runs finished steps again: the operator agrees
        # to that, knowing which ones, before anything changes.
        if args.command == "next" and args.replan:
            change = service.plan_change(args.task_id)
            if change is not None and change.refusal is None and change.reruns:
                if not confirm_operator(
                    "ww next --replan",
                    "run these finished steps again under the new definition: "
                    + ", ".join(change.reruns),
                    "Replan and rerun them?",
                    "Replan",
                    assume_yes=args.yes,
                ):
                    return 1
                args.confirmation = _confirmation(args.yes)
        if logged:
            log("started", None)
        # Every command this invocation prints starts with the project's ww.
        with printed_executable(extensions.config.executable):
            result = _HANDLERS[args.command](
                _Context(args, storage, extensions, service)
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
                storage,
                hook_agent(args.agent),
                args.hook_action,
                payload,
                on_request=config.on_request,
                settings=config.agent_hooks,
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
        if (candidate / WORKFLOWS_FILE).is_file() or (candidate / ".ww").exists():
            return candidate
    return current
