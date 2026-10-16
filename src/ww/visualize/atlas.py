# SPDX-License-Identifier: GPL-3.0-or-later
"""The Workflows tab: each workflow as ww compiles it, shaped for the page.

The step tree is read from the compiled plan, so a step shows what ww will
actually run: inherited steps, resolved handlers, hooks, rules and checks,
roles, models and gotos. Each plan item names its container (``parent``,
``assessment_parent``, ``child_stage``), so nothing is inferred from a step
path. The definitions add only what a plan does not keep, such as whether an
assessment is a step of its own or decides after a step's work.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ww.actions.contracts import Commands, Extension, PlannedAction, Prompt
from ww.builtin_workflows import builtin_workflow_names
from ww.config.composition import configuration_files
from ww.config.rules import rule_source
from ww.config_files import display_path
from ww.errors import WwError
from ww.operations import ChildWorkflowRun, WorkflowHandoff
from ww.plan import PlanItem, PlannedCheck, WorkflowPlan, compile_for_any_agent
from ww.project_config import compose_settings
from ww.rule_disputes import DisputeLog
from ww.rule_stats import RuleStatsStore
from ww.rule_store import RuleStore, describe_command
from ww.rule_views import rules_listing
from ww.workflow_config import (
    HandlerDefinition,
    StepDefinition,
    WorkflowConfiguration,
    WorkflowDefinition,
)

from .project import Project

JSON = dict[str, Any]

_LANES = {
    "before_start_workflow": "before_start",
    "before_complete_workflow": "before_complete",
}
_STEP_HOOK_PHASE = {
    "before_start": "before start",
    "before_complete": "before complete",
    "after_complete": "after complete",
}
_SAVE_PREFIX = {"task": "metadata", "project": "project_metadata"}


def build_atlas(project: Project) -> JSON:
    """Every workflow and the library beside them, or why there are none."""
    storage = project.storage
    files = _configuration_files(project)
    if project.configuration is None or project.extensions is None:
        return {"error": project.error, "files": files, "workflows": [], "library": {}}
    configuration = project.configuration
    listing: JSON = rules_listing(
        configuration,
        storage.config_root,
        DisputeLog(storage.root).load(),
        RuleStore(storage.config_root).load(),
        RuleStatsStore(storage).load(),
    ).to_dict()
    group_of = {
        rule["id"]: group["name"]
        for group in listing["groups"]
        for rule in group["rules"]
    }
    workflows: list[JSON] = []
    plans: dict[str, WorkflowPlan] = {}
    for workflow in configuration.workflows:
        try:
            plan = compile_for_any_agent(
                configuration,
                storage.root,
                workflow.name,
                project.extensions,
                project.extensions.config,
            )
        except (WwError, ValueError) as error:
            workflows.append(
                {**_workflow_header(configuration, workflow), "error": str(error)}
            )
            continue
        plans[workflow.name] = plan
        workflows.append(_Workflow(project, workflow, plan, group_of).build())
    return {
        "error": None,
        "files": files,
        "workflows": workflows,
        "library": _library(project, configuration, plans, listing),
    }


def _configuration_files(project: Project) -> list[str]:
    storage = project.storage
    try:
        yaml_files = configuration_files(storage.config_path)
        _, settings = compose_settings(storage.project_config_path)
    except (WwError, OSError, ValueError):
        return []
    return [display_path(path, storage.root) for path in (*yaml_files, *settings)]


def _workflow_header(
    configuration: WorkflowConfiguration, workflow: WorkflowDefinition
) -> JSON:
    provenance = configuration.workflow_provenance.get(workflow.name)
    return {
        "name": workflow.name,
        "description": workflow.description,
        "inherits": workflow.inherits,
        "hooks_from": workflow.hooks_from,
        "runtime": workflow.runtime,
        "manual": workflow.manual,
        "restartable": workflow.restartable,
        "modes": list(workflow.modes),
        "recommended_next": workflow.recommended_next_workflow,
        "source": provenance.source if provenance else None,
        "level": provenance.level if provenance else None,
        # ww's own: shipped with ww, and no configuration file defines it.
        "builtin": provenance is None and workflow.name in builtin_workflow_names(),
    }


@dataclass
class _Workflow:
    project: Project
    definition: WorkflowDefinition
    plan: WorkflowPlan
    group_of: dict[str, str]
    nodes: dict[str, JSON] = field(default_factory=dict)
    roots: list[JSON] = field(default_factory=list)
    lanes: dict[str, list[JSON]] = field(
        default_factory=lambda: {"before_start": [], "before_complete": []}
    )
    # The steps a rule selection chooses for, by the selecting step's path.
    selections: dict[str, list[str]] = field(default_factory=dict)

    def build(self) -> JSON:
        assert self.project.configuration is not None
        definitions = dict(_definitions(self.definition.steps))
        by_id = {item.id: item for item in self.plan.items}
        for item in self.plan.items:
            if item.verifies is not None or item.repair_of or item.verification_hook:
                continue
            if item.phase in _LANES:
                self.lanes[_LANES[item.phase]].append(_hook(item))
            elif item.phase != "step":
                self._step_hook(item, by_id)
            elif item.step != "init":
                self._step(item, definitions)
        self._place_gotos()
        for chooser, chosen in self.selections.items():
            for path in chosen:
                if path in self.nodes:
                    self.nodes[path]["rules_chosen_by"] = _logical(chooser)
        return {
            **_workflow_header(self.project.configuration, self.definition),
            "handoff": self.plan.handoff,
            "lanes": self.lanes,
            "steps": self.roots,
            "error": None,
        }

    def _step(self, item: PlanItem, definitions: dict[str, StepDefinition]) -> None:
        existing = self.nodes.get(item.step)
        if existing is not None:
            # A handler group compiles to one item per member, all at its path.
            existing["members"].append(_member(item))
            return
        node = _node(item, self.group_of, definitions.get(_logical(item.step)))
        self.nodes[item.step] = node
        self._container(item).append(node)

    def _container(self, item: PlanItem) -> list[JSON]:
        parent = item.parent
        owner_path = item.assessment_parent
        if owner_path is not None and item.assessment_outcome is not None:
            label = item.assessment_outcome
            if parent in (f"{owner_path}/{label}", owner_path):
                owner = self.nodes[owner_path]
                outcome_steps: list[JSON] = _outcome(owner, label)["steps"]
                return outcome_steps
        if parent is None:
            return self.roots
        if item.child_stage is not None and parent == f"{item.child_stage}/{{child}}":
            children: list[JSON] = self.nodes[item.child_stage]["children"]["steps"]
            return children
        owner = self.nodes.get(parent) or self._group(parent, item.ancestors)
        if owner.get("items") is not None:
            items: list[JSON] = owner["items"]["steps"]
            return items
        steps: list[JSON] = owner.setdefault("steps", [])
        return steps

    def _group(self, path: str, ancestors: tuple[str, ...]) -> JSON:
        """A plain group of nested steps: it has no plan item of its own."""
        above = ancestors[: ancestors.index(path)] if path in ancestors else ()
        node: JSON = {
            "name": path.rsplit("/", 1)[-1],
            "path": _logical(path),
            "kind": "group",
            "steps": [],
            "members": [],
            "hooks": [],
            "rules": [],
        }
        self.nodes[path] = node
        if not above:
            self.roots.append(node)
        else:
            parent = self.nodes.get(above[-1]) or self._group(above[-1], above)
            parent.setdefault("steps", []).append(node)
        return node

    def _step_hook(self, item: PlanItem, by_id: dict[str, PlanItem]) -> None:
        node = self.nodes.get(item.step)
        if node is None or item.item_operation == "complete_collection":
            return
        if item.selects_rules_for:
            chosen = [by_id[target].step for target in item.selects_rules_for]
            node["selects_rules_for"] = [_logical(path) for path in chosen]
            # The steps chosen for come later in the plan than the selection.
            self.selections[item.step] = chosen
            return
        node["hooks"].append(
            {**_hook(item), "phase": _STEP_HOOK_PHASE.get(item.phase, item.phase)}
        )

    def _place_gotos(self) -> None:
        order = {item.step: item.position for item in self.plan.items}
        top = [node["path"] for node in self.roots]
        for goto in self.plan.gotos:
            source = self.nodes.get(goto.source)
            if source is None or goto.outcome is None or "assessment" not in source:
                continue
            back = order.get(goto.target, 0) <= order.get(goto.source, 0)
            skipped = 0
            if not back and goto.source in top and goto.target in top:
                skipped = top.index(goto.target) - top.index(goto.source) - 1
            _outcome(source, goto.outcome)["goto"] = {
                "target": _logical(goto.target),
                "detail": goto.detail,
                "direction": "back" if back else "forward",
                "skipped": skipped,
            }


def _definitions(
    steps: Iterable[StepDefinition], parent: str | None = None
) -> Iterable[tuple[str, StepDefinition]]:
    """Every authored step by its logical path, assessment outcomes included."""
    for step in steps:
        path = f"{parent}/{step.name}" if parent else step.name
        yield path, step
        yield from _definitions(step.child_steps, path)
        if step.items is not None:
            yield from _definitions(step.items.steps, path)
        if step.children is not None:
            yield from _definitions(step.children.steps, path)
        for outcome in step.assessment_outcomes:
            yield from _definitions(outcome.child_steps, f"{path}/{outcome.name}")


def _logical(path: str) -> str:
    return path.replace("/{child}", "").replace("/{item}", "")


def _outcome(node: JSON, label: str) -> JSON:
    outcomes: list[JSON] = node["assessment"]["outcomes"]
    found = next((entry for entry in outcomes if entry["label"] == label), None)
    if found is None:
        found = {"label": label, "steps": [], "stop": False, "goto": None}
        outcomes.append(found)
    return found


def _kind(item: PlanItem, standalone: bool) -> str:
    operation = item.operation
    if isinstance(operation, ChildWorkflowRun):
        return "workflow"
    if isinstance(operation, WorkflowHandoff):
        return "handoff"
    if item.item_operation == "collect":
        return "items"
    if item.child_operation == "collect":
        return "children"
    if item.assessment_question and standalone:
        return "assess"
    if item.owner == "ww":
        return "auto"
    return "manager" if item.role == "manager" else "worker"


def _node(
    item: PlanItem, group_of: dict[str, str], definition: StepDefinition | None
) -> JSON:
    standalone = bool(
        item.assessment_question
        and definition is not None
        and not definition.assessment_attached
    )
    kind = _kind(item, standalone)
    node: JSON = {
        "name": item.name,
        "path": _logical(item.step),
        "kind": kind,
        "description": "" if standalone else _description(item),
        "handler": item.registered_handler,
        "model": item.model if kind in {"worker", "assess"} else None,
        "reasoning": item.reasoning if kind in {"worker", "assess"} else None,
        "profile": item.profile,
        "subagents": item.subagents,
        "explicit": item.explicit,
        "interactive": "page"
        if item.ui
        else ("conversation" if item.interactive else None),
        "choices": [choice.to_dict() for choice in item.choices],
        "variables": [value.name for value in item.provide],
        "saves": _saves(item),
        "artifact_from": item.artifact_dependency,
        "artifact": item.artifact,
        "on_failure": item.on_failure if item.on_failure != "operator" else None,
        "max_fixes": item.max_handler_fixes if item.on_failure == "fix" else None,
        "members": [_member(item)] if kind == "auto" else [],
        "hooks": [
            _check_hook(check) for check in item.checks if check.source == "hook"
        ],
        "rules": _rules(item, group_of),
    }
    operation = item.operation
    if isinstance(operation, ChildWorkflowRun):
        node["workflow"] = operation.workflow
    if isinstance(operation, WorkflowHandoff):
        node["handoff_to"] = operation.target
    if item.item_operation == "collect":
        node["items"] = {"description": item.split_instruction, "steps": []}
    if item.child_operation == "collect":
        children = definition.children if definition else None
        node["children"] = {
            "description": item.split_instruction,
            "workflow": children.workflow if children and not children.steps else None,
            "steps": [],
        }
    if item.assessment_question:
        labels = (
            [entry.name for entry in definition.assessment_outcomes]
            if definition
            else []
        )
        node["assessment"] = {
            "question": item.assessment_question,
            "attached": not standalone,
            "repeat_until": item.repeat_until,
            "outcomes": [
                {
                    "label": label,
                    "steps": [],
                    "stop": label in item.assessment_stops,
                    "goto": None,
                }
                for label in dict.fromkeys(
                    (*labels, *item.assessment_outcomes, *item.assessment_stops)
                )
            ],
        }
    return node


def _description(item: PlanItem) -> str:
    operation = item.operation
    if isinstance(operation, PlannedAction) and isinstance(operation.payload, Prompt):
        return operation.payload.text
    return item.description


def _saves(item: PlanItem) -> list[str]:
    return [
        *(
            f"{_SAVE_PREFIX.get(entry.scope, entry.scope)}.{entry.key}"
            for entry in item.save_metadata
        ),
        *(f"documents.{entry.name}" for entry in item.update_document),
        *(f"item.field.{entry.name}" for entry in item.update_item),
    ]


def _member(item: PlanItem) -> JSON:
    """One command or extension call ww runs for an automatic step."""
    return {"name": item.name, "command": _command(item), "extension": _extension(item)}


def _command(item: PlanItem) -> str | None:
    operation = item.operation
    if isinstance(operation, PlannedAction) and isinstance(operation.payload, Commands):
        return _commands_text(operation.payload)
    return None


def _commands_text(commands: Commands) -> str:
    text = " && ".join(
        describe_command(Commands((command,))) for command in commands.commands
    )
    if commands.assertion is not None:
        text += f"  ({commands.assertion.describe()})"
    return text


def _extension(item: PlanItem) -> str | None:
    operation = item.operation
    if isinstance(operation, PlannedAction) and isinstance(
        operation.payload, Extension
    ):
        return operation.payload.reference
    return None


def _hook(item: PlanItem) -> JSON:
    return {
        "name": item.name,
        "owner": item.owner,
        "command": _command(item),
        "extension": _extension(item),
        "builtin": item.source == "internal",
        "description": _description(item) if item.owner == "agent" else None,
    }


def _check_hook(check: PlannedCheck) -> JSON:
    """A ``before_complete`` hook with ``on_failure: fix``: one of the checks."""
    return {
        "name": check.id,
        "phase": "before complete",
        "owner": "ww",
        "command": _commands_text(check.command),
        "on_failure": "fix",
        "builtin": False,
    }


def _rules(item: PlanItem, group_of: dict[str, str]) -> list[JSON]:
    commands = {check.id: check for check in item.checks if check.source == "rule"}
    rules = []
    for rule in item.rules:
        check = commands.get(rule.id)
        rules.append(
            {
                "id": rule.id,
                "summary": rule.summary,
                "text": rule.text,
                "paths": list(rule.paths),
                "contains_in_file": list(rule.contains_in_file),
                "contains_in_diff": list(rule.contains_in_diff),
                "command": _commands_text(check.command) if check else None,
                "max_fixes": rule.max_fixes if check else None,
                "verifier": rule.hints.to_dict(),
                "group": group_of.get(rule.id),
                "source": rule.source,
            }
        )
    return rules


def _library(
    project: Project,
    configuration: WorkflowConfiguration,
    plans: dict[str, WorkflowPlan],
    listing: JSON,
) -> JSON:
    assert project.extensions is not None
    settings = project.extensions.config
    known = {entry.name for entry in configuration.handlers}
    used: dict[str, list[str]] = {}
    extensions: dict[str, str] = {}
    for name, plan in plans.items():
        for item in plan.items:
            handler = item.registered_handler or (
                item.name if item.name in known or item.phase in _LANES else None
            )
            if handler:
                used.setdefault(handler, [])
                if name not in used[handler]:
                    used[handler].append(name)
            reference = _extension(item)
            if reference:
                extensions.setdefault(item.name, reference)
    handlers = [
        _handler(entry, used.get(entry.name, [])) for entry in configuration.handlers
    ]
    handlers.extend(
        {
            "name": name,
            "kind": "extension",
            "extension": reference,
            "description": "",
            "used_by": used.get(name, []),
        }
        for name, reference in extensions.items()
        if name not in known
    )
    try:
        files = {
            entry["name"]: entry for entry in project.service().documents_listing(None)
        }
    except (WwError, OSError, ValueError):
        files = {}
    root = project.storage.config_root
    for group in listing["groups"]:
        for rule in group["rules"]:
            rule["source"] = rule_source(rule.get("source"), root)
    return {
        "handlers": handlers,
        "rules": listing,
        "documents": [
            {
                "name": doc.name,
                "description": doc.description,
                "scope": doc.scope,
                "path": doc.path,
                # Task documents live with each task; the Tasks tab opens them.
                "exists": bool(files.get(doc.name, {}).get("exists")),
                "last_update": files.get(doc.name, {}).get("last_update"),
            }
            for doc in configuration.documents
        ],
        "modes": [
            {
                "name": mode.name,
                "description": " ".join(mode.description),
                "automatic": mode.workflows is not None or mode.steps is not None,
            }
            for mode in configuration.modes
        ],
        "profiles": [
            {"name": profile.name, "description": profile.description or ""}
            for profile in configuration.profiles
        ],
        "settings": {
            "runtime": settings.runtime,
            "task_format": settings.task_format,
            "config_ref": settings.config_ref,
            "limits": {
                "fixes": settings.limits.fixes,
                "auto_retries": settings.limits.auto_retries,
                "gotos": settings.limits.gotos,
                "assessments": settings.limits.assessments,
            },
            "extensions": settings.extensions,
        },
    }


def _handler(handler: HandlerDefinition, used_by: list[str]) -> JSON:
    action = handler.action
    if handler.handlers:
        kind = "group"
    elif action is not None and isinstance(action.payload, Commands):
        kind = "command"
    else:
        kind = "agent"
    return {
        "name": handler.name,
        "kind": kind,
        "description": handler.description,
        "command": _commands_text(action.payload)
        if action is not None and isinstance(action.payload, Commands)
        else None,
        "members": [member.name for member in handler.handlers],
        "model": handler.model,
        "reasoning": handler.reasoning,
        "used_by": used_by,
    }
