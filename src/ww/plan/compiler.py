# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile normalized workflow definitions into a flat, inspectable plan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ww.actions import (
    DefinedAction,
    PlannedAction,
    Prompt,
    actions,
)
from ww.contracts import (
    ExecutionKind,
    HookPhase,
    PlanItemKind,
    PlanItemOwner,
    PlanItemPhase,
)
from ww.control import child_workflow, workflow_transition
from ww.discovery import AgentDiscovery
from ww.errors import ConfigurationError
from ww.extensions import ExtensionRegistry, is_extension_reference
from ww.interpolation import dependencies
from ww.operations import ChildWorkflowRun, LoopBoundary, PlanOperation, WorkflowHandoff
from ww.project_config import ProjectConfig
from ww.variables import CORE_VARIABLE_NAMES, compile_variable_values
from ww.workflow_config import (
    INIT_STEP_NAME,
    HandlerDefinition,
    ProvidedVariable,
    StepDefinition,
    WorkflowConfiguration,
    WorkflowDefinition,
    binds_task_identity,
)
from ww.workflow_validation import implicit_init_step, validate_configuration
from ww.workspace import relative_workspace

from .actions import ActionResolver
from .constructs import (
    EMPTY_ITEM_ANNOTATIONS,
    ConstructPlannerRegistry,
    ItemAnnotations,
    LeafRequest,
    LoopBoundaryRequest,
    PlanningContext,
    PlanningScope,
    builtin_construct_planners,
    normalize_construct,
    step_annotations,
)
from .models import PlanItem, WorkflowPlan, number_step_paths


@dataclass(frozen=True)
class ExecutionHints:
    """The worker shape a step requests: agent, model, reasoning, and profile.

    Every field is inherited along the same chain, workflow, then each
    enclosing step, then the step itself, so a loop wrapper or a parent step
    can set the profile for its whole body.
    """

    agent: str
    model: str = "auto"
    reasoning: str = "auto"
    profile: str | None = None
    profile_description: str | None = None

    def overlay(self, value: HandlerDefinition | WorkflowDefinition) -> ExecutionHints:
        agent = value.agent if value.agent is not None else self.agent
        model = self.model
        reasoning = self.reasoning
        if value.model is not None:
            if value.reasoning is None and value.model != model:
                reasoning = "auto"
            model = value.model
        if value.reasoning is not None:
            reasoning = value.reasoning
        profile, profile_description = self.profile, self.profile_description
        # Hooks, handlers, and modes cannot declare profiles.
        if isinstance(value, StepDefinition | WorkflowDefinition):
            if value.profile is not None:
                profile = value.profile
            if value.profile_description is not None:
                profile_description = value.profile_description
        return ExecutionHints(agent, model, reasoning, profile, profile_description)


@dataclass(frozen=True)
class PlanCompilationOptions:
    """Context-dependent choices applied while producing the final plan.

    ``task_id`` makes that ID authoritative and removes it from agent-provided
    values. ``completed_bootstrap_step`` omits an identity action that already
    ran before the durable task was created, while retaining normal start hooks.
    Keeping both transformations here gives ``plan`` and ``start`` one plan
    interpretation instead of service-only rewriting.
    """

    task_id: str | None = None
    completed_bootstrap_step: str | None = None


@dataclass(frozen=True)
class _CompilerPlanningContext(PlanningContext):
    """Compiler-private implementation of the construct planning primitives."""

    scope: PlanningScope
    default_loop_max_times: int
    _compile: Callable[[tuple[StepDefinition, ...], PlanningScope], tuple[str, ...]]
    _compile_region: Callable[
        [tuple[StepDefinition, ...], PlanningScope, ItemAnnotations], tuple[str, ...]
    ]
    _emit_leaf: Callable[[LeafRequest], tuple[str, ...]]
    _emit_boundary: Callable[[LoopBoundaryRequest], None]

    def derive_scope(
        self,
        *,
        parent: str,
        parent_ancestors: tuple[str, ...],
        item_template: bool | None = None,
    ) -> PlanningScope:
        return replace(
            self.scope,
            parent=parent,
            ancestors=parent_ancestors,
            item_template=(
                self.scope.item_template if item_template is None else item_template
            ),
        )

    def compile_steps(
        self, steps: tuple[StepDefinition, ...], scope: PlanningScope
    ) -> tuple[str, ...]:
        return self._compile(steps, scope)

    def compile_scoped_region(
        self,
        steps: tuple[StepDefinition, ...],
        scope: PlanningScope,
        annotations: ItemAnnotations,
    ) -> tuple[str, ...]:
        return self._compile_region(steps, scope, annotations)

    def emit_leaf(self, request: LeafRequest) -> tuple[str, ...]:
        return self._emit_leaf(request)

    def emit_loop_boundary(self, request: LoopBoundaryRequest) -> None:
        self._emit_boundary(request)


