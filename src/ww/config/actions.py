# SPDX-License-Identifier: GPL-3.0-or-later
"""Action, command, metadata, and hook parsing for ww-agentic-workflows.yaml."""

from __future__ import annotations

import re
from typing import Any, cast

from ww.actions import DefinedAction, actions
from ww.contracts import HookFailure, HookPhase, HookScope, RequestedActionKind
from ww.errors import ConfigurationError
from ww.extensions import is_extension_reference
from ww.operations import WorkflowHandoff
from ww.variables import is_reserved_name
from ww.workflow_config import (
    ALL,
    ALL_NAMES,
    DocumentUpdate,
    HandlerDefinition,
    HookDefinition,
    ItemFieldUpdate,
    MetadataScope,
    ProvidedVariable,
    SavedMetadata,
)
from ww.workspace import WORKDIRS, Workdir

from .values import (
    _description,
    _mapping,
    _name,
    _name_filter,
    _named_entry,
    _nonempty_string,
    _only,
    _optional_agent,
    _optional_bool,
    _optional_string,
    _unique,
)

HOOK_PHASES: tuple[HookPhase, ...] = (
    "before_start_workflow",
    "before_in_progress",
    "before_complete",
    "after_complete",
    "before_complete_workflow",
)


# ``action.type`` names that spell a core control rather than a registered action.
CORE_ACTION_TYPES = frozenset({"loop", "workflow_transition", "child_workflow"})


