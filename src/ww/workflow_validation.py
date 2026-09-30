# SPDX-License-Identifier: GPL-3.0-or-later
"""Notation-independent validation for normalized workflow definitions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from types import MappingProxyType

from ww.actions import DefinedAction, Prompt
from ww.builtin_workflows import with_builtin_workflows
from ww.errors import ConfigurationError
from ww.extensions import ExtensionRegistry, is_extension_reference
from ww.operations import WorkflowHandoff
from ww.project_config import ProjectConfig
from ww.validation import is_positive_int
from ww.workflow_config import (
    INIT_STEP_NAME,
    INIT_STEP_PROMPT,
    HandlerDefinition,
    HookDefinition,
    NameFilter,
    RuleHints,
    StepDefinition,
    WorkflowConfiguration,
    WorkflowDefinition,
    step_tree,
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
    "before_start",
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
    configuration = with_builtin_workflows(
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
        _validate_transitions(workflow, normalized.global_hooks)
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
    _validate_rule_groups(normalized, all_steps, known_workflows)
    _validate_mode_filters(normalized, all_steps, known_workflows)
    _validate_workflow_boundary_hooks(normalized)
    _validate_hook_references(normalized)
    _validate_recommendations(normalized)
    _validate_child_tasks(normalized.workflows)
    return normalized


def _validate_steps(
    workflow_name: str,
    steps: tuple[StepDefinition, ...],
    *,
    top_level: bool = False,
    inside_loop: bool = False,
    inside_children: bool = False,
    enclosing: Mapping[str, StepDefinition] = MappingProxyType({}),
    finished_containers: tuple[StepDefinition, ...] = (),
) -> None:
    """Validate one sibling list; ``enclosing`` holds earlier upper-level steps.

    ``artifact_from`` resolves to the nearest earlier step of that name: an
    earlier sibling first, then an earlier step of each enclosing level.  A
    container's own step is visible to its nested steps only when its work
    has finished before them (``finished_containers``): an assessment to its
    outcomes and an item collection to its per-item stages, but never a
    running loop.  Such a container supplies its own artifact; a group, or an
    assessment named after its outcomes, supplies the latest artifact saved
    inside it, so it needs a step inside that can save one.

    ``break`` ends the nearest enclosing loop, or the per-child stages of a
    ``children`` step (``inside_children``): the remaining children are
    skipped.  ``continue`` needs a loop.
    """
    _unique((step.name for step in steps), f"step in workflow {workflow_name!r}")
    prior: dict[str, StepDefinition] = (
        {INIT_STEP_NAME: implicit_init_step()} if top_level else {}
    )
    for step in steps:
        if step.assessment_outcomes and all(
            outcome.stop_workflow for outcome in step.assessment_outcomes
        ):
            raise ConfigurationError(
                f"assess {step.name!r} in workflow {workflow_name!r} has only "
                "outcomes that stop the workflow; give one of them steps, or use "
                "the compact form"
            )
        _validate_execution_hints(step, f"step {step.name!r}")
        if step.name == INIT_STEP_NAME:
            raise ConfigurationError(
                f"step name {INIT_STEP_NAME!r} is reserved and must not be declared"
            )
        if (step.loop_continue is not None and not inside_loop) or (
            step.loop_break is not None and not (inside_loop or inside_children)
        ):
            control = "break" if step.loop_break is not None else "continue"
            raise ConfigurationError(
                f"step {step.name!r} in workflow {workflow_name!r} uses "
                f"{control} outside a loop"
            )
        if step.max_rounds is not None and (
            not is_positive_int(step.max_rounds)
        ):
            raise ConfigurationError(
                f"step {step.name!r} in workflow {workflow_name!r} has an invalid "
                "max_rounds; expected a positive integer"
            )
        if step.max_rounds is not None and not step.loop_steps:
            raise ConfigurationError(
                f"step {step.name!r} in workflow {workflow_name!r} uses "
                "max_rounds without a loop"
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
            dependency = prior.get(step.artifact_dependency) or enclosing.get(
                step.artifact_dependency
            )
            if dependency is None:
                raise ConfigurationError(
                    f"step {step.name!r} in workflow {workflow_name!r} takes "
                    "artifact_from "
                    f"step {step.artifact_dependency!r}, which is not an earlier "
                    "step at its own or an enclosing level"
                )
            if not _supplies_artifact(
                dependency,
                own=any(dependency is found for found in finished_containers),
            ):
                raise ConfigurationError(
                    f"step {step.name!r} in workflow {workflow_name!r} takes "
                    "artifact_from "
                    f"step {dependency.name!r}, which does not produce an artifact"
                )
        _validate_hooks(step.hooks, set(), expected_scope="step")
        visible = {**enclosing, **prior}
        finished = (*finished_containers, step)
        _validate_steps(
            workflow_name,
            step.child_steps,
            inside_loop=inside_loop,
            inside_children=inside_children,
            enclosing=visible,
            finished_containers=finished_containers,
        )
        _validate_steps(
            workflow_name,
            step.loop_steps,
            inside_loop=True,
            enclosing=visible,
            finished_containers=finished_containers,
        )
        _validate_steps(
            workflow_name,
            _item_steps(step),
            inside_loop=inside_loop,
            enclosing={**visible, step.name: step},
            finished_containers=finished,
        )
        # A ``break`` in a per-child stage ends the children; a ``continue``
        # needs a loop of its own inside the stage.
        _validate_steps(
            workflow_name,
            _child_stages(step),
            inside_children=True,
            enclosing={**visible, step.name: step},
            finished_containers=finished,
        )
        # Outcomes are alternatives, so none is an earlier sibling of another.
        for outcome in step.assessment_outcomes:
            _validate_steps(
                workflow_name,
                (outcome,),
                inside_loop=inside_loop,
                inside_children=inside_children,
                enclosing={**visible, step.name: step},
                finished_containers=finished,
            )
        prior[step.name] = step


def _validate_transitions(
    workflow: WorkflowDefinition, global_hooks: tuple[HookDefinition, ...]
) -> None:
    """A transition makes a handoff workflow and must be the last thing it runs.

    A task hands off once and a transition never returns, so anything after it
    could not run.  The transition is either the last top-level step or the
    last ``after_complete`` hook of that step; rejecting any other placement
    here turns a run-time surprise into a configuration error.
    """
    for hook in (*global_hooks, *workflow.hooks):
        if _is_transition_hook(hook):
            raise ConfigurationError(
                f"{hook.path or 'hook'} is a handoff_to transition at "
                f"{hook.scope} scope; a transition hook belongs on the "
                "after_complete hooks of a workflow's last step"
            )
    steps = tuple(step_tree(workflow.steps))
    transitions = [
        step for step in steps if isinstance(step.operation, WorkflowHandoff)
    ]
    hooked = [
        (step, hook)
        for step in steps
        for hook in step.hooks
        if _is_transition_hook(hook)
    ]
    if not transitions and not hooked:
        return
    if len(transitions) + len(hooked) > 1:
        raise ConfigurationError(
            f"workflow {workflow.name!r} has more than one handoff_to; "
            "a task hands off once, so choose the target dynamically instead"
        )
    last = workflow.steps[-1]
    if transitions and transitions[0] is not last:
        raise ConfigurationError(
            f"workflow {workflow.name!r} must place its handoff_to "
            f"step {transitions[0].name!r} last; nothing after a handoff runs"
        )
    if hooked:
        owner, hook = hooked[0]
        if owner is not last or hook.phase != "after_complete":
            raise ConfigurationError(
                f"workflow {workflow.name!r} must place its handoff_to "
                f"hook in the after_complete hooks of its last step "
                f"{last.name!r}; nothing after a handoff runs"
            )
        following = [
            later
            for later in owner.hooks[owner.hooks.index(hook) + 1 :]
            if later.phase == "after_complete"
        ]
    else:
        # Every completion hook of the transition step runs after it.
        paths = frozenset(_logical_step_paths(workflow.steps))
        following = [
            later
            for later in (*global_hooks, *workflow.hooks, *last.hooks)
            if later.phase in {"before_complete", "after_complete"}
            and later.applies_to(workflow.name, last.name, last.name, paths)
        ]
    if following:
        raise ConfigurationError(
            f"handoff workflow {workflow.name!r} must end with its handoff_to; "
            f"{following[0].path or 'a hook'} would run after it"
        )


def _is_transition_hook(hook: HookDefinition) -> bool:
    return isinstance(hook.handler.operation, WorkflowHandoff)


def _supplies_artifact(step: StepDefinition, *, own: bool) -> bool:
    """Whether ``artifact_from`` naming ``step`` can find an artifact.

    ``own``: the dependent runs inside the step, whose own work has finished.
    A group saves nothing itself and supplies the latest artifact saved
    inside it.  An assessment named after its outcomes supplies the latest
    artifact of its chosen outcome when an outcome can save one, else its
    own.  Any other step supplies only its own artifact.
    """
    if own:
        return step.artifact
    if step.child_steps:
        return any(_can_save_artifact(child) for child in step.child_steps)
    return step.artifact or any(
        _can_save_artifact(outcome) for outcome in step.assessment_outcomes
    )


def _can_save_artifact(step: StepDefinition) -> bool:
    """Whether running ``step`` can save an artifact, itself or inside it."""
    if step.stop_workflow:
        return False
    if step.child_steps:
        return any(_can_save_artifact(child) for child in step.child_steps)
    return step.artifact or any(
        _can_save_artifact(nested)
        for nested in (
            *step.loop_steps,
            *step.assessment_outcomes,
            *_template_steps(step),
        )
    )


def _item_steps(step: StepDefinition) -> tuple[StepDefinition, ...]:
    return step.items.steps if step.items is not None else ()


def _child_stages(step: StepDefinition) -> tuple[StepDefinition, ...]:
    return step.children.steps if step.children is not None else ()


def _template_steps(step: StepDefinition) -> tuple[StepDefinition, ...]:
    """The stages a step repeats: per item, or per child."""
    return (*_item_steps(step), *_child_stages(step))


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
        if expected_scope != "global" and not hook.workflows.admits_all:
            raise ConfigurationError(
                f"{hook.path or 'hook'} cannot filter by workflow at "
                f"{expected_scope} scope"
            )
        if expected_scope == "step" and not hook.steps.admits_all:
            raise ConfigurationError(
                f"{hook.path or 'hook'} cannot filter by step at step scope"
            )
        if hook.phase in {"before_start_workflow", "before_complete_workflow"} and (
            not hook.steps.admits_all
        ):
            raise ConfigurationError(
                f"{hook.path or 'hook'} cannot filter a workflow boundary by step"
            )
        if hook.on_failure == "fix":
            if hook.phase != "before_complete":
                raise ConfigurationError(
                    f"{hook.path or 'hook'}: on_failure: fix is only valid on "
                    "before_complete hooks"
                )
            if isinstance(hook.handler.operation, WorkflowHandoff):
                raise ConfigurationError(
                    f"{hook.path or 'hook'}: on_failure: fix is not valid on a "
                    "workflow transition"
                )
        _validate_filters(
            hook.path or "hook",
            hook.workflows,
            hook.steps,
            known_steps,
            known_workflows,
        )


def _validate_filters(
    label: str,
    workflows: NameFilter,
    steps: NameFilter,
    known_steps: set[str],
    known_workflows: set[str] | None,
) -> None:
    """Reject ``workflows``/``steps`` filters naming nothing that exists."""
    if known_workflows is not None:
        unknown_workflows = set(workflows.listed) - known_workflows
        if unknown_workflows:
            raise ConfigurationError(
                f"{label} references unknown workflow(s): "
                + ", ".join(sorted(unknown_workflows))
            )
    unknown_steps = set(steps.listed) - known_steps
    if unknown_steps:
        raise ConfigurationError(
            f"{label} references unknown step(s): " + ", ".join(sorted(unknown_steps))
        )


def _validate_rule_groups(
    configuration: WorkflowConfiguration,
    known_steps: set[str],
    known_workflows: set[str],
) -> None:
    """A rule group's filters name workflows and steps that exist, as a hook's do."""
    _unique((group.name for group in configuration.rule_groups), "rule group")
    for group in configuration.rule_groups:
        _validate_filters(
            f"rule group {group.name!r}",
            group.workflows,
            group.steps,
            known_steps,
            known_workflows,
        )
        _validate_rule_hints(group.hints, f"rule group {group.name!r}")
        for rule in group.rules:
            _validate_rule_hints(rule.hints, f"rule {rule.id!r}")


def _validate_mode_filters(
    configuration: WorkflowConfiguration,
    known_steps: set[str],
    known_workflows: set[str],
) -> None:
    """An automatic mode's filters name workflows and steps that exist."""
    for mode in configuration.modes:
        if mode.automatic:
            _validate_filters(
                f"mode {mode.name!r}",
                mode.workflows or NameFilter(),
                mode.steps or NameFilter(),
                known_steps,
                known_workflows,
            )


