# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed planning contracts for composite workflow constructs.

The normalized workflow definition deliberately remains the source of truth for
YAML parsing and validation.  This module makes the compiler's second-stage
choice explicit: a definition is normalized into one construct input, then a
registered planner requests ordinary leaf items, boundaries, or nested regions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, TypeVar, runtime_checkable

from ww.actions import DefinedAction
from ww.contracts import (
    DEFAULT_LOOP_ASSIGNMENT,
    ChildOperation,
    ItemAssignment,
    ItemOperation,
    LoopAssignment,
    LoopOperation,
)
from ww.errors import ConfigurationError
from ww.operations import ChildWorkflowRun
from ww.workflow_config import (
    ChildFlow,
    HandlerDefinition,
    ItemFlow,
    StepDefinition,
    WorkflowDefinition,
)


@dataclass(frozen=True)
class ItemAnnotations:
    """Construct-selected annotations for one emitted plan item."""

    item_operation: ItemOperation | None = None
    child_operation: ChildOperation | None = None
    assessment_question: str | None = None
    assessment_outcomes: tuple[str, ...] = ()
    assessment_stops: tuple[str, ...] = ()
    assessment_parent: str | None = None
    assessment_outcome: str | None = None
    # Carried by every per-item stage and hook of an ``items`` step.
    item_assignment: ItemAssignment | None = None
    # Carried by every body step and hook of the nearest enclosing ``loop``.
    loop_id: str | None = None
    loop_assignment: LoopAssignment | None = None
    # Carried only by the collection item itself.
    split_instruction: str | None = None
    shared_items: bool = False
    item_identity: str | None = None
    item_unique: tuple[str, ...] = ()


EMPTY_ITEM_ANNOTATIONS = ItemAnnotations()


@dataclass(frozen=True)
class PlanningScope:
    """Immutable logical scope shared by a construct planner and core."""

    workflow: WorkflowDefinition
    step: StepDefinition
    path: str
    parent: str | None
    ancestors: tuple[str, ...]
    available_values: tuple[str, ...]
    item_template: bool
    annotations: ItemAnnotations = EMPTY_ITEM_ANNOTATIONS


@dataclass(frozen=True)
class ExpansionResult:
    """Explicit variable result from a construct expansion.

    ``available_values`` replaces the enclosing available scope for containers
    whose bodies intentionally propagate values (sequences and loops).  Leaf,
    assessment, and item-template planners instead return only direct outputs.
    """

    outputs: tuple[str, ...] = ()
    available_values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class LeafRequest:
    reference: HandlerDefinition
    action: DefinedAction | None = None
    annotations: ItemAnnotations = EMPTY_ITEM_ANNOTATIONS


@dataclass(frozen=True)
class LoopBoundaryRequest:
    operation: LoopOperation
    max_times: int
    artifact: bool


class PlanningContext(Protocol):
    """Small set of compiler-owned primitives exposed to construct planners."""

    scope: PlanningScope
    default_loop_max_times: int

    def derive_scope(
        self,
        *,
        parent: str,
        parent_ancestors: tuple[str, ...],
        item_template: bool | None = None,
    ) -> PlanningScope: ...

    def compile_steps(
        self, steps: tuple[StepDefinition, ...], scope: PlanningScope
    ) -> tuple[str, ...]: ...

    def compile_scoped_region(
        self,
        steps: tuple[StepDefinition, ...],
        scope: PlanningScope,
        annotations: ItemAnnotations,
    ) -> tuple[str, ...]: ...

    def emit_leaf(self, request: LeafRequest) -> tuple[str, ...]: ...

    def emit_loop_boundary(self, request: LoopBoundaryRequest) -> None: ...


DefinitionT = TypeVar("DefinitionT", contravariant=True)


@runtime_checkable
class ConstructPlanner(Protocol[DefinitionT]):
    def expand(
        self, definition: DefinitionT, context: PlanningContext
    ) -> ExpansionResult: ...


