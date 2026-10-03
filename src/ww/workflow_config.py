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

from ww.contracts import (
    HookFailure,
    HookPhase,
    HookScope,
    ItemAssignment,
    ItemOperation,
    LoopAssignment,
    StepRole,
)
from ww.operations import ChildWorkflowRun, WorkflowHandoff
from ww.workspace import Workdir

MetadataScope = Literal["task", "project"]
# A document also has the user scope: one file per user, in the user
# configuration directory, shared by every project.
DocumentScope = Literal["task", "project", "user"]
DOCUMENT_SCOPES = ("task", "project", "user")
# The project metadata namespace ww keeps its own state in, such as
# ``ww.setup.done``; no workflow may save into it.
WW_METADATA_NAMESPACE = "ww"
# The task ID in a task-scoped document ``path``.
TASK_ID_TOKEN = "{{ww.task.id}}"
# A saved metadata name, e.g. "pr.url" or "base-branch".
_SAVED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
# A dotted saved metadata key, e.g. "github.owner"; "github..owner" does not
# match.
_SAVED_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*")
# A document or item field name, e.g. "plan" or "due-date"; "2nd" does not
# match.
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")

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
    """A metadata value a handler must preserve."""

    name: str
    key: str
    description: str = ""
    scope: MetadataScope = "task"
    # An append key holds a list: each completion may add values, none is
    # required, and repeats are dropped.
    append: bool = False
    source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SAVED_NAME.fullmatch(self.name):
            raise ValueError(f"invalid saved metadata name: {self.name!r}")
        if not isinstance(self.key, str) or not _SAVED_KEY.fullmatch(self.key):
            raise ValueError(f"invalid saved metadata key: {self.key!r}")
        if not isinstance(self.description, str):
            raise ValueError("saved metadata description must be a string")
        if self.scope not in {"task", "project"}:
            raise ValueError(f"invalid saved metadata scope: {self.scope!r}")
        if self.scope == "project" and (
            self.key == WW_METADATA_NAMESPACE
            or self.key.startswith(f"{WW_METADATA_NAMESPACE}.")
        ):
            raise ValueError(
                f"project metadata under {WW_METADATA_NAMESPACE}. is ww's own "
                "state, such as its onboarding; save under another path"
            )
        if self.source is not None and self.source != "stdout":
            raise ValueError("saved metadata source must be stdout")
        if type(self.append) is not bool:
            raise ValueError("saved metadata append must be a bool")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "name": self.name,
            "key": self.key,
            "description": self.description,
            "scope": self.scope,
        }
        if self.source is not None:
            data["source"] = self.source
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
    scope: DocumentScope = "task"
    # An explicit file, relative to the project (or, for a task document, to
    # the task's working directory when the run has one; for a user document,
    # to the user configuration directory).  ``{{ww.task.id}}`` is replaced in
    # a task-scoped path.  Omitted, the document lives under ``.ww``, or in
    # the user configuration directory for the user scope.
    path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _FIELD_NAME.fullmatch(self.name):
            raise ValueError(f"invalid document name: {self.name!r}")
        if not isinstance(self.description, str):
            raise ValueError("document description must be a string")
        if self.scope not in DOCUMENT_SCOPES:
            raise ValueError(f"invalid document scope: {self.scope!r}")
        if self.path is not None:
            if not isinstance(self.path, str) or not self.path.strip():
                raise ValueError("document path must be a non-empty string")
            parts = self.path.replace("\\", "/").split("/")
            if self.path.startswith(("/", "\\")) or ".." in parts:
                inside = (
                    "the user configuration directory"
                    if self.scope == "user"
                    else "the project"
                )
                raise ValueError(
                    f"document path must stay inside {inside}: {self.path!r}"
                )
            if self.scope != "task" and TASK_ID_TOKEN in self.path:
                raise ValueError(
                    f"a {self.scope} document path cannot use {{{{ww.task.id}}}}"
                )

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
        if not isinstance(self.name, str) or not _FIELD_NAME.fullmatch(self.name):
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
    """A root mode: guidance delivered on the pages of the steps it covers.

    A mode applies where it is selected (``start --mode`` or the workflow's
    default modes). ``workflows`` and ``steps`` are its filters: a mode with
    either one also applies automatically wherever it matches; ``None`` means
    the key is absent. On a mode, as on a hook, ``[]`` admits every name.
    """

    name: str
    description: tuple[str, ...] = ()
    workflows: NameFilter | None = None
    steps: NameFilter | None = None

    @property
    def automatic(self) -> bool:
        return self.workflows is not None or self.steps is not None

    def applies_to(
        self,
        workflow_name: str,
        step_name: str,
        step_path: str,
        precise_step_paths: frozenset[str] = frozenset(),
    ) -> bool:
        """Whether this mode applies automatically to one step of one workflow."""
        return self.automatic and step_filter_matches(
            self.workflows or ALL,
            self.steps or ALL,
            workflow_name,
            step_name,
            step_path,
            precise_step_paths,
        )

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "name": self.name,
            "description": list(self.description),
        }
        if self.workflows is not None:
            data["workflows"] = self.workflows.to_data()
        if self.steps is not None:
            data["steps"] = self.steps.to_data()
        return data


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
        if not isinstance(self.name, str) or not _FIELD_NAME.fullmatch(self.name):
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
    # ``args`` of a reference to an extension handler: the positional
    # arguments it is run with, templates allowed.
    extension_arguments: tuple[str, ...] = ()
    on_failure: HookFailure | None = None
    on_failure_instruction: str | None = None
    # An ordered group of ww-owned actions, sharing a logical lifecycle.
    handlers: tuple[HandlerDefinition, ...] = ()

    @property
    def is_reference(self) -> bool:
        """Whether this only names a root or extension handler to run.

        With no action, description, or values of its own, a hook or handler
        entry such as ``- update-readme: ~`` stands for the handler it names.
        """
        return not (
            self.action is not None
            or self.handlers
            or self.operation is not None
            or self.description
            or self.provide
            or self.save_metadata
        )


