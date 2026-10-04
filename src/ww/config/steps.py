# SPDX-License-Identifier: GPL-3.0-or-later
"""Recursive step parsing and reusable handler catalog parsing."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast, get_args

from ww.actions import (
    DefinedAction,
    DefinitionOverrideContext,
    Prompt,
    actions,
)
from ww.contracts import ItemAssignment, ItemOperation, LoopAssignment, StepRole
from ww.errors import ConfigurationError
from ww.items import FIELD_NAME
from ww.operations import ChildWorkflowRun, WorkflowHandoff
from ww.validation import is_positive_int
from ww.workflow_config import (
    ChildFlow,
    ChoiceDefinition,
    HandlerDefinition,
    HookDefinition,
    ItemFlow,
    StepDefinition,
    StepRule,
    step_tree,
)

from .actions import (
    _bare_extension_reference,
    _handler_keys,
    _parse_handler,
    _parse_hooks,
    extension_reference,
)
from .rules import parse_step_rules
from .values import (
    _NAME,
    _description,
    _mapping,
    _named_entry,
    _nonempty_string,
    _only,
    _optional_agent,
    _optional_string,
    _profile,
    _role,
    _subagents,
    _unique,
)

STEP_ONLY_KEYS: set[str] = {
    "hooks",
    "steps",
    "loop",
    "max_rounds",
    "assignment",
    "break",
    "continue",
    "role",
    "subagents",
    "interactive",
    "explicit",
    "learnable",
    "choices",
    "profile",
    "items",
    "item_phase",
    "artifact_from",
    "artifact",
    "children",
    "handler",
    "question",
    "outcomes",
    "positive",
    "negative",
    "mixed",
    "rules",
}

CHILD_FLOW_KEYS = {"description", "workflow", "steps", "assignment"}
ITEM_FLOW_KEYS = {
    "description",
    "steps",
    "assignment",
    "persistent",
    "analyze",
    "resolve",
    "report",
    "variables",
    "saves",
    "interactive",
    "choices",
    "identity",
    "unique",
    "agent",
    "model",
    "reasoning",
    "profile",
    "role",
    "subagents",
}
_ITEM_FLOW_SETTINGS = ("agent", "model", "reasoning", "profile", "role", "subagents")
BUILTIN_ITEM_STEP_NAME = "handle-item"
BUILTIN_ITEM_STEP_PROMPT = (
    "Handle this item end to end: analyze it, resolve it, and report the outcome."
)
# Under ``items``, guidance for one phase of the built-in stage.
_ITEM_PHASE_GUIDANCE = (
    ("analyze", "When analyzing it"),
    ("resolve", "When resolving it"),
    ("report", "When reporting the outcome"),
)
# ``item_phase`` on a per-item stage, and the item operation it marks.
ITEM_PHASES: dict[str, ItemOperation] = {
    "analyze": "process_item",
    "resolve": "resolve_item",
    "report": "report_item",
}
# ``interactive`` takes true (a conversation) or ``page`` (the operator page).
INTERACTIVE_PAGE = "page"
LOOP_ASSIGNMENTS: tuple[LoopAssignment, ...] = ("per_round", "per_step")
CHILD_ASSIGNMENTS = ("per_step",)


def _parse_handlers(data: Any) -> tuple[HandlerDefinition, ...]:
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ConfigurationError("handlers must be a list")
    # A catalog entry may be a complete step tree, not just an executable
    # action.  Parse entries in order so a handler can reuse an earlier one in
    # the same way that workflow steps do.
    result: list[HandlerDefinition] = []
    known: dict[str, HandlerDefinition] = {}
    for index, item in enumerate(data):
        path = f"handlers[{index}]"
        raw_mapping = _mapping(item, path)
        # Unlike an action-only handler, a reusable step may use its
        # named-entry value for the container definition itself:
        # ``- review: {loop: [...]}``.
        if "name" not in raw_mapping and raw_mapping:
            name, value = next(iter(raw_mapping.items()))
            if isinstance(value, dict):
                if not isinstance(name, str) or not name.strip():
                    raise ConfigurationError(
                        f"{path} shorthand name must be a non-empty string"
                    )
                remaining = dict(raw_mapping)
                del remaining[name]
                overlap = set(remaining).intersection(value)
                if overlap:
                    raise ConfigurationError(
                        f"{path} repeats key(s): {', '.join(sorted(overlap))}"
                    )
                mapping = {"name": name, **value, **remaining}
            else:
                mapping = _named_entry(raw_mapping, path)
        else:
            mapping = _named_entry(raw_mapping, path)
        handler = (
            _parse_step(mapping, path, known)
            if STEP_ONLY_KEYS & set(mapping)
            else _parse_handler(mapping, path)
        )
        result.append(handler)
        known[handler.name] = handler
    return tuple(result)


# Keys that give a step content of its own; a step with none of them and a
# root handler of the same name refers to that handler.
_STEP_CONTENT_KEYS = frozenset(
    {
        "description",
        "handler",
        "kind",
        "mcp",
        "argv",
        "shell",
        "args",
        "env",
        "assert",
        "idempotent",
        "action",
        "variables",
        "saves",
        "handlers",
        "steps",
        "loop",
        "items",
        "children",
        "explicit",
        "handoff_to",
        "question",
        "outcomes",
        "positive",
        "negative",
        "mixed",
        "item_phase",
        "rules",
    }
)


def _parse_choices(data: Any, path: str) -> tuple[ChoiceDefinition, ...]:
    """Parse ``choices``: named entries whose key is the label shown as is."""
    if data is None:
        return ()
    if not isinstance(data, list) or not data:
        raise ConfigurationError(f"{path}.choices must be a non-empty list")
    result = []
    for index, item in enumerate(data):
        item_path = f"{path}.choices[{index}]"
        mapping = _named_entry(_mapping(item, item_path), item_path)
        _only(mapping, {"name", "description"}, item_path)
        label = mapping.get("name")
        if not isinstance(label, str) or not label.strip():
            raise ConfigurationError(f"{item_path} label must be a non-empty string")
        result.append(
            ChoiceDefinition(
                label.strip(), _description(mapping.get("description"), item_path)
            )
        )
    _unique((choice.label for choice in result), f"choice label in {path}")
    return tuple(result)


def _is_bare_reference(mapping: dict[str, Any]) -> bool:
    return isinstance(mapping.get("name"), str) and all(
        mapping.get(key) is None for key in _STEP_CONTENT_KEYS
    )


def _parse_step(
    data: Any,
    path: str,
    handlers_by_name: dict[str, HandlerDefinition],
    *,
    item_stage: bool = False,
) -> StepDefinition:
    """Parse one step entry, its nested steps included."""
    mapping = _step_mapping(data, path, handlers_by_name)
    base = (
        extension_reference(mapping, path)
        if _bare_extension_reference(mapping)
        else _parse_handler(
            mapping, path, transition=True, allowed_extra=STEP_ONLY_KEYS
        )
    )
    question, outcomes, base = _parse_assessment(mapping, path, base, handlers_by_name)
    base, referenced = _resolve_handler(mapping, path, base, handlers_by_name)
    if base.handlers and (base.action is not None or base.operation is not None):
        raise ConfigurationError(f"{path} cannot combine handlers with an action")
    children = _parse_nested_steps(mapping, "steps", path, handlers_by_name)
    if "steps" not in mapping and referenced is not None:
        children = referenced.child_steps
    loop_steps, max_rounds, loop_assignment = _parse_loop(
        mapping, path, handlers_by_name, referenced
    )
    loop_break, loop_continue = _parse_loop_controls(mapping, path, referenced)
    _check_loop_wrapper(mapping, path, base, loop_steps)
    item_operation = _parse_item_phase(mapping, path)
    child_flow = _parse_child_flow(mapping, path, handlers_by_name, referenced)
    if "children" in mapping and base.operation is not None:
        raise ConfigurationError(
            f"{path} cannot combine children with handoff_to; "
            "name the child workflow under children.workflow"
        )
    artifact, artifact_from = _parse_artifact(mapping, path, referenced)
    role, subagents, profile = _parse_performer(mapping, path, referenced)
    interactive, ui, choices = _parse_interactive(mapping, path, referenced, item_stage)
    explicit = mapping.get(
        "explicit", referenced.explicit if referenced is not None else None
    )
    if "explicit" in mapping and not isinstance(explicit, bool):
        raise ConfigurationError(f"{path}.explicit must be true or false")
    if interactive and role == "worker":
        raise ConfigurationError(
            f"{path} is interactive, so the manager holds the conversation; "
            "role: worker contradicts it"
        )
    learnable = mapping.get("learnable", referenced.learnable if referenced else False)
    if not isinstance(learnable, bool):
        raise ConfigurationError(f"{path}.learnable must be true or false")
    if learnable and not artifact:
        raise ConfigurationError(f"{path}.learnable requires artifact: true")
    hooks = _parse_step_hooks(mapping, path, referenced)
    rules = _parse_rules(mapping, path, base.name, referenced)
    items = (
        _parse_items(mapping, path, handlers_by_name, profile, role, subagents)
        if "items" in mapping
        else referenced.items
        if referenced is not None
        else None
    )
    containers = sum(
        bool(value)
        for value in (children, loop_steps, items is not None, child_flow is not None)
    )
    if base.handlers and containers:
        raise ConfigurationError(
            f"{path} cannot combine handlers with steps, loop, items, or children"
        )
    if containers > 1:
        raise ConfigurationError(
            f"{path} cannot combine steps, loop, items, and children"
        )
    if items is not None and item_operation is not None:
        raise ConfigurationError(f"{path} cannot combine items with item_phase")
    if item_operation is None and referenced is not None:
        item_operation = referenced.item_operation
    return StepDefinition(
        name=base.name,
        description=base.description,
        action=base.action,
        operation=base.operation,
        provide=base.provide,
        save_metadata=base.save_metadata,
        update_document=base.update_document,
        update_item=base.update_item,
        outputs=base.outputs,
        agent=base.agent,
        model=base.model,
        reasoning=base.reasoning,
        workdir=base.workdir,
        extension_arguments=base.extension_arguments,
        on_failure=base.on_failure,
        on_failure_instruction=base.on_failure_instruction,
        handlers=base.handlers,
        role=role,
        subagents=subagents,
        interactive=interactive,
        explicit=explicit,
        learnable=learnable,
        choices=choices,
        ui=ui,
        profile=profile["profile"],
        profile_description=profile["profile_description"],
        hooks=hooks,
        rules=rules,
        child_steps=children,
        loop_steps=loop_steps,
        max_rounds=max_rounds,
        loop_assignment=loop_assignment,
        loop_break=loop_break,
        loop_continue=loop_continue,
        items=items,
        item_operation=item_operation,
        artifact=artifact,
        children=child_flow,
        artifact_dependency=artifact_from,
        assessment_question=question,
        assessment_outcomes=outcomes,
    )


def _step_mapping(
    data: Any, path: str, handlers_by_name: dict[str, HandlerDefinition]
) -> dict[str, Any]:
    """Normalize a step entry to one mapping of its declared keys."""
    raw = _mapping(data, path)
    # ``assess`` is deliberately a construct rather than a conventional step
    # name: its compact spelling is a question and its long spelling owns
    # outcome substeps.
    mapping: dict[str, Any]
    if "assess" in raw:
        value = raw["assess"]
        rest = {key: item for key, item in raw.items() if key != "assess"}
        if isinstance(value, str):
            if rest:
                raise ConfigurationError(
                    f"{path}.assess compact form cannot have other keys"
                )
            mapping = {"name": "assess", "question": value}
        elif isinstance(value, dict):
            overlap = set(rest).intersection(value)
            if overlap:
                raise ConfigurationError(
                    f"{path}.assess repeats key(s): {', '.join(sorted(overlap))}"
                )
            mapping = {"name": "assess", **value, **rest}
        else:
            raise ConfigurationError(
                f"{path}.assess must be a question string or mapping"
            )
    else:
        mapping = _named_entry(raw, path)
    if _is_bare_reference(mapping) and mapping["name"] in handlers_by_name:
        # ``- fetch_requirements: ~`` with a root handler of that name means
        # the handler, exactly as a bare hook entry does; settings such as
        # ``profile`` or ``model`` on the step still override the copy.
        mapping = {**mapping, "handler": mapping["name"]}
    allowed = _handler_keys() | STEP_ONLY_KEYS | {"handoff_to"}
    if mapping.get("name") == "assess":
        unknown_branches = {
            key
            for key, value in mapping.items()
            if key not in allowed and isinstance(value, dict)
        }
        if unknown_branches:
            label = sorted(unknown_branches)[0]
            raise ConfigurationError(
                f"{path}.{label} is not a direct assessment branch; "
                "use positive, negative, mixed, or outcomes"
            )
    _only(mapping, allowed, path)
    return mapping


def _parse_assessment(
    mapping: dict[str, Any],
    path: str,
    base: HandlerDefinition,
    handlers_by_name: dict[str, HandlerDefinition],
) -> tuple[str | None, tuple[StepDefinition, ...], HandlerDefinition]:
    """Parse an assess step's question and outcomes, and its prompt handler."""
    question = (
        _nonempty_string(mapping, "question", path) if "question" in mapping else None
    )
    if question is not None and mapping["name"] != "assess":
        raise ConfigurationError(f"{path}.question is only valid for an assess step")
    direct = {
        label: mapping[label]
        for label in ("positive", "negative", "mixed")
        if label in mapping
    }
    if direct and question is None:
        raise ConfigurationError(
            f"{path}.{next(iter(direct))} requires an assess question"
        )
    if direct and "outcomes" in mapping:
        raise ConfigurationError(
            f"{path}.outcomes cannot be combined with direct assessment branches"
        )
    if "outcomes" in mapping and question is None:
        raise ConfigurationError(f"{path}.outcomes requires an assess question")
    outcomes = (
        _parse_assessment_branches(direct, path, handlers_by_name, "")
        if direct
        else _parse_assessment_outcomes(mapping, path, handlers_by_name)
    )
    if question is None:
        return None, outcomes, base
    if any(
        (
            base.action is not None,
            base.operation is not None,
            "handler" in mapping,
            "steps" in mapping,
            "loop" in mapping,
        )
    ):
        raise ConfigurationError(
            f"{path} assess cannot also declare an action or ordinary nested steps"
        )
    prompt = HandlerDefinition(
        name="assess",
        description=question,
        action=DefinedAction("prompt", Prompt(question)),
        workdir=base.workdir,
    )
    return question, outcomes, prompt