class ConstructPlannerRegistry:
    """Checked, internal dispatch table for normalized construct inputs."""

    def __init__(self) -> None:
        self._planners: dict[type[object], ConstructPlanner[object]] = {}

    def register(
        self, definition_type: type[DefinitionT], planner: ConstructPlanner[DefinitionT]
    ) -> None:
        if not isinstance(planner, ConstructPlanner):
            raise TypeError("construct planner must implement expand")
        if definition_type in self._planners:
            raise ValueError(f"construct planner already registered: {definition_type}")
        self._planners[definition_type] = planner  # type: ignore[assignment]

    def expand(self, definition: object, context: PlanningContext) -> ExpansionResult:
        try:
            planner = self._planners[type(definition)]
        except KeyError as error:
            raise ConfigurationError(
                f"no construct planner registered for {type(definition).__name__}"
            ) from error
        return planner.expand(definition, context)


@dataclass(frozen=True)
class LeafDefinition:
    step: StepDefinition
    annotations: ItemAnnotations


@dataclass(frozen=True)
class SequenceDefinition:
    step: StepDefinition
    body: tuple[StepDefinition, ...]


@dataclass(frozen=True)
class AssessmentDefinition:
    step: StepDefinition
    question: str
    outcomes: tuple[StepDefinition, ...]


@dataclass(frozen=True)
class ItemFlowDefinition:
    step: StepDefinition
    flow: ItemFlow


@dataclass(frozen=True)
class LoopDefinition:
    step: StepDefinition
    body: tuple[StepDefinition, ...]
    max_times: int | None
    assignment: LoopAssignment | None = None


@dataclass(frozen=True)
class ChildFlowDefinition:
    step: StepDefinition
    flow: ChildFlow


# The leaf under a ``children`` step that runs the collected children.
CHILDREN_RUN_STEP_NAME = "children"


class LeafPlanner(ConstructPlanner[LeafDefinition]):
    def expand(
        self, definition: LeafDefinition, context: PlanningContext
    ) -> ExpansionResult:
        return ExpansionResult(
            outputs=context.emit_leaf(
                LeafRequest(definition.step, annotations=definition.annotations)
            )
        )


class SequencePlanner(ConstructPlanner[SequenceDefinition]):
    def expand(
        self, definition: SequenceDefinition, context: PlanningContext
    ) -> ExpansionResult:
        scope = context.derive_scope(
            parent=context.scope.path,
            parent_ancestors=context.scope.ancestors,
        )
        return ExpansionResult(
            available_values=context.compile_steps(definition.body, scope)
        )


class AssessmentPlanner(ConstructPlanner[AssessmentDefinition]):
    def expand(
        self, definition: AssessmentDefinition, context: PlanningContext
    ) -> ExpansionResult:
        context.emit_leaf(
            LeafRequest(
                definition.step,
                annotations=ItemAnnotations(
                    assessment_question=definition.question,
                    assessment_outcomes=tuple(
                        outcome.name for outcome in definition.outcomes
                    ),
                    assessment_stops=tuple(
                        outcome.name
                        for outcome in definition.outcomes
                        if outcome.stop_workflow
                    ),
                ),
            )
        )
        scope = context.derive_scope(
            parent=context.scope.path,
            parent_ancestors=context.scope.ancestors,
        )
        for outcome in definition.outcomes:
            if outcome.stop_workflow:
                # Selecting it completes the workflow; there is nothing to run.
                continue
            context.compile_scoped_region(
                (outcome,),
                scope,
                ItemAnnotations(
                    assessment_parent=context.scope.path,
                    assessment_outcome=outcome.name,
                ),
            )
        return ExpansionResult()


class ItemFlowPlanner(ConstructPlanner[ItemFlowDefinition]):
    """Collect items with the step's own action, then template each stage.

    The templates are replaced by one concrete lifecycle per collected item
    when collection completes.
    """

    def expand(
        self, definition: ItemFlowDefinition, context: PlanningContext
    ) -> ExpansionResult:
        flow = definition.flow
        outputs = context.emit_leaf(
            LeafRequest(
                definition.step,
                annotations=ItemAnnotations(
                    item_operation="collect",
                    split_instruction=flow.description,
                    shared_items=flow.shared,
                    item_identity=flow.identity,
                    item_unique=flow.unique,
                ),
            )
        )
        if flow.steps:
            scope = context.derive_scope(
                parent=f"{context.scope.path}/{{item}}",
                parent_ancestors=(*context.scope.ancestors, context.scope.path),
                item_template=True,
            )
            scope = replace(
                scope,
                annotations=replace(scope.annotations, item_assignment=flow.assignment),
            )
            context.compile_steps(flow.steps, scope)
        return ExpansionResult(outputs=outputs)