def _validate_rule_hints(hints: RuleHints, path: str) -> None:
    if hints.agent == "auto":
        raise ConfigurationError(f"{path}.agent must not be 'auto'")


def _validate_hook_references(configuration: WorkflowConfiguration) -> None:
    """Reject a hook naming a root handler that is a whole step tree.

    A hook runs one action. A handler defining ``loop``, ``steps``,
    ``items``, or ``children`` is only usable as a workflow step; run as a
    hook it would lose its tree and become a prompt carrying nothing but its
    name.
    """
    handlers = configuration.handlers_by_name
    for hook in _every_hook(configuration):
        if not hook.handler.is_reference:
            continue
        registered = handlers.get(hook.handler.name)
        if isinstance(registered, StepDefinition) and _is_container(registered):
            raise ConfigurationError(
                f"{hook.path or 'hook'} runs handler {registered.name!r}, which "
                "defines a loop, steps, items, or children; a hook runs a "
                f"single action, so use {registered.name!r} as a workflow "
                "step instead"
            )


def _validate_recommendations(configuration: WorkflowConfiguration) -> None:
    """A recommended next workflow must exist and must not race a handoff."""
    known = configuration.workflows_by_name
    for workflow in configuration.workflows:
        recommended = workflow.recommended_next_workflow
        if recommended is None:
            continue
        if recommended not in known:
            raise ConfigurationError(
                f"workflow {workflow.name!r} recommends unknown workflow "
                f"{recommended!r}"
            )
        if workflow.hands_off:
            raise ConfigurationError(
                f"workflow {workflow.name!r} hands off at its end and cannot "
                "also recommend a next workflow"
            )