class WorkflowPlanCompiler:
    """Resolve a selected workflow into exactly the actions it will perform."""

    def __init__(
        self,
        configuration: WorkflowConfiguration,
        root: Path,
        agent: str,
        task_id: str | None = None,
        extensions: ExtensionRegistry | None = None,
        options: PlanCompilationOptions | None = None,
        project_config: ProjectConfig | None = None,
        construct_planners: ConstructPlannerRegistry | None = None,
        construct_normalizer: Callable[[StepDefinition], object] = normalize_construct,
    ) -> None:
        self.configuration = validate_configuration(configuration, extensions)
        self.root = root
        self.agent = agent
        if options is not None and task_id is not None and options.task_id != task_id:
            raise ValueError("task_id and compilation options disagree")
        self.options = options or PlanCompilationOptions(task_id=task_id)
        self.task_id = self.options.task_id
        self.extensions = extensions
        self.project_config = project_config or ProjectConfig()
        self.discovery = AgentDiscovery(root)
        self.available = self.discovery.available(agent)
        self.builtins = compile_variable_values(
            tuple(item.name for item in self.configuration.workflows), self.task_id
        )
        self.actions = ActionResolver(
            self.configuration,
            self.agent,
            self.extensions,
            self.available,
            self.builtins,
        )
        self.construct_planners = construct_planners or builtin_construct_planners()
        self.construct_normalizer = construct_normalizer

    def compile(self, workflow_name: str) -> WorkflowPlan:
        try:
            workflow = self.configuration.workflows_by_name[workflow_name]
        except KeyError as error:
            raise ConfigurationError(f"workflow not found: {workflow_name}") from error
        effective_steps = (implicit_init_step(), *workflow.steps)
        items: list[PlanItem] = []
        workflow_hints = ExecutionHints(self.agent).overlay(workflow)
        start_values = self._append_hooks(
            items,
            workflow,
            effective_steps[0],
            INIT_STEP_NAME,
            None,
            "before_start_workflow",
            (),
            boundary_hints=workflow_hints,
        )
        available_values = self._compile_steps(
            items,
            workflow,
            effective_steps,
            None,
            start_values,
            parent_hints=workflow_hints,
        )
        transition = (
            items.pop()
            if workflow.handoff and items and workflow_transition(items[-1]) is not None
            else None
        )
        terminal_step = effective_steps[-1]
        self._append_hooks(
            items,
            workflow,
            terminal_step,
            terminal_step.name,
            None,
            "before_complete_workflow",
            (),
            available_values,
            boundary_hints=workflow_hints,
        )
        if transition is not None:
            items.append(transition)
        if not workflow.handoff:
            self._append_handler(
                items,
                workflow,
                terminal_step,
                terminal_step.name,
                None,
                "before_complete_workflow",
                "internal",
                HandlerDefinition(
                    "update-workflow-summary",
                    description=(
                        "Provide a concise summary of this workflow run's "
                        "goal and result."
                    ),
                    action=DefinedAction(
                        "prompt",
                        Prompt(
                            "Provide a concise summary of this workflow run's "
                            "goal and result."
                        ),
                    ),
                    provide=(
                        ProvidedVariable(
                            "summary",
                            "A short goal/result summary for the task workflow ledger.",
                        ),
                    ),
                ),
                (),
                summary=True,
                boundary_hints=ExecutionHints(
                    self.agent,
                    **self.project_config.builtin_settings("workflow_summary"),
                ),
                annotations=step_annotations(terminal_step),
            )
        if workflow.handoff and (not items or workflow_transition(items[-1]) is None):
            raise ConfigurationError(
                f"handoff workflow {workflow.name!r} must end with a "
                "workflow transition"
            )
        items = self._mark_child_identity(items)
        plan = WorkflowPlan(
            workflow=workflow.name,
            workflow_description=workflow.description,
            agent=self.agent,
            task_id=self.task_id,
            modes=workflow.modes,
            handoff=workflow.handoff,
            items=number_step_paths(tuple(items)),
            documents=self.configuration.documents,
            recommended_next_workflow=workflow.recommended_next_workflow,
        )
        return self._apply_options(plan)

    def _mark_child_identity(self, items: list[PlanItem]) -> list[PlanItem]:
        """Flag the children collection when the child workflow binds its ID."""
        target = next(
            (
                coordinator.workflow
                for coordinator in map(child_workflow, items)
                if coordinator is not None
            ),
            None,
        )
        if target is None:
            return items
        child = self.configuration.workflows_by_name.get(target)
        if child is None or not binds_task_identity(child):
            return items
        return [
            replace(item, child_identity=True)
            if item.child_operation == "collect"
            else item
            for item in items
        ]

    def _apply_options(self, plan: WorkflowPlan) -> WorkflowPlan:
        completed = self.options.completed_bootstrap_step
        if completed is not None:
            if self.options.task_id is None:
                raise ConfigurationError(
                    "a completed bootstrap step requires an authoritative task ID"
                )
            if not any(
                item.step == completed and item.phase == "step" for item in plan.items
            ):
                raise ConfigurationError(
                    f"completed bootstrap step not found in plan: {completed}"
                )
        retained = tuple(
            item
            for item in plan.items
            if completed is None
            or item.step != completed
            or item.phase == "before_start_workflow"
        )
        if self.options.task_id is not None:
            without_task_id = []
            for item in retained:
                provide = tuple(
                    value for value in item.provide if value.name != "task_id"
                )
                without_task_id.append(
                    replace(
                        item,
                        provide=provide,
                        requires_agent_input=(
                            item.execution == "automatic" and bool(provide)
                        ),
                    )
                )
            retained = tuple(without_task_id)
        return replace(
            plan,
            items=tuple(
                replace(item, position=index) for index, item in enumerate(retained, 1)
            ),
        )

    def _compile_steps(
        self,
        items: list[PlanItem],
        workflow: WorkflowDefinition,
        steps: tuple[StepDefinition, ...],
        parent: str | None,
        available_values: tuple[str, ...],
        item_template: bool = False,
        parent_ancestors: tuple[str, ...] = (),
        parent_hints: ExecutionHints | None = None,
        annotations: ItemAnnotations = EMPTY_ITEM_ANNOTATIONS,
    ) -> tuple[str, ...]:
        """Flatten a recursive step tree while retaining each logical lifecycle."""
        values = available_values
        for step in steps:
            inherited_hints = parent_hints or ExecutionHints(self.agent).overlay(
                workflow
            )
            step_hints = inherited_hints.overlay(step)
            action_hints = step_hints
            if step.name == INIT_STEP_NAME:
                settings = self.project_config.builtin_settings("init")
                action_hints = ExecutionHints(self.agent, **settings)
            path = f"{parent}/{step.name}" if parent else step.name
            ancestors = (*parent_ancestors, parent) if parent else ()
            scope = PlanningScope(
                workflow,
                step,
                path,
                parent,
                ancestors,
                values,
                item_template,
                annotations,
            )
            values = (
                *values,
                *self._append_hooks(
                    items,
                    workflow,
                    step,
                    path,
                    parent,
                    "before_in_progress",
                    values,
                    item_template=item_template,
                    ancestors=ancestors,
                    boundary_hints=step_hints,
                    annotations=annotations,
                ),
            )
            scope = replace(scope, available_values=values)

            def compile_nested(
                nested_steps: tuple[StepDefinition, ...],
                nested_scope: PlanningScope,
                current_hints: ExecutionHints = step_hints,
            ) -> tuple[str, ...]:
                return self._compile_steps(
                    items,
                    workflow,
                    nested_steps,
                    nested_scope.parent,
                    nested_scope.available_values,
                    nested_scope.item_template,
                    parent_ancestors=nested_scope.ancestors,
                    parent_hints=current_hints,
                    annotations=nested_scope.annotations,
                )

            def compile_region(
                nested_steps: tuple[StepDefinition, ...],
                nested_scope: PlanningScope,
                region_annotations: ItemAnnotations,
            ) -> tuple[str, ...]:
                start = len(items)
                result = compile_nested(
                    nested_steps,
                    replace(nested_scope, annotations=region_annotations),
                )
                for index in range(start, len(items)):
                    items[index] = _with_annotations(items[index], region_annotations)
                return result

            def emit_leaf(
                request: LeafRequest,
                current_step: StepDefinition = step,
                current_path: str = path,
                current_scope: PlanningScope = scope,
                current_ancestors: tuple[str, ...] = ancestors,
                current_action_hints: ExecutionHints = action_hints,
                current_item_template: bool = item_template,
                current_annotations: ItemAnnotations = annotations,
            ) -> tuple[str, ...]:
                return self._append_handler(
                    items,
                    workflow,
                    current_step,
                    current_path,
                    parent,
                    "step",
                    "step",
                    request.reference,
                    current_scope.available_values,
                    item_template=current_item_template,
                    ancestors=current_ancestors,
                    boundary_hints=current_action_hints,
                    action_override=request.action,
                    operation_override=request.operation,
                    annotations=_merge_annotations(
                        current_annotations, request.annotations
                    ),
                )

            def emit_boundary(
                request: LoopBoundaryRequest,
                current_step: StepDefinition = step,
                current_path: str = path,
                current_ancestors: tuple[str, ...] = ancestors,
                current_annotations: ItemAnnotations = annotations,
            ) -> None:
                self._append_loop_control(
                    items,
                    workflow,
                    current_step,
                    current_path,
                    parent,
                    current_ancestors,
                    request,
                    current_annotations,
                )

            context = _CompilerPlanningContext(
                scope,
                self.project_config.loop_max_times,
                compile_nested,
                compile_region,
                emit_leaf,
                emit_boundary,
            )
            expansion = self.construct_planners.expand(
                self.construct_normalizer(step), context
            )
            expanded_values = (
                expansion.available_values
                if expansion.available_values is not None
                else (*values, *expansion.outputs)
            )
            available_after = (
                *expanded_values,
                *(item.name for item in step.provide),
            )
            available_after = (
                *available_after,
                *self._append_hooks(
                    items,
                    workflow,
                    step,
                    path,
                    parent,
                    "before_complete",
                    (),
                    available_after,
                    item_template=item_template,
                    ancestors=ancestors,
                    boundary_hints=step_hints,
                    annotations=annotations,
                ),
            )
            available_after = (
                *available_after,
                *self._append_hooks(
                    items,
                    workflow,
                    step,
                    path,
                    parent,
                    "after_complete",
                    (),
                    available_after,
                    item_template=item_template,
                    ancestors=ancestors,
                    boundary_hints=step_hints,
                    annotations=annotations,
                ),
            )
            values = available_after
        return values

    def _append_loop_control(
        self,
        items: list[PlanItem],
        workflow: WorkflowDefinition,
        step: StepDefinition,
        path: str,
        parent: str | None,
        ancestors: tuple[str, ...],
        request: LoopBoundaryRequest,
        annotations: ItemAnnotations,
    ) -> None:
        """Add a manager-owned boundary around an otherwise ordinary step tree."""
        suffix = request.operation
        description = step.description or f"Run the {step.name!r} loop."
        items.append(
            PlanItem(
                id=f"{workflow.name}:{path}:loop:{suffix}:1",
                position=len(items) + 1,
                name=step.name,
                description=description,
                operation=LoopBoundary(path, request.operation, request.max_times),
                owner="ww",
                execution="loop_control",
                requires_agent_input=False,
                workflow=workflow.name,
                step=path,
                parent=parent,
                phase="step",
                source="step",
                registered_handler=None,
                artifact=request.artifact,
                ancestors=ancestors,
                loop_break=None,
                loop_continue=None,
                assessment_question=annotations.assessment_question,
                assessment_outcomes=annotations.assessment_outcomes,
                assessment_stops=annotations.assessment_stops,
                assessment_parent=annotations.assessment_parent,
                assessment_outcome=annotations.assessment_outcome,
            )
        )

    def _append_hooks(
        self,
        items: list[PlanItem],
        workflow: WorkflowDefinition,
        step: StepDefinition,
        step_path: str,
        parent: str | None,
        phase: HookPhase,
        before_variables: tuple[str, ...],
        after_variables: tuple[str, ...] = (),
        item_template: bool = False,
        ancestors: tuple[str, ...] = (),
        boundary_hints: ExecutionHints | None = None,
        annotations: ItemAnnotations = EMPTY_ITEM_ANNOTATIONS,
    ) -> tuple[str, ...]:
        available = (
            before_variables
            if phase in {"before_start_workflow", "before_in_progress"}
            else after_variables
        )
        hooks = (
            *(hook for hook in self.configuration.global_hooks if hook.phase == phase),
            *(hook for hook in workflow.hooks if hook.phase == phase),
            *(hook for hook in step.hooks if hook.phase == phase),
        )
        hook_annotations = _merge_annotations(annotations, step_annotations(step))
        produced: list[str] = []
        for hook in hooks:
            applies = hook.applies_to(
                workflow.name,
                step.name,
                step_path,
                _logical_step_paths(workflow.steps),
            )
            if applies:
                output_names = self._append_handler(
                    items,
                    workflow,
                    step,
                    step_path,
                    parent,
                    phase,
                    hook.scope,
                    hook.handler,
                    (*available, *produced),
                    item_template=item_template,
                    ancestors=ancestors,
                    boundary_hints=boundary_hints,
                    annotations=hook_annotations,
                )
                produced.extend(output_names)
        return tuple(produced)

    def _append_handler(
        self,
        items: list[PlanItem],
        workflow: WorkflowDefinition,
        step: StepDefinition,
        step_path: str,
        parent: str | None,
        phase: PlanItemPhase,
        source: str,
        reference: HandlerDefinition,
        available_variables: tuple[str, ...],
        summary: bool = False,
        item_template: bool = False,
        ancestors: tuple[str, ...] = (),
        boundary_hints: ExecutionHints | None = None,
        action_override: DefinedAction | None = None,
        operation_override: ChildWorkflowRun | None = None,
        annotations: ItemAnnotations = EMPTY_ITEM_ANNOTATIONS,
    ) -> tuple[str, ...]:
        handler, registered_name, definition = self.actions._handler(
            reference,
            resolve_reference=(
                phase != "step" or is_extension_reference(reference.name)
            ),
        )
        hints = boundary_hints or ExecutionHints(self.agent).overlay(workflow).overlay(
            step
        )
        if phase != "step" and definition is not None:
            hints = hints.overlay(definition)
        if phase != "step":
            hints = hints.overlay(reference)
        profile, profile_instruction, profile_path = (
            self._resolve_profile(hints.profile, hints.profile_description)
            if source == "step" and step.name != INIT_STEP_NAME
            else (None, None, None)
        )
        local = phase == "step" and (not step.subagents or step.interactive)
        if local:
            # An explicitly local step, or a conversation with the operator
            # that only the talking session can hold, has no worker shape.
            hints = ExecutionHints(self.agent)
            profile, profile_instruction, profile_path = (None, None, None)
        operation = operation_override or handler.operation
        action: DefinedAction | None = None
        owner: PlanItemOwner
        execution: ExecutionKind
        if operation is not None:
            if action_override is not None:
                raise ConfigurationError(
                    "a plan item cannot combine action and core operation"
                )
            kind, owner, execution = (
                operation.kind,
                operation.owner,
                operation.execution,
            )
        else:
            kind, owner, action = self.actions._resolve(handler)
            if action_override is not None:
                kind = action_override.identifier
                owner = actions.get(kind).owner
                action = action_override
            execution = _execution_kind(kind)
        if (
            phase == "step"
            and (step.loop_break is not None or step.loop_continue is not None)
            and owner != "agent"
        ):
            raise ConfigurationError(
                f"step {step.name!r} uses break/continue but is not agent-owned"
            )
        if handler.save_metadata and owner != "agent":
            raise ConfigurationError(
                f"handler {handler.name!r} can save metadata only when agent-owned"
            )
        if handler.update_document and owner != "agent":
            raise ConfigurationError(
                f"handler {handler.name!r} can update documents only when agent-owned"
            )
        allowed = {
            "__workflows",
            "__task_id",
            *CORE_VARIABLE_NAMES,
            *available_variables,
            *(item.name for item in handler.provide),
            *(
                f"documents.{document.name}"
                for document in self.configuration.documents
            ),
        }
        if isinstance(operation, WorkflowHandoff):
            operation = WorkflowHandoff(
                self.actions._interpolate(operation.target, allowed)
            )
        compiled_operation: PlanOperation
        if operation is not None:
            compiled_operation = operation
        else:
            assert action is not None
            planned_payload = self.actions.plan_action(action, allowed)
            compiled_operation = PlannedAction(kind, planned_payload)
        description = (
            self.actions._interpolate(handler.description, allowed)
            if handler.description
            else ""
        )
        dependency_names = tuple(
            dict.fromkeys(
                name
                for value in (
                    *(
                        actions.get(action.identifier).templates(action.payload)
                        if action is not None
                        else ()
                    ),
                    *(
                        (operation.target,)
                        if isinstance(operation, WorkflowHandoff)
                        else ()
                    ),
                    handler.description,
                )
                if value is not None
                for name in dependencies(value)
            )
        )
        ordinal = (
            sum(
                1
                for item in items
                if item.workflow == workflow.name
                and item.step == step_path
                and item.phase == phase
                and item.source == source
            )
            + 1
        )
        item_id = f"{workflow.name}:{step_path}:{phase}:{source}:{ordinal}"
        items.append(
            PlanItem(
                id=item_id,
                position=len(items) + 1,
                name=handler.name,
                description=description,
                operation=compiled_operation,
                owner=owner,
                execution=execution,
                requires_agent_input=execution == "automatic" and bool(handler.provide),
                workflow=workflow.name,
                step=step_path,
                parent=parent,
                phase=phase,
                source=source,
                registered_handler=registered_name,
                provide=handler.provide,
                save_metadata=handler.save_metadata,
                update_document=handler.update_document,
                update_item=handler.update_item,
                outputs=handler.outputs,
                dependencies=dependency_names,
                requested_agent=hints.agent if not local else None,
                requested_model=hints.model if not local else None,
                requested_reasoning=hints.reasoning if not local else None,
                subagents=not local if phase == "step" else step.subagents,
                interactive=step.interactive and phase == "step",
                choices=step.choices if phase == "step" else (),
                ui=step.ui and phase == "step",
                # Explicit retained aliases for plan schemas <= 3.
                model=hints.model if not local else None,
                reasoning=hints.reasoning if not local else None,
                profile=profile,
                profile_instruction=profile_instruction,
                profile_path=profile_path,
                summary=summary,
                item_operation=annotations.item_operation,
                item_template=item_template,
                item_assignment=annotations.item_assignment or "per_step",
                loop_id=annotations.loop_id,
                loop_assignment=annotations.loop_assignment,
                split_instruction=annotations.split_instruction,
                shared_items=annotations.shared_items,
                item_identity=annotations.item_identity,
                item_unique=annotations.item_unique,
                artifact=step.artifact,
                child_operation=annotations.child_operation,
                ancestors=ancestors,
                artifact_dependency=(
                    _artifact_dependency_path(
                        step.artifact_dependency, ancestors, items
                    )
                    if phase == "step" and step.artifact_dependency is not None
                    else None
                ),
                loop_break=step.loop_break if phase == "step" else None,
                loop_continue=step.loop_continue if phase == "step" else None,
                assessment_question=annotations.assessment_question,
                assessment_outcomes=annotations.assessment_outcomes,
                assessment_stops=annotations.assessment_stops,
                assessment_parent=annotations.assessment_parent,
                assessment_outcome=annotations.assessment_outcome,
            )
        )
        return (
            *(item.name for item in handler.provide),
            *handler.outputs,
        )

    def _resolve_profile(
        self, name: str | None, description: str | None
    ) -> tuple[str | None, str | None, str | None]:
        """Return the profile name, its configured text, and its file.

        A project-local profile file is recorded relative to the project root,
        so a plan compiled on one filesystem prints the right path on another.
        """
        if name is not None:
            profile_path = self.discovery.profile(self.agent, name)
            if profile_path is not None:
                return name, None, relative_workspace(self.root, profile_path)
        configured = self.configuration.profiles_by_name.get(name) if name else None
        text = description or (configured.description if configured else None)
        if text:
            return name, text, None
        if name:
            return name, f"Use the `{name}` profile.", None
        return None, None, None