class LoopPlanner(ConstructPlanner[LoopDefinition]):
    def expand(
        self, definition: LoopDefinition, context: PlanningContext
    ) -> ExpansionResult:
        max_times = (
            definition.max_times
            if definition.max_times is not None
            else context.default_loop_max_times
        )
        context.emit_loop_boundary(
            LoopBoundaryRequest("enter", max_times, definition.step.artifact)
        )
        scope = context.derive_scope(
            parent=context.scope.path,
            parent_ancestors=context.scope.ancestors,
        )
        scope = replace(
            scope,
            annotations=replace(
                scope.annotations,
                loop_id=context.scope.path,
                loop_assignment=definition.assignment or DEFAULT_LOOP_ASSIGNMENT,
            ),
        )
        available = context.compile_steps(definition.body, scope)
        context.emit_loop_boundary(LoopBoundaryRequest("repeat", max_times, False))
        return ExpansionResult(available_values=available)


class ChildFlowPlanner(ConstructPlanner[ChildFlowDefinition]):
    """Collect child tasks with the step's own action, then run them.

    The run is a ww-owned leaf nested under the collecting step, so one step
    owns the whole child lifecycle; the step's completion hooks follow it.
    """

    def expand(
        self, definition: ChildFlowDefinition, context: PlanningContext
    ) -> ExpansionResult:
        flow = definition.flow
        outputs = context.emit_leaf(
            LeafRequest(
                definition.step,
                annotations=ItemAnnotations(
                    child_operation="collect", split_instruction=flow.description
                ),
            )
        )
        run = StepDefinition(
            name=CHILDREN_RUN_STEP_NAME,
            description=(
                f"Run every child task with the `{flow.workflow}` workflow, "
                "one at a time."
            ),
            operation=ChildWorkflowRun(flow.workflow),
        )
        context.compile_steps(
            (run,),
            context.derive_scope(
                parent=context.scope.path,
                parent_ancestors=context.scope.ancestors,
            ),
        )
        return ExpansionResult(outputs=outputs)


def step_annotations(step: StepDefinition) -> ItemAnnotations:
    """Return the operation annotations inherited by a step and its hooks.

    Child collection is not among them: only the collect leaf of a
    ``children`` step collects, while its hooks and the workflow's completion
    items around it do not.
    """
    return ItemAnnotations(item_operation=step.item_operation)


def normalize_construct(step: StepDefinition) -> object:
    """Translate existing normalized steps at one deliberate dispatch boundary."""
    if step.assessment_question is not None:
        return AssessmentDefinition(
            step, step.assessment_question, step.assessment_outcomes
        )
    if step.items is not None:
        return ItemFlowDefinition(step, step.items)
    if step.loop_steps:
        return LoopDefinition(
            step, step.loop_steps, step.loop_max_times, step.loop_assignment
        )
    if step.child_steps:
        return SequenceDefinition(step, step.child_steps)
    if step.children is not None:
        return ChildFlowDefinition(step, step.children)
    return LeafDefinition(step, step_annotations(step))


def builtin_construct_planners() -> ConstructPlannerRegistry:
    registry = ConstructPlannerRegistry()
    registry.register(LeafDefinition, LeafPlanner())
    registry.register(SequenceDefinition, SequencePlanner())
    registry.register(AssessmentDefinition, AssessmentPlanner())
    registry.register(ItemFlowDefinition, ItemFlowPlanner())
    registry.register(LoopDefinition, LoopPlanner())
    registry.register(ChildFlowDefinition, ChildFlowPlanner())
    return registry
