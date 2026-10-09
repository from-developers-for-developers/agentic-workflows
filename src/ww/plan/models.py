# SPDX-License-Identifier: GPL-3.0-or-later
"""Immutable plan data and serialization."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TypeVar

from ww.actions import Commands, PlannedAction, actions
from ww.contracts import (
    CheckSource,
    ChildOperation,
    ExecutionKind,
    ItemOperation,
    PlanItemKind,
    PlanItemOwner,
    PlanItemPhase,
    StepRole,
)
from ww.operations import PlanOperation, encode_operation
from ww.validation import is_positive_int
from ww.workflow_config import (
    ChoiceDefinition,
    DocumentDefinition,
    DocumentUpdate,
    ItemFieldUpdate,
    ProvidedVariable,
    RuleHints,
    SavedMetadata,
)
from ww.workspace import WORKDIRS, Workdir

PayloadT = TypeVar("PayloadT")


@dataclass(frozen=True)
class PlannedMode:
    """A mode frozen into the plan for one agent step.

    ``automatic`` marks a mode the step gets from the mode's own filters
    rather than from the run's selected modes. The description is frozen so
    a run keeps delivering the guidance it started with.
    """

    name: str
    description: tuple[str, ...] = ()
    automatic: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": list(self.description),
            "automatic": self.automatic,
        }

    @classmethod
    def from_dict(cls, value: object, path: str) -> PlannedMode:
        """Read a mode written by :meth:`to_dict`, rejecting any other shape."""
        keys = {"name", "description", "automatic"}
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError(
                f"{path} must be an object of name, description, automatic"
            )
        name, description, automatic = (
            value["name"],
            value["description"],
            value["automatic"],
        )
        if (
            not isinstance(name, str)
            or not isinstance(description, list)
            or not all(isinstance(line, str) for line in description)
            or not isinstance(automatic, bool)
        ):
            raise ValueError(f"{path} has an invalid name, description or automatic")
        return cls(name, tuple(description), automatic)


@dataclass(frozen=True)
class PlannedRule:
    """A rule frozen into the plan for one agent step.

    The text and its hash are frozen so a run keeps delivering the wording it
    started with; ``has_command`` says whether ww checks it mechanically.
    ``source`` is the rule file, relative to the project root when it lies
    inside it, or ``None`` for a rule written in the step's YAML.
    """

    id: str
    summary: str
    text: str
    text_hash: str
    paths: tuple[str, ...] = ()
    contains_in_file: tuple[str, ...] = ()
    contains_in_diff: tuple[str, ...] = ()
    has_command: bool = False
    max_fixes: int = 1
    hints: RuleHints = RuleHints()
    source: str | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id,
            "summary": self.summary,
            "text": self.text,
            "text_hash": self.text_hash,
            "paths": list(self.paths),
            "contains_in_file": list(self.contains_in_file),
            "contains_in_diff": list(self.contains_in_diff),
            "has_command": self.has_command,
            "max_fixes": self.max_fixes,
        }
        if self.hints.to_dict():
            data["hints"] = self.hints.to_dict()
        if self.source is not None:
            data["source"] = self.source
        return data


@dataclass(frozen=True)
class PlannedCheck:
    """A command ww runs when the step completes; a failure sends it back.

    ``command`` is already planned like any cli handler's: build-time values
    are substituted and runtime ones are left for execution. ``summary`` is
    the rule's first sentence, or the hook's handler name.

    A ``derived`` check is one the operator approved into the rule-automation
    store; it is never compiled into a plan but resolved when the step
    begins, and ``covers`` names the rules of the step it checks, so a check
    shared by several rules runs once.

    ``files`` are the project files the command needs, relative to the
    step's directory, such as the script it runs: when one is missing there
    the check is unavailable and does not run.
    """

    id: str
    source: CheckSource
    summary: str
    command: Commands
    paths: tuple[str, ...] = ()
    contains_in_file: tuple[str, ...] = ()
    contains_in_diff: tuple[str, ...] = ()
    max_fixes: int = 1
    covers: tuple[str, ...] = ()
    on_failure_instruction: str | None = None
    files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source not in {"rule", "hook", "derived"}:
            raise ValueError(f"invalid check source: {self.source!r}")
        if not is_positive_int(self.max_fixes):
            raise ValueError("check max_fixes must be a positive integer")
        if bool(self.covers) != (self.source == "derived"):
            raise ValueError("exactly a derived check names the rules it covers")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id,
            "source": self.source,
            "summary": self.summary,
            "command": actions.get("cli").encode(self.command),
            "paths": list(self.paths),
            "contains_in_file": list(self.contains_in_file),
            "contains_in_diff": list(self.contains_in_diff),
            "max_fixes": self.max_fixes,
        }
        if self.covers:
            data["covers"] = list(self.covers)
        if self.on_failure_instruction is not None:
            data["on_failure_instruction"] = self.on_failure_instruction
        if self.files:
            data["files"] = list(self.files)
        return data


@dataclass(frozen=True)
class VerificationTarget:
    """What a ww-generated verification item verifies.

    The item judges, or proposes a check for, the rules without a command of
    the agent item ``item_id``. Each distinct set of worker hints among those
    rules gets its own verification item; ``ordinal`` numbers them in the
    order ww created them and ``hints`` is the set this one runs with.
    """

    item_id: str
    ordinal: int
    hints: RuleHints = RuleHints()

    def __post_init__(self) -> None:
        if not is_positive_int(self.ordinal):
            raise ValueError("verification ordinal must be a positive integer")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"item_id": self.item_id, "ordinal": self.ordinal}
        if self.hints.to_dict():
            data["hints"] = self.hints.to_dict()
        return data


@dataclass(frozen=True)
class PlanItem:
    id: str
    position: int
    name: str
    description: str
    operation: PlanOperation
    owner: PlanItemOwner
    execution: ExecutionKind
    requires_agent_input: bool
    workflow: str
    step: str
    parent: str | None
    phase: PlanItemPhase
    source: str
    registered_handler: str | None
    provide: tuple[ProvidedVariable, ...] = ()
    save_metadata: tuple[SavedMetadata, ...] = ()
    # Documents this agent-owned item creates or edits in place.
    update_document: tuple[DocumentUpdate, ...] = ()
    # Custom item fields this agent-owned item must set on its item, or on
    # every item when it is the collection.
    update_item: tuple[ItemFieldUpdate, ...] = ()
    outputs: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    requested_agent: str | None = None
    requested_model: str | None = None
    requested_reasoning: str | None = None
    # Who performs the item: the manager in its own session, or a worker.
    role: StepRole = "worker"
    # ``False``: its performer spawns no subagents for anything.
    subagents: bool = True
    # A conversation with the operator; performed by the session that can
    # talk to them, so ``role`` is ``manager`` as well.
    interactive: bool = False
    explicit: bool = False
    learnable: bool = False
    choices: tuple[ChoiceDefinition, ...] = ()
    # A step the operator answers on the operator page.
    ui: bool = False
    model: str | None = None
    reasoning: str | None = None
    profile: str | None = None
    profile_instruction: str | None = None
    # A project-local profile file, relative to the project root; the
    # instruction prints it absolute for the current filesystem.
    profile_path: str | None = None
    # The directory this item works in: the task workspace, the project's
    # own directory, or the project root.
    workdir: Workdir = "task"
    summary: bool = False
    item_operation: ItemOperation | None = None
    child_template: bool = False
    # Nearest concrete collection path; independent of work-item and plan IDs.
    item_context: str | None = None
    split_instruction: str | None = None
    # On a collection item: the items outlive the run and are reconciled.
    shared_items: bool = False
    # On a collection item: the field a new item must carry, and the fields
    # whose values may each appear once across all items.
    item_identity: str | None = None
    item_unique: tuple[str, ...] = ()
    artifact: bool = True
    child_operation: ChildOperation | None = None
    # On a children collection: each child binds its own external ID through
    # the first step of the child workflow, so ``add-child`` takes no ``--id``.
    child_identity: bool = False
    # On every per-child stage (``children.steps``) and its hooks: the path
    # of the ``children`` step. Its templates carry ``child_template`` until
    # the children are collected.
    child_stage: str | None = None
    # On a concrete per-child stage: the one-based position of its child in
    # the run's children, which are append-only and never reordered.
    child_number: int | None = None
    # Ordered logical container paths above ``parent``. Hierarchy is explicit
    # plan data; consumers must not reconstruct it by parsing ``step``.
    ancestors: tuple[str, ...] = ()
    # One-based declaration ordinal for every path in ``(*ancestors, step)``.
    # Artifact storage uses these values to produce stable, ordered paths.
    step_ordinals: tuple[int, ...] = ()
    artifact_dependency: str | None = None
    assessment_question: str | None = None
    assessment_outcomes: tuple[str, ...] = ()
    # The outcomes that end the workflow; they emit no items of their own.
    assessment_stops: tuple[str, ...] = ()
    assessment_parent: str | None = None
    assessment_outcome: str | None = None
    # Rules delivered on this agent step's page, and the checks ww runs when
    # it completes.
    rules: tuple[PlannedRule, ...] = ()
    checks: tuple[PlannedCheck, ...] = ()
    # The modes delivered on this agent step's page: the run's selected
    # modes, then the automatic modes whose filters admit the step.
    modes: tuple[PlannedMode, ...] = ()
    # Set on a verification item ww inserts before an agent step whose rules
    # without a command need a verifier; never compiled from configuration.
    verifies: VerificationTarget | None = None
    on_failure: str = "operator"
    on_failure_instruction: str | None = None
    max_handler_fixes: int = 3

    @property
    def kind(self) -> PlanItemKind:
        return self.operation.kind

    @property
    def hands_over(self) -> bool:
        """Whether completing this item must leave a summary for the next step.

        Only an ordinary agent step does: hooks, ``init`` (its artifact is the
        requirements), the built-in workflow summary, and verification items
        are exempt.
        """
        return (
            self.phase == "step"
            and self.owner == "agent"
            and not self.summary
            and self.step != "init"
            and self.verifies is None
        )

    def payload_as(self, payload_type: type[PayloadT]) -> PayloadT:
        if not isinstance(self.operation, PlannedAction):
            raise ValueError(f"plan operation {self.kind!r} has no action payload")
        payload = self.operation.payload
        if not isinstance(payload, payload_type):
            raise ValueError(
                f"plan action {self.kind!r} does not contain "
                f"{payload_type.__name__} data"
            )
        return payload

    def __post_init__(self) -> None:
        if self.on_failure not in {"operator", "fix"}:
            raise ValueError("invalid handler failure policy")
        if not is_positive_int(self.max_handler_fixes):
            raise ValueError("handler fix limit must be positive")
        if (self.on_failure == "fix" or self.on_failure_instruction is not None) and (
            self.kind != "cli" or self.owner != "ww"
        ):
            raise ValueError("handler repair requires an automatic command")
        if self.phase not in {
            "before_start_workflow",
            "before_start",
            "step",
            "before_complete",
            "after_complete",
            "before_complete_workflow",
        }:
            raise ValueError(f"invalid plan item phase: {self.phase!r}")
        if self.item_operation not in {
            None,
            "collect",
            "complete_collection",
            "save_fields",
        }:
            raise ValueError(f"invalid item operation: {self.item_operation!r}")
        if self.workdir not in WORKDIRS:
            raise ValueError(f"invalid workdir: {self.workdir!r}")
        if self.child_operation not in {None, "collect"}:
            raise ValueError(f"invalid child operation: {self.child_operation!r}")
        if self.child_number is not None and (
            self.child_stage is None or not is_positive_int(self.child_number)
        ):
            raise ValueError("a child number belongs to a per-child stage")
        if self.step_ordinals and (
            len(self.step_ordinals) != len(self.ancestors) + 1
            or not all(is_positive_int(value) for value in self.step_ordinals)
        ):
            raise ValueError("step ordinals must match the positive step hierarchy")
        if isinstance(self.operation, PlannedAction):
            contract = actions.get(self.kind) if actions.contains(self.kind) else None
            if contract is not None and (
                self.owner != contract.owner or self.execution != contract.execution
            ):
                raise ValueError(
                    f"plan item kind {self.kind!r} conflicts with owner/execution"
                )
            if contract is not None and not isinstance(
                self.operation.payload, contract.planned_type
            ):
                raise ValueError(
                    f"plan item kind {self.kind!r} has the wrong action payload type"
                )
        elif (self.owner, self.execution) != (
            self.operation.owner,
            self.operation.execution,
        ):
            raise ValueError(
                f"{self.kind} items must be {self.operation.owner}-owned "
                f"{self.operation.execution} operations"
            )
        # ``validate`` accepts the pre-planning definition.  Planned payloads
        # may deliberately have a distinct type, and their strict boundary is
        # the action's decoder (used for persisted snapshots).
        expected_agent_input = self.execution == "automatic" and bool(self.provide)
        if self.requires_agent_input != expected_agent_input:
            raise ValueError(
                "requires_agent_input must match an automatic action with provided "
                "values"
            )
        if (self.rules or self.checks) and self.owner != "agent":
            raise ValueError("only agent-owned plan items carry rules and checks")
        if self.modes and self.owner != "agent":
            raise ValueError("only agent-owned plan items carry modes")
        if self.verifies is not None and (
            self.owner != "agent" or self.rules or self.checks or self.provide
        ):
            raise ValueError(
                "a verification item is agent-owned and carries no rules, checks, "
                "or provided values"
            )
        if self.save_metadata and self.owner != "agent" and self.kind != "cli":
            raise ValueError("only agent-owned or CLI plan items can save metadata")
        if self.update_document and self.owner != "agent":
            raise ValueError("only agent-owned plan items can update documents")
        if self.update_item and self.owner != "agent" and self.kind != "cli":
            raise ValueError("only agent-owned or CLI plan items can save item fields")
        metadata_names = [item.name for item in self.save_metadata]
        metadata_keys = [(item.scope, item.key) for item in self.save_metadata]
        if len(metadata_names) != len(set(metadata_names)):
            raise ValueError("plan item has duplicate saved metadata names")
        if len(metadata_keys) != len(set(metadata_keys)):
            raise ValueError("plan item has duplicate saved metadata keys")
        for index, (scope, key) in enumerate(metadata_keys):
            if any(
                scope == other_scope
                and (key.startswith(f"{other}.") or other.startswith(f"{key}."))
                for other_scope, other in metadata_keys[index + 1 :]
            ):
                raise ValueError("plan item has overlapping saved metadata keys")

    def to_dict(self) -> dict[str, object]:
        """The persisted form."""
        data = self._to_dict()
        # Pass identity is written only where it applies.
        if self.item_context is not None:
            data["item_context"] = self.item_context
        if self.on_failure != "operator":
            data["on_failure"] = self.on_failure
            data["max_handler_fixes"] = self.max_handler_fixes
        if self.on_failure_instruction is not None:
            data["on_failure_instruction"] = self.on_failure_instruction
        if self.profile_path is None:
            del data["profile_path"]
        if self.workdir == "task":
            del data["workdir"]
        if not self.update_document:
            del data["update_document"]
        if not self.interactive:
            del data["interactive"]
        if not self.explicit:
            del data["explicit"]
        if not self.learnable:
            del data["learnable"]
        if not self.choices:
            del data["choices"]
        if not self.ui:
            del data["ui"]
        if not self.shared_items:
            del data["shared_items"]
        if not self.update_item:
            del data["update_item"]
        if self.item_identity is None and not self.item_unique:
            del data["item_identity"], data["item_unique"]
        if not self.rules:
            del data["rules"]
        if not self.checks:
            del data["checks"]
        if not self.modes:
            del data["modes"]
        if self.verifies is None:
            del data["verifies"]
        if self.child_stage is None:
            del data["child_stage"]
        if self.child_number is None:
            del data["child_number"]
        return data

    def _to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "position": self.position,
            "name": self.name,
            "description": self.description,
            "operation": encode_operation(self.operation),
            "owner": self.owner,
            "execution": self.execution,
            "requires_agent_input": self.requires_agent_input,
            "workflow": self.workflow,
            "step": self.step,
            "parent": self.parent,
            "phase": self.phase,
            "source": self.source,
            "registered_handler": self.registered_handler,
            "provide": [item.to_dict() for item in self.provide],
            "save_metadata": [item.to_dict() for item in self.save_metadata],
            "update_document": [item.to_dict() for item in self.update_document],
            "outputs": list(self.outputs),
            "dependencies": list(self.dependencies),
            "requested_agent": self.requested_agent,
            "requested_model": self.requested_model,
            "requested_reasoning": self.requested_reasoning,
            "role": self.role,
            "subagents": self.subagents,
            "interactive": self.interactive,
            "explicit": self.explicit,
            "learnable": self.learnable,
            "choices": [choice.to_dict() for choice in self.choices],
            "ui": self.ui,
            "model": self.model,
            "reasoning": self.reasoning,
            "profile": self.profile,
            "profile_instruction": self.profile_instruction,
            "profile_path": self.profile_path,
            "workdir": self.workdir,
            "summary": self.summary,
            "item_operation": self.item_operation,
            "child_template": self.child_template,
            "split_instruction": self.split_instruction,
            "shared_items": self.shared_items,
            "update_item": [item.to_dict() for item in self.update_item],
            "item_identity": self.item_identity,
            "item_unique": list(self.item_unique),
            "artifact": self.artifact,
            "child_operation": self.child_operation,
            "child_identity": self.child_identity,
            "child_stage": self.child_stage,
            "child_number": self.child_number,
            "ancestors": list(self.ancestors),
            "step_ordinals": list(self.step_ordinals),
            "artifact_dependency": self.artifact_dependency,
            "assessment_question": self.assessment_question,
            "assessment_outcomes": list(self.assessment_outcomes),
            "assessment_parent": self.assessment_parent,
            "assessment_outcome": self.assessment_outcome,
            # Written only when set: a plan without stops has no such key.
            **(
                {"assessment_stops": list(self.assessment_stops)}
                if self.assessment_stops
                else {}
            ),
            "rules": [rule.to_dict() for rule in self.rules],
            "checks": [check.to_dict() for check in self.checks],
            "modes": [mode.to_dict() for mode in self.modes],
            "verifies": self.verifies.to_dict() if self.verifies else None,
        }


def number_step_paths(items: tuple[PlanItem, ...]) -> tuple[PlanItem, ...]:
    """Attach each plan item's declaration ordinal at every hierarchy level."""
    children: dict[str | None, list[str]] = {}
    ordinals: dict[str, int] = {}
    for item in items:
        chain = (*item.ancestors, item.step)
        parent: str | None = None
        for path in chain:
            siblings = children.setdefault(parent, [])
            if path not in siblings:
                siblings.append(path)
                ordinals[path] = len(siblings)
            parent = path
    return tuple(
        replace(
            item,
            step_ordinals=tuple(
                ordinals[path] for path in (*item.ancestors, item.step)
            ),
        )
        for item in items
    )


