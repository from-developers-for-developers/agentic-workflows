# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only views of rules for the ``check``, ``rule`` and ``rules`` commands.

A task's rules are read from its frozen plan, never from the configuration as
it reads now, so ``ww rule`` shows the wording the task was given. The
project's rules as declared, for ``ww rules``, come from the composed
configuration; the rule-automation store adds what ww learned about them.
Nothing here changes a task, the store, or a file; ``prune`` returns the
store with its orphans removed for the caller to save.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ww.actions import actions
from ww.config.rules import rule_source
from ww.errors import StateError
from ww.execution_models import CheckReport, ExecutionState, PlanItemExecution
from ww.extensions import is_extension_reference
from ww.instructions.builder import checks_unavailable, fix_failures
from ww.instructions.models import CheckPreview
from ww.plan import PlanItem, PlannedCheck, PlannedRule, WorkflowPlan
from ww.rule_conversion import ScriptizeState, every_rule, scriptize_state
from ww.rule_disputes import DisputeEntry
from ww.rule_stats import STATS_FILE, RuleStats
from ww.rule_store import RuleAutomation, describe_command
from ww.rule_verification import judged_now
from ww.workflow_config import (
    INIT_STEP_NAME,
    HandlerDefinition,
    NameFilter,
    RuleDefinition,
    RuleGroupRef,
    StepDefinition,
    WorkflowConfiguration,
    every_step,
    step_paths,
)


def check_preview(
    state: ExecutionState, item: PlanItem, report: CheckReport
) -> CheckPreview:
    """What ``ww check`` shows for a report it ran and did not record."""
    record = state.item_executions[state.cursor]
    return CheckPreview(
        task_id=state.task_id,
        step=item.name,
        checks=len(report.results),
        failures=fix_failures(item, record, report.failed),
        passed=tuple(
            result.id for result in report.results if result.status == "passed"
        ),
        not_applicable=tuple(
            result.id for result in report.results if result.status == "not_applicable"
        ),
        unavailable=checks_unavailable(report),
        judged=tuple(
            rule.id
            for rule in judged_now(
                item, record, {result.id for result in report.unavailable}
            )
        ),
        waived=record.checks_waived,
        all_files=report.all_files,
    )


@dataclass(frozen=True)
class RuleView:
    """One rule or check of a task, in full, for ``ww rule``.

    ``kind`` is ``rule`` (a rule with or without a command of its own),
    ``hook`` (a ``before_complete`` hook with ``on_failure: fix``) or
    ``derived`` (an approved store check the step resolved). ``steps`` are
    the task's steps that carry it. For a rule without a command,
    ``store_status``, ``store_check`` and ``interpretation`` are what the
    rule-automation store knows about its wording, and ``resolution`` how
    the current step enforces it.
    """

    id: str
    kind: str
    summary: str
    text: str
    steps: tuple[str, ...]
    paths: tuple[str, ...] = ()
    contains_in_file: tuple[str, ...] = ()
    contains_in_diff: tuple[str, ...] = ()
    source: str | None = None
    command: str | None = None
    assertion: str | None = None
    max_fixes: int | None = None
    text_hash: str | None = None
    store_status: str | None = None
    store_check: str | None = None
    interpretation: str | None = None
    resolution: str | None = None
    covers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "summary": self.summary,
            "text": self.text,
            "steps": list(self.steps),
            "paths": list(self.paths),
            "contains_in_file": list(self.contains_in_file),
            "contains_in_diff": list(self.contains_in_diff),
            "source": self.source,
            "command": self.command,
            "assert": self.assertion,
            "max_fixes": self.max_fixes,
            "text_hash": self.text_hash,
            "store_status": self.store_status,
            "store_check": self.store_check,
            "interpretation": self.interpretation,
            "resolution": self.resolution,
            "covers": list(self.covers),
        }


def rule_view(
    state: ExecutionState,
    plan: WorkflowPlan,
    automation: RuleAutomation,
    rule_id: str,
) -> RuleView:
    """Find a rule or check of the task by ID, the current step first."""
    order = sorted(range(len(plan.items)), key=lambda index: index != state.cursor)
    steps = tuple(
        dict.fromkeys(
            plan.items[index].name
            for index in order
            if any(rule.id == rule_id for rule in plan.items[index].rules)
            or any(check.id == rule_id for check in plan.items[index].checks)
        )
    )
    for index in order:
        item = plan.items[index]
        record = state.item_executions[index]
        rule = next((rule for rule in item.rules if rule.id == rule_id), None)
        if rule is not None:
            return _rule(
                item, record, automation, rule, steps, current=index == state.cursor
            )
        check = next(
            (
                check
                for check in (*item.checks, *record.resolved_checks)
                if check.id == rule_id
            ),
            None,
        )
        if check is not None:
            return _check(check, steps or (item.name,))
    raise StateError(
        f"task {state.task_id!r} has no rule or check {rule_id!r}; the step "
        "page and the fix page name them"
    )