def _parse_handler(
    mapping: dict[str, Any],
    path: str,
    *,
    inline: bool = False,
    transition: bool = False,
    allowed_extra: set[str] | None = None,
) -> HandlerDefinition:
    """Parse one handler; ``transition`` admits the ``workflow`` key on a step."""
    _only(
        mapping,
        _handler_keys()
        | ({"workflow"} if inline or transition else set())
        | (allowed_extra or set()),
        path,
    )
    if (inline or transition) and "workflow" in mapping:
        extra = set(mapping) - {
            "name",
            "workflow",
            "description",
            "agent",
            "model",
            "reasoning",
        }
        if "children" in extra:
            raise ConfigurationError(
                f"{path} cannot combine children with a workflow transition; "
                "name the child workflow under children.workflow"
            )
        if extra:
            raise ConfigurationError(
                f"{path} workflow transition cannot contain other handler fields"
            )
        return HandlerDefinition(
            _name(mapping, path) if "name" in mapping else "start-workflow",
            _description(mapping.get("description"), path),
            operation=WorkflowHandoff(_nonempty_string(mapping, "workflow", path)),
            agent=_optional_agent(mapping, "agent", path),
            model=_optional_string(mapping, "model", path),
            reasoning=_optional_string(mapping, "reasoning", path),
        )
    if inline and "name" not in mapping:
        if "argv" in mapping:
            name = "inline-argv"
        elif "shell" in mapping:
            name = "inline-shell"
        elif "command" in mapping:
            name = "inline-command"
        elif "mcp" in mapping and isinstance(mapping["mcp"], str):
            name = f"mcp-{mapping['mcp']}"
        else:
            raise ConfigurationError(f"{path}.name is required for this handler")
    else:
        name = _name(mapping, path)
    description = _description(mapping.get("description"), f"handler {name!r}")
    if "action" in mapping:
        shorthand_keys = {
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
        }
        if shorthand_keys & set(mapping):
            raise ConfigurationError(
                f"{path} cannot combine action with shorthand fields"
            )
        raw_action = _mapping(mapping["action"], f"{path}.action")
        identifier = _nonempty_string(raw_action, "type", f"{path}.action")
        source = {key: value for key, value in raw_action.items() if key != "type"}
        if identifier in CORE_ACTION_TYPES:
            # Core controls are engine behaviour with their own keys, not
            # registry actions selected by type.
            raise ConfigurationError(
                f"{path}.action.type {identifier!r} is a core control; use "
                "`workflow`, `children`, or `loop` on the step instead"
            )
        implementation = actions.get(identifier)
        payload = implementation.parse(source, name, description, f"{path}.action")
        implementation.validate(payload, f"{path}.action")
        action = DefinedAction(identifier, payload)
        return HandlerDefinition(
            name=name,
            description=description,
            action=action,
            provide=_parse_provide(mapping.get("provide", []), f"handler {name!r}"),
            outputs=_parse_outputs(mapping.get("outputs", []), f"handler {name!r}"),
            save_metadata=_parse_save_metadata(
                mapping.get("update_metadata", []), f"handler {name!r}"
            ),
            update_document=_parse_update_document(
                mapping.get("update_document", []), f"handler {name!r}"
            ),
            update_item=_parse_update_item(
                mapping.get("update_item", []), f"handler {name!r}"
            ),
            agent=_optional_agent(mapping, "agent", f"handler {name!r}"),
            model=_optional_string(mapping, "model", f"handler {name!r}"),
            reasoning=_optional_string(mapping, "reasoning", f"handler {name!r}"),
            workdir=_optional_workdir(mapping, f"handler {name!r}"),
        )
    explicit: list[RequestedActionKind] = [
        key for key in ("skill", "slash_command") if key in mapping
    ]
    prompt_value = mapping.get("prompt")
    mcp_value = mapping.get("mcp")
    if "prompt" in mapping and isinstance(prompt_value, bool):
        explicit.append("prompt")
    if len(explicit) > 1:
        raise ConfigurationError(f"handler {name!r} declares conflicting kinds")
    if any(mapping.get(key) is not True for key in explicit):
        raise ConfigurationError(f"handler {name!r} kind flags must be true")
    has_command = bool({"command", "argv", "shell"} & set(mapping))
    if {"args", "env", "assert", "idempotent"} & set(mapping) and not has_command:
        actions.get("cli").parse(mapping, name, description, f"handler {name!r}")
    if has_command and explicit:
        raise ConfigurationError(
            f"handler {name!r} cannot combine command and a kind flag"
        )
    if mcp_value is not None:
        if not isinstance(mcp_value, str) or not mcp_value.strip():
            raise ConfigurationError(f"handler {name!r} mcp must be non-empty")
        if has_command or explicit:
            raise ConfigurationError(
                f"handler {name!r} cannot combine mcp with another action"
            )
        requested_kind: RequestedActionKind | None = "mcp"
    elif "prompt" in mapping and not isinstance(prompt_value, bool):
        raise ConfigurationError(f"handler {name!r} prompt must be true")
    else:
        requested_kind = explicit[0] if explicit else None
    action_kind = (
        "cli" if has_command else "mcp" if mcp_value is not None else requested_kind
    )
    typed_action = None
    if action_kind is not None:
        implementation = actions.get(action_kind)
        payload = implementation.parse(mapping, name, description, path)
        implementation.validate(payload, path)
        typed_action = DefinedAction(action_kind, payload)
    return HandlerDefinition(
        name=name,
        description=description,
        action=typed_action,
        provide=_parse_provide(mapping.get("provide", []), f"handler {name!r}"),
        outputs=_parse_outputs(mapping.get("outputs", []), f"handler {name!r}"),
        save_metadata=_parse_save_metadata(
            mapping.get("update_metadata", []), f"handler {name!r}"
        ),
        update_document=_parse_update_document(
            mapping.get("update_document", []), f"handler {name!r}"
        ),
        update_item=_parse_update_item(
            mapping.get("update_item", []), f"handler {name!r}"
        ),
        agent=_optional_agent(mapping, "agent", f"handler {name!r}"),
        model=_optional_string(mapping, "model", f"handler {name!r}"),
        reasoning=_optional_string(mapping, "reasoning", f"handler {name!r}"),
        workdir=_optional_workdir(mapping, f"handler {name!r}"),
    )


def _optional_workdir(mapping: dict[str, Any], path: str) -> Workdir | None:
    if "workdir" not in mapping:
        return None
    value = mapping["workdir"]
    if value not in WORKDIRS:
        raise ConfigurationError(
            f"{path}.workdir must be one of: {', '.join(WORKDIRS)}"
        )
    return cast(Workdir, value)


def _handler_keys() -> set[str]:
    return {
        "name",
        "description",
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
        "agent",
        "command",
        "provide",
        "outputs",
        "update_metadata",
        "update_document",
        "update_item",
        "model",
        "reasoning",
        "action",
        "workdir",
    }


def _parse_hooks(data: Any, scope: HookScope, path: str) -> tuple[HookDefinition, ...]:
    if data is None:
        return ()
    mapping = _mapping(data, path)
    _only(mapping, set(HOOK_PHASES), path)
    result: list[HookDefinition] = []
    for phase in HOOK_PHASES:
        entries = mapping.get(phase, [])
        if not isinstance(entries, list):
            raise ConfigurationError(f"{path}.{phase} must be a list")
        for index, entry in enumerate(entries):
            result.extend(_parse_hook(entry, phase, scope, f"{path}.{phase}[{index}]"))
    return tuple(result)