ALL_NAMES = "*"


@dataclass(frozen=True)
class NameFilter:
    """The names a ``workflows`` or ``steps`` filter admits.

    ``names`` is ``None`` when every name is admitted (``"*"``, or an omitted
    key); otherwise only the names listed, and none when it is empty. The
    configuration writes it as ``"*"`` or a list of names.
    """

    names: tuple[str, ...] | None = None

    @classmethod
    def of(cls, names: Iterable[str]) -> NameFilter:
        return cls(tuple(names))

    @property
    def admits_all(self) -> bool:
        return self.names is None

    @property
    def admits_none(self) -> bool:
        return self.names == ()

    @property
    def listed(self) -> tuple[str, ...]:
        """The names listed; empty when the filter admits every name."""
        return self.names or ()

    def admits(self, name: str) -> bool:
        return self.names is None or name in self.names

    def to_data(self) -> str | list[str]:
        """``"*"`` for every name, else the list of names."""
        return ALL_NAMES if self.names is None else list(self.names)


ALL = NameFilter()
# Admits no name: a rule group with ``[]`` applies only where a step names it.
NO_NAMES = NameFilter(())


def step_filter_matches(
    workflows: NameFilter,
    steps: NameFilter,
    workflow_name: str,
    step_name: str,
    step_path: str,
    precise_step_paths: frozenset[str] = frozenset(),
) -> bool:
    """Whether ``workflows``/``steps`` filters admit one step of one workflow.

    A step selector that is a precise logical path matches that path, any
    other matches the step's own name. Hooks and rule groups share this
    matching so a filter means the same in both.
    """
    logical_path = step_path.replace("/{item}", "").replace("/{child}", "")
    step_matches = steps.names is None or any(
        logical_path == selector
        if selector in precise_step_paths
        else step_name == selector
        for selector in steps.names
    )
    return workflows.admits(workflow_name) and step_matches