def compile_workflow_plan(
    configuration: WorkflowConfiguration,
    root: Path,
    workflow: str,
    agent: str,
    task_id: str | None = None,
    extensions: ExtensionRegistry | None = None,
    options: PlanCompilationOptions | None = None,
    project_config: ProjectConfig | None = None,
) -> WorkflowPlan:
    return WorkflowPlanCompiler(
        configuration, root, agent, task_id, extensions, options, project_config
    ).compile(workflow)


def _execution_kind(kind: PlanItemKind) -> ExecutionKind:
    return actions.get(kind).execution


def _merge_annotations(
    inherited: ItemAnnotations, emitted: ItemAnnotations
) -> ItemAnnotations:
    """Apply a construct's local choices without losing scoped outcome tags."""
    return ItemAnnotations(
        item_operation=(
            emitted.item_operation
            if emitted.item_operation is not None
            else inherited.item_operation
        ),
        child_operation=(
            emitted.child_operation
            if emitted.child_operation is not None
            else inherited.child_operation
        ),
        assessment_question=(
            emitted.assessment_question
            if emitted.assessment_question is not None
            else inherited.assessment_question
        ),
        assessment_outcomes=(
            emitted.assessment_outcomes
            if emitted.assessment_outcomes
            else inherited.assessment_outcomes
        ),
        assessment_stops=(
            emitted.assessment_stops
            if emitted.assessment_outcomes
            else inherited.assessment_stops
        ),
        assessment_parent=(
            emitted.assessment_parent
            if emitted.assessment_parent is not None
            else inherited.assessment_parent
        ),
        assessment_outcome=(
            emitted.assessment_outcome
            if emitted.assessment_outcome is not None
            else inherited.assessment_outcome
        ),
        item_assignment=(
            emitted.item_assignment
            if emitted.item_assignment is not None
            else inherited.item_assignment
        ),
        loop_id=emitted.loop_id if emitted.loop_id is not None else inherited.loop_id,
        loop_assignment=(
            emitted.loop_assignment
            if emitted.loop_assignment is not None
            else inherited.loop_assignment
        ),
        split_instruction=emitted.split_instruction,
        shared_items=emitted.shared_items,
        item_identity=emitted.item_identity,
        item_unique=emitted.item_unique,
    )