def _is_container(step: StepDefinition) -> bool:
    return bool(
        step.child_steps or step.loop_steps or step.items or step.children
    )


def _every_hook(configuration: WorkflowConfiguration) -> Iterable[HookDefinition]:
    yield from configuration.global_hooks
    for workflow in configuration.workflows:
        yield from workflow.hooks
        yield from _step_hooks(workflow.steps)
    for handler in configuration.handlers:
        if isinstance(handler, StepDefinition):
            yield from _step_hooks((handler,))


def _step_hooks(steps: tuple[StepDefinition, ...]) -> Iterable[HookDefinition]:
    for step in steps:
        yield from step.hooks
        yield from _step_hooks(
            (
                *step.child_steps,
                *step.loop_steps,
                *_template_steps(step),
                *step.assessment_outcomes,
            )
        )


def _walk_steps(steps: tuple[StepDefinition, ...]) -> tuple[StepDefinition, ...]:
    result: list[StepDefinition] = []
    for step in steps:
        result.append(step)
        result.extend(_walk_steps(step.child_steps))
        result.extend(_walk_steps(step.loop_steps))
        result.extend(_walk_steps(_template_steps(step)))
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
        result.update(_logical_step_paths(_template_steps(step), path))
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
        result.update(_step_filter_references(_template_steps(step), path))
    return result


