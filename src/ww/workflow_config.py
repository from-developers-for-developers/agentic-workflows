# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalized workflow-definition domain models.

These models describe configuration, not task progress. A later execution layer
consumes a compiled plan rather than independently deciding which hooks apply.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from ww.actions import Commands, DefinedAction
    from ww.operations import ChildWorkflowRun, WorkflowHandoff

from ww.contracts import (
    HookFailure,
    HookPhase,
    HookScope,
    ItemAssignment,
    ItemOperation,
    LoopAssignment,
)
from ww.workspace import Workdir

MetadataScope = Literal["task", "project"]

INIT_STEP_NAME = "init"
INIT_STEP_PROMPT = (
    "Record the passed requirements for this task. Correct their grammar and "
    "style while preserving their meaning. Do not analyze, reason about, or "
    "plan the work; only save the requirements."
)


class ConfigurationLoader(Protocol):
    """A notation frontend that emits normalized workflow definitions."""

    def __call__(self) -> WorkflowConfiguration:
        """Load definitions without assigning execution semantics."""
        ...


@dataclass(frozen=True)
class ProvidedVariable:
    """An input a handler asks its executor to provide."""

    name: str
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description}


@dataclass(frozen=True)
class SavedMetadata:
    """A metadata value an agent-owned handler must preserve."""

    name: str
    key: str
    description: str = ""
    scope: MetadataScope = "task"
    # An append key holds a list: each completion may add values, none is
    # required, and repeats are dropped.
    append: bool = False

    def __post_init__(self) -> None:
        name_pattern = r"[A-Za-z_][A-Za-z0-9_.-]*"
        key_pattern = r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*"
        if not isinstance(self.name, str) or not re.fullmatch(name_pattern, self.name):
            raise ValueError(f"invalid saved metadata name: {self.name!r}")
        if not isinstance(self.key, str) or not re.fullmatch(key_pattern, self.key):
            raise ValueError(f"invalid saved metadata key: {self.key!r}")
        if not isinstance(self.description, str):
            raise ValueError("saved metadata description must be a string")
        if self.scope not in {"task", "project"}:
            raise ValueError(f"invalid saved metadata scope: {self.scope!r}")
        if type(self.append) is not bool:
            raise ValueError("saved metadata append must be a bool")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "name": self.name,
            "key": self.key,
            "description": self.description,
            "scope": self.scope,
        }
        if self.append:
            data["append"] = True
        return data


@dataclass(frozen=True)
class DocumentDefinition:
    """A root-level durable document workflows read and update across runs.

    The format is the workflow's business; ww only knows the document's
    name, scope, file, and which step last updated it.
    """

    name: str
    description: str = ""
    scope: MetadataScope = "task"
    # An explicit file, relative to the project (or, for a task document, to
    # the task's working directory when the run has one).  ``{task_id}`` is
    # replaced in a task-scoped path.  Omitted, the document lives under
    # ``.ww``.
    path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_-]*", self.name
        ):
            raise ValueError(f"invalid document name: {self.name!r}")
        if not isinstance(self.description, str):
            raise ValueError("document description must be a string")
        if self.scope not in {"task", "project"}:
            raise ValueError(f"invalid document scope: {self.scope!r}")
        if self.path is not None:
            if not isinstance(self.path, str) or not self.path.strip():
                raise ValueError("document path must be a non-empty string")
            parts = self.path.replace("\\", "/").split("/")
            if self.path.startswith(("/", "\\")) or ".." in parts:
                raise ValueError(
                    f"document path must stay inside the project: {self.path!r}"
                )
            if self.scope == "project" and "{task_id}" in self.path:
                raise ValueError("a project document path cannot use {task_id}")

    def to_dict(self) -> dict[str, str]:
        data = {"name": self.name, "description": self.description, "scope": self.scope}
        if self.path is not None:
            data["path"] = self.path
        return data


@dataclass(frozen=True)
class DocumentUpdate:
    """An agent-owned action's promise to create or update a document."""

    name: str
    instruction: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_-]*", self.name
        ):
            raise ValueError(f"invalid document update name: {self.name!r}")
        if not isinstance(self.instruction, str):
            raise ValueError("document update instruction must be a string")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "instruction": self.instruction}