def _rule(
    item: PlanItem,
    record: PlanItemExecution,
    automation: RuleAutomation,
    rule: PlannedRule,
    steps: tuple[str, ...],
    *,
    current: bool,
) -> RuleView:
    own = next((check for check in item.checks if check.id == rule.id), None)
    entry = None if rule.has_command else automation.rules.get(rule.text_hash)
    resolution = next(
        (entry for entry in record.rule_resolutions if entry.id == rule.id), None
    )
    derived = next(
        (check for check in record.resolved_checks if rule.id in check.covers), None
    )
    command = own or derived
    return RuleView(
        id=rule.id,
        kind="rule",
        summary=rule.summary,
        text=rule.text,
        steps=steps,
        paths=rule.paths,
        contains_in_file=rule.contains_in_file,
        contains_in_diff=rule.contains_in_diff,
        source=rule.source,
        command=describe_command(command.command) if command else None,
        assertion=(
            command.command.assertion.describe()
            if command and command.command.assertion
            else None
        ),
        max_fixes=rule.max_fixes,
        text_hash=rule.text_hash,
        store_status=entry.status if entry else None,
        store_check=entry.check if entry else None,
        interpretation=entry.interpretation if entry else None,
        resolution=resolution.status if resolution and current else None,
    )


def _check(check: PlannedCheck, steps: tuple[str, ...]) -> RuleView:
    return RuleView(
        id=check.id,
        kind=check.source,
        summary=check.summary,
        text=check.summary,
        steps=steps,
        paths=check.paths,
        contains_in_file=check.contains_in_file,
        contains_in_diff=check.contains_in_diff,
        command=describe_command(check.command),
        assertion=(
            check.command.assertion.describe() if check.command.assertion else None
        ),
        max_fixes=check.max_fixes,
        covers=check.covers,
    )


@dataclass(frozen=True)
class ListedRule:
    """One declared rule: its ID, first sentence, globs, and file.

    ``store_check`` names the approved rule-automation check that runs for a
    rule without a command of its own, the one ``rules promote`` copies into
    its file. ``scriptize`` says where the rule stands: ``command``,
    ``converted``, ``not_convertible``, ``rejected`` or ``unscriptized``.
    ``stats`` are the checkout's local counters for the rule.
    """

    id: str
    summary: str
    paths: tuple[str, ...] = ()
    contains_in_file: tuple[str, ...] = ()
    contains_in_diff: tuple[str, ...] = ()
    has_check: bool = False
    source: str | None = None
    disputes: int = 0
    store_check: str | None = None
    scriptize: ScriptizeState = "unscriptized"
    stats: RuleStats = RuleStats()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "summary": self.summary,
            "paths": list(self.paths),
            "contains_in_file": list(self.contains_in_file),
            "contains_in_diff": list(self.contains_in_diff),
            "has_check": self.has_check,
            "source": self.source,
            "disputes": self.disputes,
            "store_check": self.store_check,
            "scriptize": self.scriptize,
            "stats": self.stats.to_dict(),
        }


@dataclass(frozen=True)
class ListedGroup:
    """One root rule group: where it applies on its own, and its rules.

    ``workflows`` and ``steps`` admit every workflow or step, render as
    ``"*"``, or list names; an empty one means the group applies only where a
    step names it.
    """

    name: str
    origin: str
    workflows: NameFilter
    steps: NameFilter
    hints: dict[str, str]
    rules: tuple[ListedRule, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "origin": self.origin,
            "workflows": self.workflows.to_data(),
            "steps": self.steps.to_data(),
            "hints": self.hints,
            "rules": [rule.to_dict() for rule in self.rules],
        }


@dataclass(frozen=True)
class ListedStep:
    """One step's own ``rules`` list: its rules, and the groups it names."""

    step: str
    rules: tuple[ListedRule, ...]
    groups: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "rules": [rule.to_dict() for rule in self.rules],
            "groups": list(self.groups),
        }