def step_label(
    step_ordinals: tuple[int, ...], items: tuple[PlanItem, ...]
) -> tuple[str, int]:
    """Return the ``Step N of M`` numbers of a step artifact.

    ``N`` is the step's dotted declaration ordinals, the ones its artifact
    path is built from, and ``M`` the number of top-level steps in the plan,
    so a nested step reads ``2.1 of 3``. A container counts once, and
    expanding items or children leaves the total alone. The step counts
    even when ``items``, a run's plan, no longer holds it.
    """
    return (
        ".".join(str(ordinal) for ordinal in step_ordinals),
        max(
            step_ordinals[0],
            *(entry.step_ordinals[0] for entry in items if entry.step_ordinals),
        ),
    )


@dataclass(frozen=True)
class WorkflowPlan:
    workflow: str
    workflow_description: str
    agent: str
    task_id: str | None
    modes: tuple[str, ...]
    handoff: bool
    items: tuple[PlanItem, ...]
    # The root documents, frozen with the plan so a run resolves their paths
    # without reading the configuration again.
    documents: tuple[DocumentDefinition, ...] = ()
    # Offered to the operator when the run completes; frozen with the plan so
    # a later configuration change does not alter a finished run's page.
    recommended_next_workflow: str | None = None
    # The workflow whose project handling the run takes (``hooks_from``),
    # frozen so extensions key their settings by the lane the run started on.
    hooks_from: str | None = None

    @property
    def lane(self) -> str:
        """The workflow extensions key their settings by: the lane, else this."""
        return self.hooks_from or self.workflow

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "workflow": self.workflow,
            "workflow_description": self.workflow_description,
            "agent": self.agent,
            "task_id": self.task_id,
            "modes": list(self.modes),
            "handoff": self.handoff,
            "items": [item.to_dict() for item in self.items],
        }
        if self.documents:
            data["documents"] = [document.to_dict() for document in self.documents]
        if self.recommended_next_workflow is not None:
            data["recommended_next_workflow"] = self.recommended_next_workflow
        if self.hooks_from is not None:
            data["hooks_from"] = self.hooks_from
        return data