def _resolve_handler(
    mapping: dict[str, Any],
    path: str,
    base: HandlerDefinition,
    handlers_by_name: dict[str, HandlerDefinition],
) -> tuple[HandlerDefinition, StepDefinition | None]:
    """Apply ``handler``, and return the step it names when it names one."""
    if "handler" not in mapping:
        return base, None
    handler_name = mapping["handler"]
    if not isinstance(handler_name, str) or not _NAME.fullmatch(handler_name):
        raise ConfigurationError(f"{path}.handler must be a handler name")
    try:
        referenced = handlers_by_name[handler_name]
    except KeyError as error:
        raise ConfigurationError(
            f"{path}.handler references unknown handler {handler_name!r}"
        ) from error
    base = _step_handler_reference(mapping, base, referenced)
    return base, referenced if isinstance(referenced, StepDefinition) else None


def _parse_loop(
    mapping: dict[str, Any],
    path: str,
    handlers_by_name: dict[str, HandlerDefinition],
    referenced: StepDefinition | None,
) -> tuple[tuple[StepDefinition, ...], int | None, LoopAssignment | None]:
    """Parse ``loop`` with its ``max_rounds`` and ``assignment``."""
    loop_steps = _parse_nested_steps(
        mapping, "loop", path, handlers_by_name, require_nonempty=True
    )
    max_rounds: int | None = mapping.get("max_rounds")
    if max_rounds is not None and (not is_positive_int(max_rounds)):
        raise ConfigurationError(f"{path}.max_rounds must be a positive integer")
    if "max_rounds" in mapping and not loop_steps:
        raise ConfigurationError(f"{path}.max_rounds requires a loop")
    assignment: LoopAssignment | None = None
    if "assignment" in mapping:
        if not loop_steps:
            raise ConfigurationError(
                f"{path}.assignment on a step goes beside a loop; for items or "
                "children, write it inside that mapping"
            )
        assignment = cast(
            LoopAssignment,
            _assignment(mapping["assignment"], f"{path}.assignment", LOOP_ASSIGNMENTS),
        )
    if referenced is not None:
        if "loop" not in mapping:
            loop_steps = referenced.loop_steps
        if "max_rounds" not in mapping:
            max_rounds = referenced.max_rounds
        if "assignment" not in mapping:
            assignment = referenced.loop_assignment
    return loop_steps, max_rounds, assignment