@dataclass(frozen=True)
class ChoiceDefinition:
    """One option an interactive step offers the operator."""

    label: str
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("choice label must be a non-empty string")
        if not isinstance(self.description, str):
            raise ValueError("choice description must be a string")

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "description": self.description}


@dataclass(frozen=True)
class ModeDefinition:
    name: str
    description: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "description": list(self.description)}


@dataclass(frozen=True)
class ProfileDefinition:
    """A root-level profile and its optional inline instruction."""

    name: str
    description: str | None


@dataclass(frozen=True)
class ItemFieldUpdate:
    """An agent-owned action's promise to set a custom field on its item.

    On a per-item stage the stage's item must carry the field when the stage
    completes; on the collection step every collected item must.
    """

    name: str
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_-]*", self.name
        ):
            raise ValueError(f"invalid item field name: {self.name!r}")
        if not isinstance(self.description, str):
            raise ValueError("item field description must be a string")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description}


@dataclass(frozen=True)
class HandlerDefinition:
    """Shared policy around a typed action or implicit name reference."""

    name: str
    description: str = ""
    action: DefinedAction | None = None
    # Core structural operation.  Unlike ``action``, it never enters the
    # ordinary action registry.
    operation: WorkflowHandoff | ChildWorkflowRun | None = None
    provide: tuple[ProvidedVariable, ...] = ()
    save_metadata: tuple[SavedMetadata, ...] = ()
    update_document: tuple[DocumentUpdate, ...] = ()
    update_item: tuple[ItemFieldUpdate, ...] = ()
    outputs: tuple[str, ...] = ()
    agent: str | None = None
    model: str | None = None
    reasoning: str | None = None
    # The directory this handler or step works in; ``None`` leaves the
    # choice to the enclosing step (for a step) or the task workspace.
    workdir: Workdir | None = None

    @property
    def is_reference(self) -> bool:
        """Whether this only names a root or extension handler to run.

        With no action, description, or values of its own, a hook or handler
        entry such as ``- update-readme: ~`` stands for the handler it names.
        """
        return not (
            self.action is not None
            or self.operation is not None
            or self.description
            or self.provide
            or self.save_metadata
        )


def step_filter_matches(
    workflow_names: tuple[str, ...] | None,
    step_names: tuple[str, ...] | None,
    workflow_name: str,
    step_name: str,
    step_path: str,
    precise_step_paths: frozenset[str] = frozenset(),
) -> bool:
    """Whether ``workflows``/``steps`` filters admit one step of one workflow.

    ``None`` admits everything; a selector that is a precise logical path
    matches that path, any other matches the step's own name. Hooks and rule
    groups share this matching so a filter means the same in both.
    """
    logical_path = step_path.replace("/{item}", "")
    step_matches = step_names is None or any(
        logical_path == selector
        if selector in precise_step_paths
        else step_name == selector
        for selector in step_names
    )
    return (workflow_names is None or workflow_name in workflow_names) and (
        step_matches
    )


@dataclass(frozen=True)
class HookDefinition:
    phase: HookPhase
    handler: HandlerDefinition
    workflow_names: tuple[str, ...] = ()
    step_names: tuple[str, ...] = ()
    scope: HookScope = "global"
    path: str = ""
    # ``fix`` turns a failed ``before_complete`` hook into a rejected
    # completion the step's worker fixes, instead of an operator stop.
    on_failure: HookFailure = "operator"

    def applies_to(
        self,
        workflow_name: str,
        step_name: str,
        step_path: str,
        precise_step_paths: frozenset[str] = frozenset(),
    ) -> bool:
        return step_filter_matches(
            self.workflow_names or None,
            self.step_names or None,
            workflow_name,
            step_name,
            step_path,
            precise_step_paths,
        )


@dataclass(frozen=True)
class RuleHints:
    """The worker a rule asks to be judged by: agent, model, and reasoning.

    Each field is ``None`` when the rule leaves it to its group or the step.
    """

    agent: str | None = None
    model: str | None = None
    reasoning: str | None = None

    def overlay(self, other: RuleHints) -> RuleHints:
        """These hints with every field ``other`` sets replacing this one's."""
        return RuleHints(
            other.agent if other.agent is not None else self.agent,
            other.model if other.model is not None else self.model,
            other.reasoning if other.reasoning is not None else self.reasoning,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            name: value
            for name, value in (
                ("agent", self.agent),
                ("model", self.model),
                ("reasoning", self.reasoning),
            )
            if value is not None
        }