HOOK_FAILURES: tuple[HookFailure, ...] = ("fix", "operator")


def _parse_hook(
    data: Any, phase: HookPhase, scope: HookScope, path: str
) -> tuple[HookDefinition, ...]:
    mapping = _mapping(data, path)
    allowed = _handler_keys() | {"handlers", "workflow", "on_failure"}
    if scope in {"global", "workflow"} and phase not in {
        "before_start_workflow",
        "before_complete_workflow",
    }:
        allowed.add("steps")
    if scope == "global":
        allowed.add("workflows")
    mapping = _named_entry(
        mapping, path, allowed=allowed, ignored={"workflows", "steps", "on_failure"}
    )
    _only(mapping, allowed, path)
    on_failure = _on_failure(mapping, path, "operator")
    if "handlers" in mapping:
        action_keys = set(mapping) & (_handler_keys() | {"workflow"})
        if action_keys:
            raise ConfigurationError(
                f"{path} cannot combine handlers with handler key(s): "
                + ", ".join(sorted(action_keys))
            )
        raw_handlers = mapping["handlers"]
        if not isinstance(raw_handlers, list) or not raw_handlers:
            raise ConfigurationError(f"{path}.handlers must be a non-empty list")
        references = tuple(
            _parse_hook_member(item, f"{path}.handlers[{index}]", on_failure)
            for index, item in enumerate(raw_handlers)
        )
    else:
        action = {
            key: value
            for key, value in mapping.items()
            if key not in {"workflows", "steps", "on_failure"}
        }
        if not action:
            raise ConfigurationError(f"{path} requires a handler action")
        references = ((_parse_hook_handler(action, path), on_failure),)

    workflows = _name_filter(
        mapping.get("workflows", ALL_NAMES), f"{path}.workflows", empty=ALL
    )
    steps = _name_filter(mapping.get("steps", ALL_NAMES), f"{path}.steps", empty=ALL)
    return tuple(
        HookDefinition(
            phase=phase,
            handler=reference,
            workflows=workflows,
            steps=steps,
            scope=scope,
            path=path,
            on_failure=failure,
        )
        for reference, failure in references
    )


def _on_failure(
    mapping: dict[str, Any], path: str, default: HookFailure
) -> HookFailure:
    value = mapping.get("on_failure", default)
    if value not in HOOK_FAILURES:
        raise ConfigurationError(
            f"{path}.on_failure must be one of: " + ", ".join(HOOK_FAILURES)
        )
    return cast(HookFailure, value)


def _parse_hook_member(
    data: Any, path: str, group_failure: HookFailure
) -> tuple[HandlerDefinition, HookFailure]:
    """One member of a hook's ``handlers`` list and its own ``on_failure``."""
    mapping = _named_entry(
        _mapping(data, path),
        path,
        allowed=_handler_keys() | {"workflow", "on_failure"},
        ignored={"on_failure"},
    )
    failure = _on_failure(mapping, path, group_failure)
    handler = {key: value for key, value in mapping.items() if key != "on_failure"}
    return _parse_hook_handler(handler, path), failure


def _parse_hook_handler(data: Any, path: str) -> HandlerDefinition:
    mapping = _named_entry(
        _mapping(data, path), path, allowed=_handler_keys() | {"workflow"}
    )
    if _bare_extension_reference(mapping):
        return HandlerDefinition(
            mapping["name"], workdir=_optional_workdir(mapping, path)
        )
    return _parse_handler(mapping, path, inline=True)


def _bare_extension_reference(mapping: dict[str, Any]) -> bool:
    """Whether ``mapping`` names an extension handler as is.

    Such an entry carries at most the directory it works in; everything else
    about the handler is the extension's to define.
    """
    name = mapping.get("name")
    return (
        isinstance(name, str)
        and is_extension_reference(name)
        and set(mapping) <= {"name", "workdir"}
    )