def _parse_loop_controls(
    mapping: dict[str, Any], path: str, referenced: StepDefinition | None
) -> tuple[str | None, str | None]:
    """Parse the loop's ``break`` and ``continue`` conditions."""
    loop_break = mapping.get("break")
    loop_continue = mapping.get("continue")
    for control_name, control_value in (
        ("break", loop_break),
        ("continue", loop_continue),
    ):
        if control_value is not None and (
            not isinstance(control_value, str) or not control_value.strip()
        ):
            raise ConfigurationError(
                f"{path}.{control_name} must be a non-empty string"
            )
    if referenced is not None:
        if "break" not in mapping:
            loop_break = referenced.loop_break
        if "continue" not in mapping:
            loop_continue = referenced.loop_continue
    return loop_break, loop_continue


def _check_loop_wrapper(
    mapping: dict[str, Any],
    path: str,
    base: HandlerDefinition,
    loop_steps: tuple[StepDefinition, ...],
) -> None:
    """Reject a loop step that also acts or collects."""
    if loop_steps and any(
        (
            base.action is not None,
            base.operation is not None,
            bool(base.provide),
            bool(base.save_metadata),
            bool(base.update_document),
            bool(base.update_item),
            bool(base.outputs),
            "items" in mapping,
            "item_phase" in mapping,
            "children" in mapping,
            "artifact_from" in mapping,
        )
    ):
        raise ConfigurationError(
            f"{path} loop wrapper cannot also declare an action or collection"
        )