@dataclass(frozen=True)
class HookDefinition:
    phase: HookPhase
    handler: HandlerDefinition
    # A hook's ``[]`` admits every name, like an omitted key.
    workflows: NameFilter = ALL
    steps: NameFilter = ALL
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
            self.workflows,
            self.steps,
            workflow_name,
            step_name,
            step_path,
            precise_step_paths,
        )

    def applies_in(
        self,
        workflow: WorkflowDefinition,
        step_name: str,
        step_path: str,
        precise_step_paths: frozenset[str] = frozenset(),
    ) -> bool:
        """Whether this hook runs for one step of ``workflow``.

        A hook filtered with ``workflows`` applies when the filter admits the
        workflow's own name or the lane it takes its hooks from
        (``hooks_from``), so a workflow with a lane keeps the hooks written
        for itself.
        """
        return any(
            self.applies_to(name, step_name, step_path, precise_step_paths)
            for name in dict.fromkeys((workflow.name, workflow.lane))
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

    ``workflows`` and ``steps`` filter like a global hook's, except that an
    empty filter admits none: such a group applies only where a step names it.
    """

    name: str
    rules: tuple[RuleDefinition, ...] = ()
    workflows: NameFilter = ALL
    steps: NameFilter = ALL
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
    # Who performs the step: ``manager`` in its own session, ``worker`` when
    # delegated. ``None`` inherits along the step chain, like ``profile``.
    role: StepRole | None = None
    # ``False``: whoever performs the step spawns no subagents for anything.
    # ``None`` inherits along the step chain, like ``profile``.
    subagents: bool | None = None
    # A conversation with the operator, held by the session that can talk to
    # them; implies ``role: manager``.
    interactive: bool = False
    # Opt-in artifact source for feedback deduction after workflow completion.
    learnable: bool = False
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
    max_rounds: int | None = None
    loop_assignment: LoopAssignment | None = None
    loop_break: str | None = None
    loop_continue: str | None = None
    # A step with ``items`` collects work items, then runs ``items.steps``
    # once for every collected item.
    items: ItemFlow | None = None
    item_operation: ItemOperation | None = None
    artifact: bool = True
    # A step with ``children`` collects child tasks, then runs each with
    # ``children.workflow``, or runs ``children.steps`` once per child.
    children: ChildFlow | None = None
    artifact_dependency: str | None = None
    # An assessment is an agent prompt whose named outcome selects a conditional
    # subtree.  Empty outcomes use the compact positive/negative continuation.
    assessment_question: str | None = None
    assessment_outcomes: tuple[StepDefinition, ...] = ()
    # An assessment outcome that ends the workflow instead of running steps.
    stop_workflow: bool = False


@dataclass(frozen=True)
class ChildFlow:
    """The child tasks owned by one ``children`` step.

    The step's own action collects them with ``add-child``.  Without
    ``steps`` ww then runs every child, one at a time, with ``workflow``, and
    the parent continues after the last one completes.  With ``steps`` the
    parent runs those stages once per child, one child at a time; exactly one
    of them runs the child task with ``workflow`` and waits for it.
    """

    workflow: str
    # Splitting guidance for the collecting agent, as ``items.description``.
    description: str | None = None
    # The parent's stages per child; the one running the child carries a
    # ``ChildWorkflowRun`` operation.  Empty in the simple form.
    steps: tuple[StepDefinition, ...] = ()


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
    assignment: ItemAssignment = "together"
    # The items outlive the run: every run of the task reuses them, and the
    # collection stage reconciles them instead of splitting anew.
    persistent: bool = False
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
    # The role every step of the workflow inherits unless it sets its own.
    role: StepRole | None = None
    # Whether the performers of its steps may spawn subagents, inherited.
    subagents: bool | None = None
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
    # The workflow whose global hooks this one runs with: a hook filtered to
    # ``workflows: [task]`` applies here too when this names ``task``, so a
    # workflow can take a lane's branch, worktree and commit handling.
    hooks_from: str | None = None
    # ``start`` refuses until ``hooks_from`` is set, for a workflow that must
    # not run without the project's lane handling (a built-in that installs
    # tools and writes configuration, for example).
    needs_hooks_from: bool = False

    @property
    def lane(self) -> str:
        """The workflow whose project handling this one takes.

        Its ``hooks_from`` when it has one, else itself: global hooks filtered
        to the lane apply here, and extensions key their workflow settings
        (a branch format, a base branch) by it.
        """
        return self.hooks_from or self.name

    @property
    def hands_off(self) -> bool:
        """Whether the workflow ends by handing the task to another workflow.

        The transition itself declares it: a ``handoff_to:`` step, or a
        ``handoff_to:`` hook on a step.  Validation places it at the end.
        """
        return any(
            isinstance(step.operation, WorkflowHandoff)
            or any(
                isinstance(hook.handler.operation, WorkflowHandoff)
                for hook in step.hooks
            )
            for step in step_tree(self.steps)
        )


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
    for step in step_tree(workflow.steps):
        if _requests_worker(step) and step.name not in requested:
            requested.append(step.name)
    return tuple(requested)


def _requests_worker(definition: object) -> bool:
    return any(
        getattr(definition, field, None)
        for field in ("agent", "model", "reasoning", "profile")
    )


def step_tree(steps: Iterable[StepDefinition]) -> Iterator[StepDefinition]:
    """Walk a step tree: nested steps, loop bodies, assessments, item and
    per-child stages."""
    for step in steps:
        yield step
        yield from step_tree(step.child_steps)
        yield from step_tree(step.loop_steps)
        yield from step_tree(step.assessment_outcomes)
        if step.items is not None:
            yield from step_tree(step.items.steps)
        if step.children is not None:
            yield from step_tree(step.children.steps)


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
        yield from step_tree(workflow.steps)
    for handler in configuration.handlers:
        if isinstance(handler, StepDefinition):
            yield from step_tree((handler,))