def _validate_child_tasks(workflows: tuple[WorkflowDefinition, ...]) -> None:
    """Keep task orchestration deliberately to one parent/child level."""
    by_name = {workflow.name: workflow for workflow in workflows}
    for workflow in workflows:
        collectors = [
            step for step in _walk_steps(workflow.steps) if step.children is not None
        ]
        if len(collectors) > 1:
            raise ConfigurationError(
                f"workflow {workflow.name!r} may define at most one children step; "
                "found " + ", ".join(repr(step.name) for step in collectors)
            )
        if not collectors:
            continue
        per_item = [
            step
            for flow_step in _walk_steps(workflow.steps)
            for step in _walk_steps(_item_steps(flow_step))
            if step.children is not None
        ]
        if per_item:
            raise ConfigurationError(
                f"workflow {workflow.name!r} collects children in the per-item "
                f"stage {per_item[0].name!r}; a children step cannot repeat per item"
            )
        flow = collectors[0].children
        assert flow is not None
        # Per-child stages expand once, when collection completes; a loop's
        # next round would replay the first child's stages and never reach
        # the others.
        looped = [
            step
            for loop_owner in _walk_steps(workflow.steps)
            for step in _walk_steps(loop_owner.loop_steps)
            if step.children is not None and step.children.steps
        ]
        if looped:
            raise ConfigurationError(
                f"workflow {workflow.name!r} runs children.steps of "
                f"{looped[0].name!r} inside a loop; per-child stages cannot "
                "repeat per loop round, so move the children step out of the "
                "loop or name the child workflow with children.workflow"
            )
        target = by_name.get(flow.workflow)
        if target is None:
            raise ConfigurationError(
                f"workflow {workflow.name!r} references unknown child workflow "
                f"{flow.workflow!r}"
            )
        if target.name == workflow.name or any(
            step.children is not None for step in _walk_steps(target.steps)
        ):
            raise ConfigurationError(
                f"child workflow {target.name!r} cannot define child tasks; "
                "recursive child tasks are not supported"
            )


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
    """Every ``saves`` document entry must name a document declared at the root."""
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
            walk(_template_steps(step), label)

    for handler in configuration.handlers:
        check(handler, f"handler {handler.name!r}")
        if isinstance(handler, StepDefinition):
            walk(handler.child_steps, f"handler {handler.name!r}")
            walk(handler.loop_steps, f"handler {handler.name!r}")
            walk(_template_steps(handler), f"handler {handler.name!r}")
    for hook in configuration.global_hooks:
        check(hook.handler, f"global hook {hook.handler.name!r}")
    for workflow in configuration.workflows:
        for hook in workflow.hooks:
            check(
                hook.handler, f"workflow {workflow.name!r} hook {hook.handler.name!r}"
            )
        walk(workflow.steps, f"workflow {workflow.name!r}")