def _parse_item_phase(mapping: dict[str, Any], path: str) -> ItemOperation | None:
    """Parse ``item_phase`` into the item operation it marks."""
    if "item_phase" not in mapping:
        return None
    phase = mapping["item_phase"]
    if not isinstance(phase, str) or phase not in ITEM_PHASES:
        raise ConfigurationError(
            f"{path}.item_phase must be one of: " + ", ".join(ITEM_PHASES)
        )
    return ITEM_PHASES[phase]


def _parse_child_flow(
    mapping: dict[str, Any],
    path: str,
    handlers_by_name: dict[str, HandlerDefinition],
    referenced: StepDefinition | None,
) -> ChildFlow | None:
    """Parse ``children``, or take the referenced step's."""
    if "children" in mapping:
        return _parse_children(mapping, path, handlers_by_name)
    return referenced.children if referenced is not None else None


def _parse_artifact(
    mapping: dict[str, Any], path: str, referenced: StepDefinition | None
) -> tuple[bool, str | None]:
    """Parse ``artifact`` and ``artifact_from``."""
    artifact = mapping.get("artifact", True)
    if not isinstance(artifact, bool):
        raise ConfigurationError(f"{path}.artifact must be true or false")
    artifact_from = mapping.get("artifact_from")
    if artifact_from is not None and (
        not isinstance(artifact_from, str) or not _NAME.fullmatch(artifact_from)
    ):
        raise ConfigurationError(f"{path}.artifact_from must be a normalized step name")
    if referenced is not None:
        if "artifact" not in mapping:
            artifact = referenced.artifact
        if "artifact_from" not in mapping:
            artifact_from = referenced.artifact_dependency
    return artifact, artifact_from