@dataclass(frozen=True)
class RuleDefinition:
    """One rule: text the step's agent follows, and optionally its check.

    ``id`` is ``<group>/<file stem>`` for a rule reached through a group and
    ``<step>/<ordinal>`` or ``<step>/<file stem>`` for a step's own entry.
    ``text_hash`` identifies the wording regardless of whitespace, so derived
    knowledge about a rule follows its text rather than its file.
    """

    id: str
    text: str
    summary: str
    text_hash: str
    paths: tuple[str, ...] = ()
    check: Commands | None = None
    max_fixes: int | None = None
    hints: RuleHints = RuleHints()
    # The rule file, or ``None`` for a rule written inline in YAML.
    source: str | None = None


@dataclass(frozen=True)
class RuleGroupRef:
    """A step's reference to a root rule group by name."""

    name: str


@dataclass(frozen=True)
class UnresolvedStepRule:
    """A bare string in a step's ``rules`` list, before the parser resolves it.

    It becomes a group reference, the rules of a file or directory, or the
    literal text of a rule; no normalized configuration keeps one.
    """

    value: str
    step: str
    ordinal: int


StepRule = RuleDefinition | RuleGroupRef | UnresolvedStepRule


@dataclass(frozen=True)
class RuleGroup:
    """A named set of rules and where they apply on their own.

    ``workflows`` and ``steps`` filter like a global hook's, except that
    ``None`` admits every workflow or step while an empty tuple admits none:
    such a group applies only where a step names it.
    """

    name: str
    rules: tuple[RuleDefinition, ...] = ()
    workflows: tuple[str, ...] | None = None
    steps: tuple[str, ...] | None = None
    hints: RuleHints = RuleHints()
    # Where the group was declared: the YAML, or the extension that ships it.
    origin: str = "configuration"

    def applies_to(
        self,
        workflow_name: str,
        step_name: str,
        step_path: str,
        precise_step_paths: frozenset[str] = frozenset(),
    ) -> bool:
        return step_filter_matches(
            self.workflows,
            self.steps,
            workflow_name,
            step_name,
            step_path,
            precise_step_paths,
        )


@dataclass(frozen=True)
class StepDefinition(HandlerDefinition):
    # ``false`` prevents orchestration settings from applying to this step.
    subagents: bool = True
    # A conversation with the operator, held by the session that can talk to
    # them; implies the step is performed without delegation.
    interactive: bool = False
    # The options the operator chooses from during an interactive step.
    choices: tuple[ChoiceDefinition, ...] = ()
    # The operator answers this per-item stage on the operator page.
    ui: bool = False
    profile: str | None = None
    profile_description: str | None = None
    hooks: tuple[HookDefinition, ...] = ()
    # The step's own rules, and the root groups it names, in declaration order.
    rules: tuple[StepRule, ...] = ()
    # The compiler flattens nested steps while preserving their parent identity.
    child_steps: tuple[StepDefinition, ...] = ()
    loop_steps: tuple[StepDefinition, ...] = ()
    loop_max_times: int | None = None
    loop_assignment: LoopAssignment | None = None
    loop_break: str | None = None
    loop_continue: str | None = None
    # A step with ``items`` collects work items, then runs ``items.steps``
    # once for every collected item.
    items: ItemFlow | None = None
    item_operation: ItemOperation | None = None
    artifact: bool = True
    collect_children: bool = False
    child_workflow: str | None = None
    artifact_dependency: str | None = None
    # An assessment is an agent prompt whose named outcome selects a conditional
    # subtree.  Empty outcomes use the compact positive/negative continuation.
    assessment_question: str | None = None
    assessment_outcomes: tuple[StepDefinition, ...] = ()
    # An assessment outcome that ends the workflow instead of running steps.
    stop_workflow: bool = False