def _parse_update_document(data: Any, path: str) -> tuple[DocumentUpdate, ...]:
    """Parse ``update_document``: named entries whose text instructs the update."""
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ConfigurationError(f"{path}.update_document must be a list")
    result = []
    for index, item in enumerate(data):
        item_path = f"{path}.update_document[{index}]"
        mapping = _named_entry(_mapping(item, item_path), item_path)
        _only(mapping, {"name", "description"}, item_path)
        result.append(
            DocumentUpdate(
                _name(mapping, item_path),
                _description(mapping.get("description"), item_path),
            )
        )
    _unique((item.name for item in result), f"document update in {path}")
    return tuple(result)


def _parse_update_item(data: Any, path: str) -> tuple[ItemFieldUpdate, ...]:
    """Parse ``update_item``: named fields whose text says what to put there."""
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ConfigurationError(f"{path}.update_item must be a list")
    result = []
    for index, item in enumerate(data):
        item_path = f"{path}.update_item[{index}]"
        mapping = _named_entry(_mapping(item, item_path), item_path)
        _only(mapping, {"name", "description"}, item_path)
        try:
            result.append(
                ItemFieldUpdate(
                    _name(mapping, item_path),
                    _description(mapping.get("description"), item_path),
                )
            )
        except ValueError as error:
            raise ConfigurationError(f"{item_path}: {error}") from error
    _unique((item.name for item in result), f"item field in {path}")
    return tuple(result)


def _parse_provide(data: Any, path: str) -> tuple[ProvidedVariable, ...]:
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ConfigurationError(f"{path}.provide must be a list")
    result = []
    for index, item in enumerate(data):
        item_path = f"{path}.provide[{index}]"
        mapping = _named_entry(_mapping(item, item_path), item_path)
        _only(mapping, {"name", "description"}, item_path)
        name = _name(mapping, item_path)
        if is_reserved_name(name):
            raise ConfigurationError(f"{path}.provide name {name!r} is reserved")
        result.append(
            ProvidedVariable(
                name,
                _description(mapping.get("description"), item_path),
            )
        )
    _unique((item.name for item in result), f"provided value in {path}")
    return tuple(result)


def _parse_outputs(data: Any, path: str) -> tuple[str, ...]:
    """Parse values produced by an automatic action's successful result."""
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ConfigurationError(f"{path}.outputs must be a list")
    outputs: list[str] = []
    for index, value in enumerate(data):
        output_path = f"{path}.outputs[{index}]"
        if not isinstance(value, str) or not value:
            raise ConfigurationError(f"{output_path} must be a non-empty string")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", value):
            raise ConfigurationError(
                f"{output_path} must be a normalized variable name"
            )
        if is_reserved_name(value):
            raise ConfigurationError(f"{output_path} {value!r} is reserved")
        outputs.append(value)
    _unique(outputs, f"output value in {path}")
    return tuple(outputs)


def _parse_save_metadata(data: Any, path: str) -> tuple[SavedMetadata, ...]:
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ConfigurationError(f"{path}.update_metadata must be a list")
    result = []
    for index, item in enumerate(data):
        item_path = f"{path}.update_metadata[{index}]"
        mapping = _named_entry(_mapping(item, item_path), item_path)
        _only(mapping, {"name", "key", "description", "scope", "append"}, item_path)
        name = _name(mapping, item_path)
        key = _nonempty_string(mapping, "key", item_path)
        if not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*", key
        ):
            raise ConfigurationError(f"{item_path}.key must be a dotted metadata path")
        result.append(
            SavedMetadata(
                name,
                key,
                _description(mapping.get("description"), item_path),
                _metadata_scope(mapping.get("scope", "task"), item_path),
                append=_optional_bool(mapping, "append", item_path) or False,
            )
        )
    _unique((item.name for item in result), f"saved metadata name in {path}")
    _unique(
        ((item.scope, item.key) for item in result),
        f"saved metadata scope and key in {path}",
    )
    for scope in ("task", "project"):
        keys = [item.key for item in result if item.scope == scope]
        for index, key in enumerate(keys):
            if any(
                key.startswith(f"{other}.") or other.startswith(f"{key}.")
                for other in keys[index + 1 :]
            ):
                raise ConfigurationError(
                    f"saved metadata paths in {path} cannot overlap within "
                    f"the {scope} scope"
                )
    return tuple(result)


def _metadata_scope(value: Any, path: str) -> MetadataScope:
    if value not in {"task", "project"}:
        raise ConfigurationError(f"{path}.scope must be 'task' or 'project'")
    return cast(MetadataScope, value)
