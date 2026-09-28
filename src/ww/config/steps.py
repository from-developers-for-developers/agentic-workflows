# SPDX-License-Identifier: GPL-3.0-or-later
"""Recursive step parsing and reusable handler catalog parsing."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, get_args

from ww.actions import (
    DefinedAction,
    DefinitionOverrideContext,
    Prompt,
    actions,
)
from ww.contracts import ItemAssignment, LoopAssignment
from ww.errors import ConfigurationError
from ww.extensions import is_extension_reference
from ww.items import FIELD_NAME
from ww.validation import is_positive_int
from ww.workflow_config import (
    ChoiceDefinition,
    HandlerDefinition,
    ItemFlow,
    StepDefinition,
)

from .actions import _handler_keys, _parse_handler, _parse_hooks
from .values import (
    _NAME,
    _description,
    _mapping,
    _named_entry,
    _nonempty_string,
    _only,
    _optional_agent,
    _optional_bool,
    _optional_string,
    _profile,
    _unique,
)

STEP_ONLY_KEYS: set[str] = {
    "hooks",
    "steps",
    "loop",
    "loop_max_times",
    "loop_assignment",
    "break",
    "continue",
    "subagents",
    "interactive",
    "choices",
    "ui",
    "profile",
    "items",
    "process_item",
    "resolve_item",
    "report_item",
    "depends_on",
    "artifact",
    "children",
    "workflow_per_child",
    "handler",
    "question",
    "outcomes",
}

ITEM_FLOW_KEYS = {
    "description",
    "steps",
    "item_assignment",
    "shared",
    "process_item",
    "resolve_item",
    "report_item",
    "update_metadata",
    "update_document",
    "update_item",
    "interactive",
    "choices",
    "ui",
    "identity",
    "unique",
    "agent",
    "model",
    "reasoning",
    "profile",
    "subagents",
}
_ITEM_FLOW_SETTINGS = ("agent", "model", "reasoning", "profile", "subagents")
BUILTIN_ITEM_STEP_NAME = "handle-item"
BUILTIN_ITEM_STEP_PROMPT = (
    "Handle this item end to end: analyze it, resolve it, and report the outcome."
)
# Under ``items``, the marker keys carry guidance for one phase of the built-in
# stage instead of marking a configured step.
_ITEM_PHASE_GUIDANCE = (
    ("process_item", "When analyzing it"),
    ("resolve_item", "When resolving it"),
    ("report_item", "When reporting the outcome"),
)


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
        "skill",
        "slash_command",
        "prompt",
        "mcp",
        "argv",
        "shell",
        "args",
        "env",
        "assert",
        "idempotent",
        "command",
        "action",
        "provide",
        "outputs",
        "update_metadata",
        "update_document",
        "steps",
        "loop",
        "items",
        "children",
        "workflow_per_child",
        "workflow",
        "question",
        "outcomes",
        "process_item",
        "resolve_item",
        "report_item",
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
    if "stop" in mapping:
        raise ConfigurationError(f"{path}.stop is obsolete; use break")
    _only(mapping, _handler_keys() | STEP_ONLY_KEYS | {"workflow"}, path)
    base = (
        HandlerDefinition(mapping["name"])
        if set(mapping) == {"name"} and is_extension_reference(mapping["name"])
        else _parse_handler(
            mapping, path, transition=True, allowed_extra=STEP_ONLY_KEYS
        )
    )
    assessment_question = (
        _nonempty_string(mapping, "question", path) if "question" in mapping else None
    )
    if assessment_question is not None and mapping["name"] != "assess":
        raise ConfigurationError(f"{path}.question is only valid for an assess step")
    if "outcomes" in mapping and assessment_question is None:
        raise ConfigurationError(f"{path}.outcomes requires an assess question")
    outcomes = _parse_assessment_outcomes(mapping, path, handlers_by_name)
    if assessment_question is not None:
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
        base = HandlerDefinition(
            name="assess",
            description=assessment_question,
            action=DefinedAction("prompt", Prompt(assessment_question)),
        )
    referenced_step: StepDefinition | None = None
    if "handler" in mapping:
        handler_name = mapping["handler"]
        if not isinstance(handler_name, str) or not _NAME.fullmatch(handler_name):
            raise ConfigurationError(f"{path}.handler must be a handler name")
        try:
            referenced_handler = handlers_by_name[handler_name]
        except KeyError as error:
            raise ConfigurationError(
                f"{path}.handler references unknown handler {handler_name!r}"
            ) from error
        base = _step_handler_reference(mapping, base, referenced_handler)
        if isinstance(referenced_handler, StepDefinition):
            referenced_step = referenced_handler
    local_children = _parse_nested_steps(mapping, "steps", path, handlers_by_name)
    local_loop_steps = _parse_nested_steps(
        mapping, "loop", path, handlers_by_name, require_nonempty=True
    )
    loop_max_times: int | None = mapping.get("loop_max_times")
    if loop_max_times is not None and (not is_positive_int(loop_max_times)):
        raise ConfigurationError(f"{path}.loop_max_times must be a positive integer")
    if "loop_max_times" in mapping and not local_loop_steps:
        raise ConfigurationError(f"{path}.loop_max_times requires a loop")
    loop_assignment: LoopAssignment | None = mapping.get("loop_assignment")
    if "loop_assignment" in mapping:
        if loop_assignment not in get_args(LoopAssignment):
            raise ConfigurationError(
                f"{path}.loop_assignment must be one of: "
                + ", ".join(get_args(LoopAssignment))
            )
        if not local_loop_steps:
            raise ConfigurationError(f"{path}.loop_assignment requires a loop")
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
    children = (
        local_children
        if "steps" in mapping or referenced_step is None
        else referenced_step.child_steps
    )
    loop_steps = (
        local_loop_steps
        if "loop" in mapping or referenced_step is None
        else referenced_step.loop_steps
    )
    loop_max_times = (
        loop_max_times
        if "loop_max_times" in mapping or referenced_step is None
        else referenced_step.loop_max_times
    )
    loop_assignment = (
        loop_assignment
        if "loop_assignment" in mapping or referenced_step is None
        else referenced_step.loop_assignment
    )
    loop_break = (
        loop_break
        if "break" in mapping or referenced_step is None
        else referenced_step.loop_break
    )
    loop_continue = (
        loop_continue
        if "continue" in mapping or referenced_step is None
        else referenced_step.loop_continue
    )
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
            any(
                key in mapping
                for key in ("process_item", "resolve_item", "report_item")
            ),
            "children" in mapping,
            "workflow_per_child" in mapping,
            "depends_on" in mapping,
        )
    ):
        raise ConfigurationError(
            f"{path} loop wrapper cannot also declare an action or collection"
        )
    operations = [
        key for key in ("process_item", "resolve_item", "report_item") if key in mapping
    ]
    if len(operations) > 1 or (operations and mapping[operations[0]] is not None):
        raise ConfigurationError(
            f"{path} item operation must be exactly one null marker"
        )
    if "children" in mapping and mapping["children"] is not None:
        raise ConfigurationError(f"{path}.children must be null")
    child_workflow = mapping.get("workflow_per_child")
    if child_workflow is not None and (
        not isinstance(child_workflow, str) or not _NAME.fullmatch(child_workflow)
    ):
        raise ConfigurationError(f"{path}.workflow_per_child must be a workflow name")
    if "children" in mapping and child_workflow is not None:
        raise ConfigurationError(f"{path} cannot collect children and run them")
    artifact = mapping.get("artifact", True)
    if not isinstance(artifact, bool):
        raise ConfigurationError(f"{path}.artifact must be true or false")
    subagents = mapping.get("subagents", True)
    if not isinstance(subagents, bool):
        raise ConfigurationError(f"{path}.subagents must be true or false")
    interactive = mapping.get("interactive", False)
    if not isinstance(interactive, bool):
        raise ConfigurationError(f"{path}.interactive must be true or false")
    choices = _parse_choices(mapping.get("choices"), path)
    ui = mapping.get("ui", False)
    if not isinstance(ui, bool):
        raise ConfigurationError(f"{path}.ui must be true or false")
    depends_on = mapping.get("depends_on")
    if depends_on is not None and (
        not isinstance(depends_on, str) or not _NAME.fullmatch(depends_on)
    ):
        raise ConfigurationError(f"{path}.depends_on must be a normalized step name")
    hooks = (
        _parse_hooks(mapping.get("hooks", {}), "step", f"{path}.hooks")
        if "hooks" in mapping or referenced_step is None
        else referenced_step.hooks
    )
    profile = (
        _profile(mapping, path)
        if "profile" in mapping or referenced_step is None
        else {
            "profile": referenced_step.profile,
            "profile_description": referenced_step.profile_description,
        }
    )
    subagents = (
        subagents
        if "subagents" in mapping or referenced_step is None
        else referenced_step.subagents
    )
    interactive = (
        interactive
        if "interactive" in mapping or referenced_step is None
        else referenced_step.interactive
    )
    choices = (
        choices
        if "choices" in mapping or referenced_step is None
        else referenced_step.choices
    )
    if choices and not interactive:
        raise ConfigurationError(
            f"{path}.choices are offered to the operator, so they require "
            "interactive: true"
        )
    ui = ui if "ui" in mapping or referenced_step is None else referenced_step.ui
    if ui and not interactive:
        raise ConfigurationError(
            f"{path}.ui serves the operator page for a conversation, so it "
            "requires interactive: true"
        )
    if ui and not item_stage:
        raise ConfigurationError(
            f"{path}.ui is offered on per-item stages only; declare it under items"
        )
    items = (
        _parse_items(mapping, path, handlers_by_name, profile, subagents)
        if "items" in mapping
        else referenced_step.items
        if referenced_step is not None
        else None
    )
    containers = sum(bool(value) for value in (children, loop_steps, items is not None))
    if containers > 1:
        raise ConfigurationError(f"{path} cannot combine steps, loop, and items")
    if items is not None and operations:
        raise ConfigurationError(
            f"{path} cannot combine items with an item operation marker"
        )
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
        subagents=subagents,
        interactive=interactive,
        choices=choices,
        ui=ui,
        profile=profile["profile"],
        profile_description=profile["profile_description"],
        hooks=hooks,
        child_steps=children,
        loop_steps=loop_steps,
        loop_max_times=loop_max_times,
        loop_assignment=loop_assignment,
        loop_break=loop_break,
        loop_continue=loop_continue,
        items=items,
        item_operation=(
            operations[0]
            if operations
            else (
                referenced_step.item_operation if referenced_step is not None else None
            )
        ),
        artifact=(
            artifact
            if "artifact" in mapping or referenced_step is None
            else referenced_step.artifact
        ),
        collect_children=(
            "children" in mapping
            if "children" in mapping or referenced_step is None
            else referenced_step.collect_children
        ),
        child_workflow=(
            child_workflow
            if "workflow_per_child" in mapping or referenced_step is None
            else referenced_step.child_workflow
        ),
        artifact_dependency=(
            depends_on
            if "depends_on" in mapping or referenced_step is None
            else referenced_step.artifact_dependency
        ),
        assessment_question=assessment_question,
        assessment_outcomes=outcomes,
    )


def _parse_items(
    mapping: dict[str, Any],
    path: str,
    handlers_by_name: dict[str, HandlerDefinition],
    step_profile: dict[str, str | None],
    step_subagents: bool,
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
    shared = value.get("shared", False)
    if not isinstance(shared, bool):
        raise ConfigurationError(f"{items_path}.shared must be true or false")
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
    assignment = value.get("item_assignment", "all_items")
    if assignment not in get_args(ItemAssignment):
        raise ConfigurationError(
            f"{items_path}.item_assignment must be one of: "
            + ", ".join(get_args(ItemAssignment))
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
        key
        for key in (
            "update_metadata",
            "update_document",
            "update_item",
            "interactive",
            "choices",
            "ui",
        )
        if key in value
    ]
    if folded and "steps" in value:
        raise ConfigurationError(
            f"{items_path}.{folded[0]} belongs to the built-in handle-item stage; "
            f"declare {folded[0]} on configured steps directly"
        )
    collect_only = "steps" in value and value["steps"] in ([], None)
    if collect_only:
        configured = [
            key for key in ("item_assignment", *_ITEM_FLOW_SETTINGS) if key in value
        ]
        if configured:
            raise ConfigurationError(
                f"{items_path} collects without per-item steps, so "
                + ", ".join(configured)
                + " has no effect"
            )
        return ItemFlow((), description, assignment, shared, identity, unique)
    defaults = _item_flow_defaults(value, path, step_profile, step_subagents)
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
            f"{items_path} may answer one stage on the operator page; found ui: "
            "true on " + ", ".join(pages)
        )
    return ItemFlow(
        tuple(
            _with_item_flow_defaults(step, _declared_keys(entry), defaults)
            for step, entry in zip(steps, entries, strict=True)
        ),
        description,
        assignment,
        shared,
        identity,
        unique,
    )


def _item_flow_defaults(
    items_mapping: dict[str, Any],
    path: str,
    step_profile: dict[str, str | None],
    step_subagents: bool,
) -> dict[str, Any]:
    """Worker settings every per-item stage inherits unless it sets its own.

    ``items`` settings win; profile and subagents otherwise come from the
    ``items`` step itself.  Agent, model, and reasoning already cascade from
    that step through the compiler's execution hints.
    """
    items_path = f"{path}.items"
    subagents = _optional_bool(items_mapping, "subagents", items_path)
    return {
        "agent": _optional_agent(items_mapping, "agent", items_path),
        "model": _optional_string(items_mapping, "model", items_path),
        "reasoning": _optional_string(items_mapping, "reasoning", items_path),
        **(
            _profile(items_mapping, items_path)
            if "profile" in items_mapping
            else step_profile
        ),
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
    if (
        "subagents" not in declared
        and step.subagents
        and defaults["subagents"] is False
    ):
        changes["subagents"] = False
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
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}.outcomes must be a mapping")
    result = []
    for label, value in raw.items():
        if not isinstance(label, str) or not _NAME.fullmatch(label):
            raise ConfigurationError(f"{path}.outcomes keys must be normalized names")
        if not isinstance(value, dict):
            raise ConfigurationError(f"{path}.outcomes.{label} must be a step mapping")
        if "name" in value:
            raise ConfigurationError(f"{path}.outcomes.{label} must not set name")
        if "stop_workflow" in value:
            if value != {"stop_workflow": True}:
                raise ConfigurationError(
                    f"{path}.outcomes.{label}.stop_workflow must be true and "
                    "stand alone: the outcome ends the workflow and runs nothing"
                )
            result.append(StepDefinition(label, stop_workflow=True))
            continue
        result.append(
            _parse_step(
                {"name": label, **value},
                f"{path}.outcomes.{label}",
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
    action_keys = {
        "skill",
        "slash_command",
        "mcp",
        "argv",
        "shell",
        "args",
        "env",
        "assert",
        "command",
    }
    prompt_is_action = isinstance(mapping.get("prompt"), bool)
    replaces_action = (
        bool(action_keys & set(mapping)) or prompt_is_action or "action" in mapping
    )
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
        operation=local.operation if replaces_action else referenced.operation,
        provide=local.provide if "provide" in mapping else referenced.provide,
        save_metadata=(
            local.save_metadata
            if "update_metadata" in mapping
            else referenced.save_metadata
        ),
        update_document=(
            local.update_document
            if "update_document" in mapping
            else referenced.update_document
        ),
        update_item=(
            local.update_item if "update_item" in mapping else referenced.update_item
        ),
        outputs=local.outputs if "outputs" in mapping else referenced.outputs,
        agent=local.agent if "agent" in mapping else referenced.agent,
        model=local.model if "model" in mapping else referenced.model,
        reasoning=(local.reasoning if "reasoning" in mapping else referenced.reasoning),
    )