@dataclass(frozen=True)
class ListedTarget:
    """One step of a workflow's flattened tree, as a step filter can name it.

    ``path`` is the step's logical path, the precise selector of a group's
    ``steps`` filter; ``step`` is its leaf name, which a filter also admits
    wherever the tree repeats it. Only an ``agent_owned`` step receives rules;
    ``groups`` are the root groups that reach it now, by their filters or
    because the step names them.
    """

    workflow: str
    step: str
    path: str
    agent_owned: bool
    groups: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow": self.workflow,
            "step": self.step,
            "path": self.path,
            "agent_owned": self.agent_owned,
            "groups": list(self.groups),
        }


@dataclass(frozen=True)
class RulesListing:
    """The project's declared rules, for ``ww rules``.

    ``targets`` are every workflow's steps in tree order, so an agent scoping
    a group names a step that exists. ``check_guidance`` is the project's
    ``rules.check_guidance``, so an agent writing a check reads it without
    opening ww's configuration files.
    """

    groups: tuple[ListedGroup, ...]
    steps: tuple[ListedStep, ...]
    targets: tuple[ListedTarget, ...] = ()
    check_guidance: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "groups": [group.to_dict() for group in self.groups],
            "steps": [step.to_dict() for step in self.steps],
            "targets": [target.to_dict() for target in self.targets],
            "check_guidance": self.check_guidance,
        }


def rules_listing(
    configuration: WorkflowConfiguration,
    root: Path,
    disputes: tuple[DisputeEntry, ...] = (),
    automation: RuleAutomation | None = None,
    stats: Mapping[str, RuleStats] | None = None,
) -> RulesListing:
    """Every root group with its filters and rules, then every step's own list.

    A step name repeated across workflows is listed once; ``disputes`` count
    how often each rule was disputed; ``automation`` names the approved
    store check of each rule without a command; ``stats`` are the local
    counters by rule ID. The targets walk every workflow's steps as the
    plan compiler does, ``init`` left out since it never takes rules.
    """
    automation = automation if automation is not None else RuleAutomation()
    stats = stats if stats is not None else {}
    counts: dict[str, int] = {}
    for dispute in disputes:
        counts[dispute.check] = counts.get(dispute.check, 0) + 1

    def listed(rule: RuleDefinition) -> ListedRule:
        return ListedRule(
            id=rule.id,
            summary=rule.summary,
            paths=rule.paths,
            contains_in_file=rule.contains_in_file,
            contains_in_diff=rule.contains_in_diff,
            has_check=rule.check is not None,
            source=rule_source(rule.source, root),
            disputes=counts.get(rule.id, 0),
            store_check=_store_check(automation, rule),
            scriptize=scriptize_state(automation, rule),
            stats=stats.get(rule.id, RuleStats()),
        )

    groups = tuple(
        ListedGroup(
            name=group.name,
            origin=group.origin,
            workflows=group.workflows,
            steps=group.steps,
            hints=group.hints.to_dict(),
            rules=tuple(listed(rule) for rule in group.rules),
        )
        for group in configuration.rule_groups
    )
    steps: dict[str, ListedStep] = {}
    for step in every_step(configuration):
        if not step.rules or step.name in steps:
            continue
        steps[step.name] = ListedStep(
            step=step.name,
            rules=tuple(
                listed(entry)
                for entry in step.rules
                if isinstance(entry, RuleDefinition)
            ),
            groups=tuple(
                entry.name for entry in step.rules if isinstance(entry, RuleGroupRef)
            ),
        )
    return RulesListing(groups, tuple(steps.values()), _targets(configuration))


def _targets(configuration: WorkflowConfiguration) -> tuple[ListedTarget, ...]:
    targets = []
    for workflow in configuration.workflows:
        walked = tuple(step_paths(workflow.steps))
        precise = frozenset(path for _, path in walked)
        for step, path in walked:
            if step.name == INIT_STEP_NAME:
                continue
            owned = _agent_owned(configuration, step)
            applying = (
                *(
                    group.name
                    for group in configuration.rule_groups
                    if group.applies_to(workflow.name, step.name, path, precise)
                ),
                *(
                    entry.name
                    for entry in step.rules
                    if isinstance(entry, RuleGroupRef)
                ),
            )
            targets.append(
                ListedTarget(
                    workflow=workflow.name,
                    step=step.name,
                    path=path,
                    agent_owned=owned,
                    groups=tuple(dict.fromkeys(applying)) if owned else (),
                )
            )
    return tuple(targets)