def _with_annotations(item: PlanItem, annotations: ItemAnnotations) -> PlanItem:
    """Overlay a scoped region exactly as the former region pass did."""
    return replace(
        item,
        assessment_parent=annotations.assessment_parent,
        assessment_outcome=annotations.assessment_outcome,
    )


def _artifact_dependency_path(
    name: str, ancestors: tuple[str, ...], items: list[PlanItem]
) -> str:
    """Return the plan path of the nearest earlier step named ``name``.

    Validation has already chosen the step: an earlier sibling, else an
    earlier step of the nearest enclosing level.  The same search over the
    already-compiled items gives its path, skipping a loop that is still
    running around the dependent step.
    """
    earlier = {item.step: item for item in items if item.phase == "step"}
    for container in (*reversed(ancestors), None):
        path = f"{container}/{name}" if container else name
        found = earlier.get(path)
        if found is not None and not (path in ancestors and found.kind == "loop"):
            return path
    return name


def _logical_step_paths(
    steps: tuple[StepDefinition, ...], parent: str | None = None
) -> frozenset[str]:
    paths: set[str] = set()
    for step in steps:
        path = f"{parent}/{step.name}" if parent else step.name
        paths.add(path)
        item_steps = step.items.steps if step.items is not None else ()
        for nested in (step.child_steps, step.loop_steps, item_steps):
            paths.update(_logical_step_paths(nested, path))
    return frozenset(paths)
