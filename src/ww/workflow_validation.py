# SPDX-License-Identifier: GPL-3.0-or-later
"""Notation-independent validation for normalized workflow definitions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from ww.actions import DefinedAction, Prompt
from ww.core_workflows import with_core_workflows
from ww.errors import ConfigurationError
from ww.extensions import ExtensionRegistry, is_extension_reference
from ww.operations import ChildWorkflowRun, WorkflowHandoff
from ww.project_config import ProjectConfig
from ww.validation import is_positive_int
from ww.workflow_config import (
    INIT_STEP_NAME,
    INIT_STEP_PROMPT,
    HandlerDefinition,
    HookDefinition,
    StepDefinition,
    WorkflowConfiguration,
    WorkflowDefinition,
)


def implicit_init_step() -> StepDefinition:
    """Return the reserved, artifact-producing first step of every workflow."""
    return StepDefinition(
        name=INIT_STEP_NAME,
        description=INIT_STEP_PROMPT,
        action=DefinedAction("prompt", Prompt(INIT_STEP_PROMPT)),
    )


_HOOK_PHASES = {
    "before_start_workflow",
    "before_in_progress",
    "before_complete",
    "after_complete",
    "before_complete_workflow",
}


def validate_configuration(
    configuration: WorkflowConfiguration,
    extensions: ExtensionRegistry | None = None,
) -> WorkflowConfiguration:
    """Validate and return the normalized contract shared by every frontend.

    A frontend is responsible only for translating its notation into the
    immutable definitions in :mod:`ww.workflow_config`. Cross-definition rules
    live here so YAML and future frontends cannot acquire different semantics.
    Extension modes are also resolved here because they are part of the
    normalized configuration consumed by catalogs and the compiler.
    """
    configuration = with_core_workflows(
        configuration,
        extensions.config if extensions is not None else ProjectConfig(),
    )
    if not configuration.workflows:
        raise ConfigurationError("configuration must define at least one workflow")

    _unique((item.name for item in configuration.modes), "mode")
    _unique((item.name for item in configuration.profiles), "profile")
    _unique((item.name for item in configuration.documents), "document")
    _validate_document_updates(configuration)
    _unique((item.name for item in configuration.handlers), "handler")
    _unique((item.name for item in configuration.workflows), "workflow")

    modes = configuration.modes
    if extensions is not None:
        extensions.validate_configuration()
        references = tuple(
            mode
            for workflow in configuration.workflows
            for mode in workflow.modes
            if is_extension_reference(mode)
        )
        existing = {mode.name for mode in modes}
        contributed = tuple(
            extensions.mode(name)
            for name in dict.fromkeys(references)
            if name not in existing
        )
        modes = (*modes, *contributed)
        _unique((item.name for item in modes), "mode")

    normalized = replace(configuration, modes=modes)
    known_modes = {mode.name for mode in normalized.modes}
    known_workflows = {workflow.name for workflow in normalized.workflows}
    for handler in normalized.handlers:
        _validate_execution_hints(handler, f"handler {handler.name!r}")
        if isinstance(handler, StepDefinition):
            _validate_steps(handler.name, (handler,))
    for workflow in normalized.workflows:
        _validate_execution_hints(workflow, f"workflow {workflow.name!r}")
        unknown_modes = set(workflow.modes) - known_modes
        if unknown_modes:
            raise ConfigurationError(
                f"workflow {workflow.name!r} references unknown mode(s): "
                + ", ".join(sorted(unknown_modes))
            )
        _validate_steps(workflow.name, workflow.steps, top_level=True)
        _validate_item_flows(workflow.name, workflow.steps)
        _validate_transitions(workflow)
        _validate_hooks(
            workflow.hooks,
            {INIT_STEP_NAME, *_step_filter_references(workflow.steps)},
            expected_scope="workflow",
        )

    all_steps = {
        INIT_STEP_NAME,
        *set().union(
            *(
                _step_filter_references(workflow.steps)
                for workflow in normalized.workflows
            )
        ),
    }
    _validate_hooks(
        normalized.global_hooks,
        all_steps,
        known_workflows,
        expected_scope="global",
    )
    _validate_workflow_boundary_hooks(normalized)
    _validate_child_tasks(normalized.workflows)
    return normalized


def _validate_steps(
    workflow_name: str,
    steps: tuple[StepDefinition, ...],
    *,
    top_level: bool = False,
    inside_loop: bool = False,
) -> None:
    _unique((step.name for step in steps), f"step in workflow {workflow_name!r}")
    prior: dict[str, StepDefinition] = (
        {INIT_STEP_NAME: implicit_init_step()} if top_level else {}
    )
    for step in steps:
        _validate_execution_hints(step, f"step {step.name!r}")
        if step.name == INIT_STEP_NAME:
            raise ConfigurationError(
                f"step name {INIT_STEP_NAME!r} is reserved and must not be declared"
            )
        if (
            step.loop_break is not None or step.loop_continue is not None
        ) and not inside_loop:
            control = "break" if step.loop_break is not None else "continue"
            raise ConfigurationError(
                f"step {step.name!r} in workflow {workflow_name!r} uses "
                f"{control} outside a loop"
            )
        if step.loop_max_times is not None and (
            not is_positive_int(step.loop_max_times)
        ):
            raise ConfigurationError(
                f"step {step.name!r} in workflow {workflow_name!r} has an invalid "
                "loop_max_times; expected a positive integer"
            )
        if step.loop_max_times is not None and not step.loop_steps:
            raise ConfigurationError(
                f"step {step.name!r} in workflow {workflow_name!r} uses "
                "loop_max_times without a loop"
            )
        if (step.loop_break is not None or step.loop_continue is not None) and (
            step.child_steps or step.loop_steps
        ):
            control = "break" if step.loop_break is not None else "continue"
            raise ConfigurationError(
                f"step {step.name!r} in workflow {workflow_name!r} uses "
                f"{control} but does not directly execute worker work"
            )
        if step.artifact_dependency is not None:
            dependency = prior.get(step.artifact_dependency)
            if dependency is None:
                raise ConfigurationError(
                    f"step {step.name!r} in workflow {workflow_name!r} depends_on "
                    f"unknown or later sibling step {step.artifact_dependency!r}"
                )
            if not dependency.artifact:
                raise ConfigurationError(
                    f"step {step.name!r} in workflow {workflow_name!r} depends_on "
                    f"step {dependency.name!r}, which does not produce an artifact"
                )
        _validate_hooks(step.hooks, set(), expected_scope="step")
        _validate_steps(workflow_name, step.child_steps, inside_loop=inside_loop)
        _validate_steps(workflow_name, step.loop_steps, inside_loop=True)
        _validate_steps(workflow_name, _item_steps(step), inside_loop=inside_loop)
        prior[step.name] = step


def _validate_transitions(workflow: WorkflowDefinition) -> None:
    """A transition ends a handoff workflow; it is its last step or a hook there.

    A task hands off once and a transition never returns, so anything after it
    could not run.  Rejecting misplaced transitions here turns a run-time
    surprise into a configuration error.
    """
    steps = workflow.steps
    transitions = [
        step
        for step in _walk_steps(steps)
        if isinstance(step.operation, WorkflowHandoff)
    ]
    hooked = [
        hook
        for step in _walk_steps(steps)
        for hook in step.hooks
        if isinstance(hook.handler.operation, WorkflowHandoff)
    ] + [
        hook
        for hook in workflow.hooks
        if isinstance(hook.handler.operation, WorkflowHandoff)
    ]
    if not transitions and not hooked:
        return
    if not workflow.handoff:
        raise ConfigurationError(
            f"workflow {workflow.name!r} uses a workflow transition; a workflow "
            "that hands off must declare handoff: true"
        )
    if len(transitions) + len(hooked) > 1:
        raise ConfigurationError(
            f"workflow {workflow.name!r} has more than one workflow transition; "
            "a task hands off once, so choose the target dynamically instead"
        )
    if transitions and (not steps or steps[-1] is not transitions[0]):
        raise ConfigurationError(
            f"workflow {workflow.name!r} must place its workflow transition "
            f"step {transitions[0].name!r} last; nothing after a handoff runs"
        )


def _item_steps(step: StepDefinition) -> tuple[StepDefinition, ...]:
    return step.items.steps if step.items is not None else ()


def _validate_item_flows(workflow_name: str, steps: tuple[StepDefinition, ...]) -> None:
    """Allow one ``items`` step per workflow; its plan expands in one place.

    Collected items belong to the workflow run and every per-item template is
    expanded at the first template position, so a second item flow would be
    merged into the first.  The limitation is intentional and documented.
    """
    flows = [step for step in _walk_steps(steps) if step.items is not None]
    if len(flows) > 1:
        raise ConfigurationError(
            f"workflow {workflow_name!r} may define at most one items step; "
            "found " + ", ".join(repr(step.name) for step in flows)
        )
    for step in flows:
        if step.collect_children or step.child_workflow is not None:
            raise ConfigurationError(
                f"step {step.name!r} in workflow {workflow_name!r} cannot "
                "combine items with child tasks"
            )


def _validate_hooks(
    hooks: tuple[HookDefinition, ...],
    known_steps: set[str],
    known_workflows: set[str] | None = None,
    *,
    expected_scope: str,
) -> None:
    for hook in hooks:
        _validate_execution_hints(hook.handler, hook.path or "hook")
        if isinstance(hook.handler, StepDefinition) and (
            hook.handler.child_steps or hook.handler.loop_steps or hook.handler.items
        ):
            raise ConfigurationError(
                f"{hook.path or 'hook'} cannot use a container handler"
            )
        if hook.phase not in _HOOK_PHASES:
            raise ConfigurationError(
                f"{hook.path or 'hook'} has unknown phase {hook.phase!r}"
            )
        if hook.scope != expected_scope:
            raise ConfigurationError(
                f"{hook.path or 'hook'} has scope {hook.scope!r}; "
                f"expected {expected_scope!r}"
            )
        if expected_scope != "global" and hook.workflow_names:
            raise ConfigurationError(
                f"{hook.path or 'hook'} cannot filter by workflow at "
                f"{expected_scope} scope"
            )
        if expected_scope == "step" and hook.step_names:
            raise ConfigurationError(
                f"{hook.path or 'hook'} cannot filter by step at step scope"
            )
        if hook.phase in {"before_start_workflow", "before_complete_workflow"} and (
            hook.step_names
        ):
            raise ConfigurationError(
                f"{hook.path or 'hook'} cannot filter a workflow boundary by step"
            )
        if known_workflows is not None:
            unknown_workflows = set(hook.workflow_names) - known_workflows
            if unknown_workflows:
                raise ConfigurationError(
                    f"{hook.path or 'hook'} references unknown workflow(s): "
                    + ", ".join(sorted(unknown_workflows))
                )
        unknown_steps = set(hook.step_names) - known_steps
        if unknown_steps:
            raise ConfigurationError(
                f"{hook.path or 'hook'} references unknown step(s): "
                + ", ".join(sorted(unknown_steps))
            )


def _walk_steps(steps: tuple[StepDefinition, ...]) -> tuple[StepDefinition, ...]:
    result: list[StepDefinition] = []
    for step in steps:
        result.append(step)
        result.extend(_walk_steps(step.child_steps))
        result.extend(_walk_steps(step.loop_steps))
        result.extend(_walk_steps(_item_steps(step)))
    return tuple(result)


def _validate_workflow_boundary_hooks(
    configuration: WorkflowConfiguration,
) -> None:
    """Keep workflow boundaries out of step-local lifecycle definitions."""
    boundary_phases = {"before_start_workflow", "before_complete_workflow"}
    for workflow in configuration.workflows:
        for step in _walk_steps(workflow.steps):
            for hook in step.hooks:
                if hook.phase in boundary_phases:
                    raise ConfigurationError(
                        f"{hook.path or 'hook'} uses {hook.phase} at step scope; "
                        "workflow boundary hooks belong at global or workflow scope"
                    )


def _logical_step_paths(
    steps: tuple[StepDefinition, ...], parent: str | None = None
) -> set[str]:
    result: set[str] = set()
    for step in steps:
        path = f"{parent}/{step.name}" if parent else step.name
        result.add(path)
        result.update(_logical_step_paths(step.child_steps, path))
        result.update(_logical_step_paths(step.loop_steps, path))
        result.update(_logical_step_paths(_item_steps(step), path))
    return result


def _step_filter_references(
    steps: tuple[StepDefinition, ...], parent: str | None = None
) -> set[str]:
    """Return both compatible leaf names and precise logical step paths."""
    result: set[str] = set()
    for step in steps:
        path = f"{parent}/{step.name}" if parent else step.name
        result.update((step.name, path))
        result.update(_step_filter_references(step.child_steps, path))
        result.update(_step_filter_references(step.loop_steps, path))
        result.update(_step_filter_references(_item_steps(step), path))
    return result


def _validate_child_tasks(workflows: tuple[WorkflowDefinition, ...]) -> None:
    """Keep task orchestration deliberately to one parent/child level."""
    by_name = {workflow.name: workflow for workflow in workflows}
    for workflow in workflows:
        steps = _walk_steps(workflow.steps)
        collectors = [step for step in steps if step.collect_children]
        runners = [
            (step, _child_workflow_target(step))
            for step in steps
            if _child_workflow_target(step) is not None
        ]
        if len(collectors) > 1 or len(runners) > 1:
            raise ConfigurationError(
                f"workflow {workflow.name!r} may define at most one children "
                "collection step and one workflow_per_child step"
            )
        if runners and not collectors:
            raise ConfigurationError(
                f"workflow {workflow.name!r} uses workflow_per_child without "
                "a children step"
            )
        if not runners:
            continue
        target_name = runners[0][1]
        assert target_name is not None
        target = by_name.get(target_name)
        if target is None:
            raise ConfigurationError(
                f"workflow {workflow.name!r} references unknown child workflow "
                f"{target_name!r}"
            )
        if target_name == workflow.name or any(
            step.collect_children or _child_workflow_target(step) is not None
            for step in _walk_steps(target.steps)
        ):
            raise ConfigurationError(
                f"child workflow {target_name!r} cannot define child tasks; "
                "recursive child tasks are not supported"
            )


def _child_workflow_target(step: StepDefinition) -> str | None:
    """Read a static child target from the shorthand or the explicit action form."""
    if step.child_workflow is not None:
        return step.child_workflow
    if isinstance(step.operation, ChildWorkflowRun):
        return step.operation.workflow
    return None


def _unique(names: Iterable[str], label: str) -> None:
    values = tuple(names)
    if len(values) != len(set(values)):
        raise ConfigurationError(f"duplicate {label} name")


def _validate_execution_hints(
    value: HandlerDefinition | WorkflowDefinition, path: str
) -> None:
    for field in ("model", "reasoning"):
        hint = getattr(value, field)
        if hint is not None and (not isinstance(hint, str) or not hint.strip()):
            raise ConfigurationError(f"{path}.{field} must be a non-empty string")
    agent = value.agent
    if agent is not None and (
        not isinstance(agent, str) or not agent.strip() or agent == "auto"
    ):
        raise ConfigurationError(
            f"{path}.agent must be non-empty and must not be 'auto'"
        )


def _validate_document_updates(configuration: WorkflowConfiguration) -> None:
    """Every ``update_document`` must name a document declared at the root."""
    declared = {document.name for document in configuration.documents}

    def check(handler: HandlerDefinition, where: str) -> None:
        unknown = sorted(
            update.name
            for update in handler.update_document
            if update.name not in declared
        )
        if unknown:
            raise ConfigurationError(
                f"{where} updates undeclared document(s): "
                + ", ".join(unknown)
                + "; declare them under the root `documents`"
            )

    def walk(steps: tuple[StepDefinition, ...], where: str) -> None:
        for step in steps:
            label = f"{where} step {step.name!r}"
            check(step, label)
            for hook in step.hooks:
                check(hook.handler, f"{label} hook {hook.handler.name!r}")
            walk(step.child_steps, label)
            walk(step.loop_steps, label)
            walk(step.assessment_outcomes, label)
            if step.items is not None:
                walk(step.items.steps, label)

    for handler in configuration.handlers:
        check(handler, f"handler {handler.name!r}")
        if isinstance(handler, StepDefinition):
            walk(handler.child_steps, f"handler {handler.name!r}")
            walk(handler.loop_steps, f"handler {handler.name!r}")
            if handler.items is not None:
                walk(handler.items.steps, f"handler {handler.name!r}")
    for hook in configuration.global_hooks:
        check(hook.handler, f"global hook {hook.handler.name!r}")
    for workflow in configuration.workflows:
        for hook in workflow.hooks:
            check(
                hook.handler, f"workflow {workflow.name!r} hook {hook.handler.name!r}"
            )
        walk(workflow.steps, f"workflow {workflow.name!r}")