def _parse_performer(
    mapping: dict[str, Any], path: str, referenced: StepDefinition | None
) -> tuple[StepRole | None, bool | None, dict[str, str | None]]:
    """Parse who performs the step: ``role``, ``subagents`` and ``profile``."""
    role = _role(mapping, path)
    subagents = _subagents(mapping, path)
    profile = _profile(mapping, path)
    if referenced is not None:
        if "role" not in mapping:
            role = referenced.role
        if "subagents" not in mapping:
            subagents = referenced.subagents
        if "profile" not in mapping:
            profile = {
                "profile": referenced.profile,
                "profile_description": referenced.profile_description,
            }
    return role, subagents, profile


def _parse_interactive(
    mapping: dict[str, Any],
    path: str,
    referenced: StepDefinition | None,
    item_stage: bool,
) -> tuple[bool, bool, tuple[ChoiceDefinition, ...]]:
    """Parse ``interactive`` and its ``choices``: whether, page, and choices."""
    raw = mapping.get("interactive", False)
    if not isinstance(raw, bool) and raw != INTERACTIVE_PAGE:
        raise ConfigurationError(f"{path}.interactive must be true, false, or page")
    interactive = raw is not False
    ui = raw == INTERACTIVE_PAGE
    choices = _parse_choices(mapping.get("choices"), path)
    if referenced is not None:
        if "interactive" not in mapping:
            interactive = referenced.interactive
            ui = referenced.ui
        if "choices" not in mapping:
            choices = referenced.choices
    if choices and not interactive:
        raise ConfigurationError(
            f"{path}.choices are offered to the operator, so they require "
            "interactive: true"
        )
    if ui and not item_stage:
        raise ConfigurationError(
            f"{path}.interactive: page is offered on per-item stages only; "
            "declare it under items"
        )
    return interactive, ui, choices


def _parse_step_hooks(
    mapping: dict[str, Any], path: str, referenced: StepDefinition | None
) -> tuple[HookDefinition, ...]:
    """Parse the step's ``hooks``, or take the referenced step's."""
    if "hooks" in mapping or referenced is None:
        return _parse_hooks(mapping.get("hooks", {}), "step", f"{path}.hooks")
    return referenced.hooks


def _parse_rules(
    mapping: dict[str, Any], path: str, name: str, referenced: StepDefinition | None
) -> tuple[StepRule, ...]:
    """Parse the step's ``rules``, or take the referenced step's."""
    if "rules" in mapping:
        return parse_step_rules(mapping["rules"], name, path)
    return referenced.rules if referenced is not None else ()


def _parse_children(
    mapping: dict[str, Any],
    path: str,
    handlers_by_name: dict[str, HandlerDefinition],
) -> ChildFlow:
    """Parse ``children``: the child ``workflow``, or the parent's ``steps``."""
    value = mapping["children"]
    children_path = f"{path}.children"
    if not isinstance(value, dict):
        raise ConfigurationError(
            f"{children_path} must be a mapping with the child workflow"
        )
    _only(value, CHILD_FLOW_KEYS, children_path)
    description = _optional_string(value, "description", children_path)
    if "assignment" in value:
        if "steps" not in value:
            raise ConfigurationError(
                f"{children_path}.assignment splits children.steps into worker "
                "assignments; without steps ww runs each child itself"
            )
        _assignment(
            value["assignment"], f"{children_path}.assignment", CHILD_ASSIGNMENTS
        )
    if "steps" in value:
        if "workflow" in value:
            raise ConfigurationError(
                f"{children_path} takes workflow or steps, not both: name the "
                "workflow every child runs, or list the parent's stages per "
                "child with one `workflow:` stage among them"
            )
        return _parse_child_stages(value, children_path, handlers_by_name, description)
    workflow = value.get("workflow")
    if not isinstance(workflow, str) or not _NAME.fullmatch(workflow):
        raise ConfigurationError(f"{children_path}.workflow must be a workflow name")
    return ChildFlow(workflow=workflow, description=description)