@dataclass(frozen=True)
class ItemFlow:
    """The collection and per-item lifecycle owned by one ``items`` step.

    ``steps`` are the resolved per-item stages: the configured ones, or the
    single built-in stage of a bare ``items: ~``.  Empty ``steps`` collect
    items without processing them.  Item-flow worker settings are already
    folded into each stage while parsing.
    """

    steps: tuple[StepDefinition, ...] = ()
    description: str | None = None
    assignment: ItemAssignment = "all_items"
    # The items outlive the run: every run of the task reuses them, and the
    # collection stage reconciles them instead of splitting anew.
    shared: bool = False
    # The custom field a new item must carry, and the fields whose values
    # form one pool in which each value may appear once across all items.
    identity: str | None = None
    unique: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    description: str = ""
    steps: tuple[StepDefinition, ...] = ()
    hooks: tuple[HookDefinition, ...] = ()
    modes: tuple[str, ...] = ()
    agent: str | None = None
    model: str | None = None
    reasoning: str | None = None
    profile: str | None = None
    profile_description: str | None = None
    handoff: bool = False
    # The runtime ``start`` uses for this workflow when ``--runtime`` is
    # omitted; it outranks the project default, and the flag outranks it.
    runtime: str | None = None
    # A new start while this workflow's previous run is unfinished abandons
    # that run instead of being refused.
    restartable: bool = False
    # The workflow this one copies, recorded for display; the definition is
    # already complete, so nothing downstream resolves it again.
    inherits: str | None = None
    # A workflow to offer the operator once this one completes.
    recommended_next_workflow: str | None = None


def binds_task_identity(workflow: WorkflowDefinition) -> bool:
    """Whether a workflow's first step supplies the task's external ID.

    Such a workflow started without an ID, or run for a child added without
    one, first executes that step as an identity request and binds the task
    to the ID it provides.
    """
    return bool(workflow.steps) and any(
        value.name == "task_id" for value in workflow.steps[0].provide
    )


def delegation_requests(workflow: WorkflowDefinition) -> tuple[str, ...]:
    """Names of the steps in ``workflow`` that ask for a particular worker.

    A declared ``agent``, ``model``, ``reasoning``, or ``profile`` is a request
    for who should perform the work. Only the ``auto`` runtime can act on one,
    because only it delegates; ``single`` keeps the request on the plan and
    performs the step in the caller's own session. A workflow that carries
    these is therefore written for ``auto``, and saying so lets ``discover``
    point that out rather than leaving the reader to infer it.
    """
    requested: list[str] = []
    if _requests_worker(workflow):
        requested.append(workflow.name)
    for step in _every_step(workflow.steps):
        if _requests_worker(step) and step.name not in requested:
            requested.append(step.name)
    return tuple(requested)


def _requests_worker(definition: object) -> bool:
    return any(
        getattr(definition, field, None)
        for field in ("agent", "model", "reasoning", "profile")
    )


def _every_step(steps: Iterable[StepDefinition]) -> Iterator[StepDefinition]:
    """Walk a step tree: nested steps, loop bodies, assessments, item stages."""
    for step in steps:
        yield step
        yield from _every_step(step.child_steps)
        yield from _every_step(step.loop_steps)
        yield from _every_step(step.assessment_outcomes)
        if step.items is not None:
            yield from _every_step(step.items.steps)


@dataclass(frozen=True)
class WorkflowConfiguration:
    modes: tuple[ModeDefinition, ...]
    profiles: tuple[ProfileDefinition, ...]
    handlers: tuple[HandlerDefinition, ...]
    global_hooks: tuple[HookDefinition, ...]
    workflows: tuple[WorkflowDefinition, ...]
    documents: tuple[DocumentDefinition, ...] = ()
    # Root rule groups, extension groups first, then YAML order.
    rule_groups: tuple[RuleGroup, ...] = ()

    @property
    def rule_groups_by_name(self) -> dict[str, RuleGroup]:
        return {group.name: group for group in self.rule_groups}

    @property
    def documents_by_name(self) -> dict[str, DocumentDefinition]:
        return {document.name: document for document in self.documents}

    @property
    def handlers_by_name(self) -> dict[str, HandlerDefinition]:
        return {handler.name: handler for handler in self.handlers}

    @property
    def profiles_by_name(self) -> dict[str, ProfileDefinition]:
        return {profile.name: profile for profile in self.profiles}

    @property
    def workflows_by_name(self) -> dict[str, WorkflowDefinition]:
        return {workflow.name: workflow for workflow in self.workflows}


def every_step(configuration: WorkflowConfiguration) -> Iterator[StepDefinition]:
    """Every step of every workflow and of every step-shaped root handler."""
    for workflow in configuration.workflows:
        yield from _every_step(workflow.steps)
    for handler in configuration.handlers:
        if isinstance(handler, StepDefinition):
            yield from _every_step((handler,))