def _agent_owned(configuration: WorkflowConfiguration, step: StepDefinition) -> bool:
    """Whether the step has agent work of its own, as the plan compiler resolves it.

    A loop or a nested sequence only holds its steps, and a handler group
    runs automated members. Any other step performs its action, the root
    handler it names, or an implicit skill, slash command or prompt, which
    is always the agent's.
    """
    if step.loop_steps or step.child_steps:
        return False
    handler: HandlerDefinition = step
    if step.is_reference:
        if is_extension_reference(step.name):
            return False
        handler = configuration.handlers_by_name.get(step.name, step)
    if handler.handlers:
        return False
    if handler.operation is not None:
        return handler.operation.owner == "agent"
    if handler.action is not None:
        return actions.get(handler.action.identifier).owner == "agent"
    return True


@dataclass(frozen=True)
class ListedRuleStats:
    """One declared rule with its local counters, for ``ww rules stats``."""

    id: str
    summary: str
    stats: RuleStats

    @property
    def never_applied(self) -> bool:
        return self.stats.applied == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "summary": self.summary,
            "never_applied": self.never_applied,
            **self.stats.to_dict(),
        }


@dataclass(frozen=True)
class RuleStatsListing:
    """Every declared rule, the most failing first."""

    rules: tuple[ListedRuleStats, ...]

    def to_dict(self) -> dict[str, object]:
        return {"path": STATS_FILE, "rules": [rule.to_dict() for rule in self.rules]}


def rule_stats_listing(
    configuration: WorkflowConfiguration, stats: Mapping[str, RuleStats]
) -> RuleStatsListing:
    """Each declared rule once with its counters: most failures first, then
    most applied, then by ID; a rule never evaluated has zero counters."""
    declared: dict[str, RuleDefinition] = {}
    for rule in every_rule(configuration):
        declared.setdefault(rule.id, rule)
    listed = [
        ListedRuleStats(rule.id, rule.summary, stats.get(rule.id, RuleStats()))
        for rule in declared.values()
    ]
    listed.sort(
        key=lambda entry: (-entry.stats.failures, -entry.stats.applied, entry.id)
    )
    return RuleStatsListing(tuple(listed))


def _store_check(automation: RuleAutomation, rule: RuleDefinition) -> str | None:
    """The approved store check that runs for a rule without its own command."""
    if rule.check is not None:
        return None
    converted = automation.converted_check(rule.text_hash)
    return converted[0] if converted is not None else None


def declared_hashes(configuration: WorkflowConfiguration) -> frozenset[str]:
    """The wording hash of every rule the composed configuration declares."""
    return frozenset(
        {rule.text_hash for group in configuration.rule_groups for rule in group.rules}
        | {
            entry.text_hash
            for step in every_step(configuration)
            for entry in step.rules
            if isinstance(entry, RuleDefinition)
        }
    )


@dataclass(frozen=True)
class Orphans:
    """Store entries no declared rule needs any more.

    ``rules`` are rule hashes no rule has the wording of; ``checks`` are
    checks that cover only such rules and that no remaining rule names.
    """

    rules: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.rules or self.checks)


def orphans(automation: RuleAutomation, declared: frozenset[str]) -> Orphans:
    rules = tuple(key for key in automation.rules if key not in declared)
    gone = set(rules)
    named = {
        entry.check
        for key, entry in automation.rules.items()
        if key not in gone and entry.check is not None
    }
    checks = tuple(
        name
        for name, check in automation.checks.items()
        if name not in named
        and all(
            key in gone
            for key in (
                *check.spec.covers,
                *(check.pending.covers if check.pending is not None else ()),
            )
        )
    )
    return Orphans(rules, checks)


def prune(
    automation: RuleAutomation, listed: Orphans, declared: frozenset[str]
) -> RuleAutomation:
    """The store without the listed entries that are still orphans.

    An entry that stopped being an orphan since it was listed, because its
    wording was declared again meanwhile, is kept; one that became an orphan
    since is not removed unasked.
    """
    current = orphans(automation, declared)
    rules = set(listed.rules) & set(current.rules)
    checks = set(listed.checks) & set(current.checks)
    return replace(
        automation,
        rules={
            key: entry for key, entry in automation.rules.items() if key not in rules
        },
        checks={
            name: check
            for name, check in automation.checks.items()
            if name not in checks
        },
    )