def _parse_child_stages(
    value: dict[str, Any],
    children_path: str,
    handlers_by_name: dict[str, HandlerDefinition],
    description: str | None,
) -> ChildFlow:
    """Parse ``children.steps``: the parent's stages, run once per child.

    Exactly one top-level stage carries ``workflow:``; inside ``children`` it
    runs the child task with that workflow and waits, so it becomes the
    stage's ``ChildWorkflowRun``.  A ``handoff_to`` transition cannot run
    here.
    """
    stages_path = f"{children_path}.steps"
    if not isinstance(value["steps"], list) or not value["steps"]:
        raise ConfigurationError(f"{stages_path} must contain at least one step")
    entries = [_child_run_entry(entry) for entry in value["steps"]]
    stages = _parse_nested_steps(
        {"steps": [entry for entry, _ in entries]},
        "steps",
        children_path,
        handlers_by_name,
    )
    runs = [
        stage
        for stage, (_, runs_child) in zip(stages, entries, strict=True)
        if runs_child
    ]
    if len(runs) != 1:
        raise ConfigurationError(
            f"{stages_path} needs exactly one stage with `workflow:`, the one "
            f"that runs the child task; found {len(runs)}"
        )
    run = runs[0]
    assert isinstance(run.operation, WorkflowHandoff)
    target = run.operation.target
    if not _NAME.fullmatch(target):
        raise ConfigurationError(
            f"{stages_path} stage {run.name!r}: workflow must be a workflow name"
        )
    if run.agent or run.model or run.reasoning:
        raise ConfigurationError(
            f"{stages_path} stage {run.name!r} runs the child task, which ww "
            "does; agent, model, and reasoning do not apply to it"
        )
    child_run = replace(
        run,
        description=run.description
        or f"Run the child task with the `{target}` workflow and wait for it.",
        operation=ChildWorkflowRun(target),
    )
    converted = tuple(child_run if stage is run else stage for stage in stages)
    for stage in step_tree(converted):
        if isinstance(stage.operation, WorkflowHandoff) or any(
            isinstance(hook.handler.operation, WorkflowHandoff) for hook in stage.hooks
        ):
            raise ConfigurationError(
                f"{stages_path}: {stage.name!r} carries handoff_to, a workflow "
                "transition, which cannot run inside children.steps; the stage "
                "with `workflow:` runs the child task"
            )
        if stage.items is not None or stage.children is not None:
            raise ConfigurationError(
                f"{stages_path} stage {stage.name!r} cannot use items or "
                "children: per-child stages do not nest another collection"
            )
    return ChildFlow(workflow=target, description=description, steps=converted)


def _child_run_entry(entry: Any) -> tuple[Any, bool]:
    """A top-level ``children.steps`` entry, and whether it runs the child.

    The stage that runs the child names its ``workflow``; it is parsed as a
    transition to that workflow and then turned into the child run.  It has
    nothing to say but its workflow, so it may be written as its name
    mapped to that setting: ``- implement: {workflow: task}``.
    """
    if (
        isinstance(entry, dict)
        and len(entry) == 1
        and "name" not in entry
        and isinstance(next(iter(entry.values())), dict)
        and "workflow" in next(iter(entry.values()))
    ):
        name, settings = next(iter(entry.items()))
        entry = {"name": name, **settings}
    if not isinstance(entry, dict) or "workflow" not in entry:
        return entry, False
    if "handoff_to" in entry:
        raise ConfigurationError(
            "a children.steps stage takes workflow (run the child) or "
            "handoff_to, not both"
        )
    converted = {
        ("handoff_to" if key == "workflow" else key): value
        for key, value in entry.items()
    }
    return converted, True


def _assignment(value: Any, path: str, allowed: tuple[str, ...]) -> str:
    """Check one ``assignment`` value against the ones its construct takes."""
    if not isinstance(value, str):
        raise ConfigurationError(f"{path} must be one of: " + ", ".join(allowed))
    if value == "per_child":
        raise ConfigurationError(
            f"{path}: per_child is reserved and not built yet; use per_step"
        )
    if value not in allowed:
        raise ConfigurationError(f"{path} must be one of: " + ", ".join(allowed))
    return value


def _parse_items(
    mapping: dict[str, Any],
    path: str,
    handlers_by_name: dict[str, HandlerDefinition],
    step_profile: dict[str, str | None],
    step_role: StepRole | None,
    step_subagents: bool | None,
) -> ItemFlow:
    """Parse ``items``: ``~``, splitting guidance text, or a full mapping."""
    value = mapping["items"]
    items_path = f"{path}.items"
    if value is None:
        value = {}
    elif isinstance(value, str):
        value = {"description": value}
    elif not isinstance(value, dict):
        raise ConfigurationError(
            f"{items_path} must be null, splitting guidance text, or a mapping"
        )
    _only(value, ITEM_FLOW_KEYS, items_path)
    description = (
        _nonempty_string(value, "description", items_path)
        if "description" in value
        else None
    )
    persistent = value.get("persistent", False)
    if not isinstance(persistent, bool):
        raise ConfigurationError(f"{items_path}.persistent must be true or false")
    identity = value.get("identity")
    if identity is not None and (
        not isinstance(identity, str) or not FIELD_NAME.fullmatch(identity)
    ):
        raise ConfigurationError(f"{items_path}.identity must be a field name")
    unique_raw = value.get("unique", [])
    if not isinstance(unique_raw, list) or not all(
        isinstance(name, str) and FIELD_NAME.fullmatch(name) for name in unique_raw
    ):
        raise ConfigurationError(f"{items_path}.unique must be a list of field names")
    unique = tuple(dict.fromkeys(([identity] if identity else []) + unique_raw))
    assignment = cast(
        ItemAssignment,
        _assignment(
            value.get("assignment", "together"),
            f"{items_path}.assignment",
            get_args(ItemAssignment),
        ),
    )
    phase_keys = [key for key, _ in _ITEM_PHASE_GUIDANCE]
    guidance = [
        (label, _nonempty_string(value, key, items_path))
        for key, label in _ITEM_PHASE_GUIDANCE
        if key in value
    ]
    if guidance and "steps" in value:
        raise ConfigurationError(
            f"{items_path} phase guidance ("
            + ", ".join(key for key in phase_keys if key in value)
            + ") describes the built-in handle-item stage; describe configured "
            "steps directly"
        )
    folded = [
        key for key in ("variables", "saves", "interactive", "choices") if key in value
    ]
    if folded and "steps" in value:
        raise ConfigurationError(
            f"{items_path}.{folded[0]} belongs to the built-in handle-item stage; "
            f"declare {folded[0]} on configured steps directly"
        )
    collect_only = "steps" in value and value["steps"] in ([], None)
    if collect_only:
        configured = [
            key for key in ("assignment", *_ITEM_FLOW_SETTINGS) if key in value
        ]
        if configured:
            raise ConfigurationError(
                f"{items_path} collects without per-item steps, so "
                + ", ".join(configured)
                + " has no effect"
            )
        return ItemFlow((), description, assignment, persistent, identity, unique)
    defaults = _item_flow_defaults(value, path, step_profile, step_role, step_subagents)
    if "steps" in value:
        steps = _parse_nested_steps(
            value,
            "steps",
            items_path,
            handlers_by_name,
            require_nonempty=True,
            item_stage=True,
        )
        entries = value["steps"]
    else:
        builtin = {
            "name": BUILTIN_ITEM_STEP_NAME,
            "description": "\n\n".join(
                (
                    BUILTIN_ITEM_STEP_PROMPT,
                    *(f"{label}: {text}" for label, text in guidance),
                )
            ),
            **{key: value[key] for key in folded},
        }
        steps = (
            replace(
                _parse_step(
                    builtin,
                    f"{items_path}.steps[0]",
                    handlers_by_name,
                    item_stage=True,
                ),
                item_operation="handle_item",
            ),
        )
        entries = [builtin]
    pages = [step.name for step in steps if step.ui]
    if len(pages) > 1:
        raise ConfigurationError(
            f"{items_path} may answer one stage on the operator page; found "
            "interactive: page on " + ", ".join(pages)
        )
    return ItemFlow(
        tuple(
            _with_item_flow_defaults(step, _declared_keys(entry), defaults)
            for step, entry in zip(steps, entries, strict=True)
        ),
        description,
        assignment,
        persistent,
        identity,
        unique,
    )


def _item_flow_defaults(
    items_mapping: dict[str, Any],
    path: str,
    step_profile: dict[str, str | None],
    step_role: StepRole | None,
    step_subagents: bool | None,
) -> dict[str, Any]:
    """Worker settings every per-item stage inherits unless it sets its own.

    ``items`` settings win; profile and role otherwise come from the
    ``items`` step itself.  Agent, model, and reasoning already cascade from
    that step through the compiler's execution hints.
    """
    items_path = f"{path}.items"
    role = _role(items_mapping, items_path)
    subagents = _subagents(items_mapping, items_path)
    return {
        "agent": _optional_agent(items_mapping, "agent", items_path),
        "model": _optional_string(items_mapping, "model", items_path),
        "reasoning": _optional_string(items_mapping, "reasoning", items_path),
        **(
            _profile(items_mapping, items_path)
            if "profile" in items_mapping
            else step_profile
        ),
        "role": step_role if role is None else role,
        "subagents": step_subagents if subagents is None else subagents,
    }


def _with_item_flow_defaults(
    step: StepDefinition, declared: set[str], defaults: dict[str, Any]
) -> StepDefinition:
    """Apply item-flow settings a stage neither declares nor copies from a handler."""
    changes: dict[str, Any] = {}
    for key in ("agent", "model", "reasoning"):
        if key not in declared and getattr(step, key) is None and defaults[key]:
            changes[key] = defaults[key]
    if (
        "profile" not in declared
        and step.profile is None
        and step.profile_description is None
    ):
        changes["profile"] = defaults["profile"]
        changes["profile_description"] = defaults["profile_description"]
    if "role" not in declared and step.role is None and defaults["role"]:
        changes["role"] = defaults["role"]
    if (
        "subagents" not in declared
        and step.subagents is None
        and defaults["subagents"] is not None
    ):
        changes["subagents"] = defaults["subagents"]
    return replace(step, **changes) if changes else step


def _declared_keys(entry: Any) -> set[str]:
    """Return the keys a raw step entry sets, after shorthand expansion."""
    if not isinstance(entry, dict):
        return set()
    value = entry.get("assess")
    if isinstance(value, dict):
        return set(value) | (set(entry) - {"assess"})
    return set(entry)


def _parse_assessment_outcomes(
    mapping: dict[str, Any], path: str, handlers_by_name: dict[str, HandlerDefinition]
) -> tuple[StepDefinition, ...]:
    raw = mapping.get("outcomes", {})
    return _parse_assessment_branches(raw, path, handlers_by_name, ".outcomes")


def _parse_assessment_branches(
    raw: Any,
    path: str,
    handlers_by_name: dict[str, HandlerDefinition],
    field_path: str,
) -> tuple[StepDefinition, ...]:
    """Normalize wrapped outcomes or sibling standard branches to steps."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}{field_path} must be a mapping")
    result = []
    for label, value in raw.items():
        if not isinstance(label, str) or not _NAME.fullmatch(label):
            raise ConfigurationError(
                f"{path}{field_path} keys must be normalized names"
            )
        if not isinstance(value, dict):
            raise ConfigurationError(
                f"{path}{field_path}.{label} must be a step mapping"
            )
        if "name" in value:
            raise ConfigurationError(f"{path}{field_path}.{label} must not set name")
        if "stop_workflow" in value:
            if value != {"stop_workflow": True}:
                raise ConfigurationError(
                    f"{path}{field_path}.{label}.stop_workflow must be true and "
                    "stand alone: the outcome ends the workflow and runs nothing"
                )
            result.append(StepDefinition(label, stop_workflow=True))
            continue
        result.append(
            _parse_step(
                {"name": label, **value},
                f"{path}{field_path}.{label}",
                handlers_by_name,
            )
        )
    return tuple(result)


def _parse_nested_steps(
    mapping: dict[str, Any],
    key: str,
    path: str,
    handlers_by_name: dict[str, HandlerDefinition],
    *,
    require_nonempty: bool = False,
    item_stage: bool = False,
) -> tuple[StepDefinition, ...]:
    """Parse an optional nested step list without collapsing key presence."""
    data = mapping.get(key, [])
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ConfigurationError(f"{path}.{key} must be a list")
    if require_nonempty and key in mapping and not data:
        raise ConfigurationError(f"{path}.{key} must contain at least one step")
    return tuple(
        _parse_step(
            item, f"{path}.{key}[{index}]", handlers_by_name, item_stage=item_stage
        )
        for index, item in enumerate(data)
    )


def _step_handler_reference(
    mapping: dict[str, Any],
    local: HandlerDefinition,
    referenced: HandlerDefinition,
) -> HandlerDefinition:
    """Copy a catalog handler into a step, retaining explicit step overrides.

    ``handler`` is deliberately notation-only: the result is an ordinary
    ``StepDefinition`` and therefore follows the same compilation path as an
    inline step.  A step name always remains its own identity, while a supplied
    description or handler field overrides the copied value.
    """
    action_keys = {"kind", "mcp", "argv", "shell", "args", "env", "assert", "action"}
    replaces_action = bool(action_keys & set(mapping))
    action = local.action if replaces_action else referenced.action
    if (
        action is not None
        and referenced.action is not None
        # Aliases may intentionally share a payload contract (for example a
        # project command action with a different identifier).  The action
        # capability, rather than its registry name, owns whether that payload
        # can inherit definition fields.
        and type(action.payload) is type(referenced.action.payload)
    ):
        payload = actions.get(action.identifier).override_definition(
            action.payload,
            referenced.action.payload,
            DefinitionOverrideContext(
                mapping,
                {
                    key: value
                    for key, value in mapping.get("action", {}).items()
                    if key != "type"
                }
                if isinstance(mapping.get("action"), dict)
                else {},
                "description" in mapping,
                local.name,
                local.description,
            ),
        )
        action = DefinedAction(action.identifier, payload)
    return HandlerDefinition(
        name=local.name,
        description=(
            local.description if "description" in mapping else referenced.description
        ),
        action=action,
        handlers=local.handlers if "handlers" in mapping else referenced.handlers,
        operation=local.operation if replaces_action else referenced.operation,
        provide=local.provide if "variables" in mapping else referenced.provide,
        outputs=local.outputs if "variables" in mapping else referenced.outputs,
        save_metadata=(
            local.save_metadata if "saves" in mapping else referenced.save_metadata
        ),
        update_document=(
            local.update_document if "saves" in mapping else referenced.update_document
        ),
        update_item=(
            local.update_item if "saves" in mapping else referenced.update_item
        ),
        agent=local.agent if "agent" in mapping else referenced.agent,
        model=local.model if "model" in mapping else referenced.model,
        reasoning=(local.reasoning if "reasoning" in mapping else referenced.reasoning),
        workdir=local.workdir if "workdir" in mapping else referenced.workdir,
        on_failure=local.on_failure
        if "on_failure" in mapping
        else referenced.on_failure,
        on_failure_instruction=(
            local.on_failure_instruction
            if "on_failure_instruction" in mapping
            else referenced.on_failure_instruction
        ),
    )
