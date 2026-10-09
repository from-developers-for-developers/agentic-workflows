# SPDX-License-Identifier: GPL-3.0-or-later
"""Action, command, metadata, and hook parsing for ww.yaml."""

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
    "before_start",
    "before_complete",
    "after_complete",
    "before_complete_workflow",
)


# ``action.type`` names that spell a core control rather than a registered action.
CORE_ACTION_TYPES = frozenset({"workflow_transition", "child_workflow"})
# The agent action kinds ``kind`` chooses between.
ACTION_KINDS: tuple[RequestedActionKind, ...] = ("skill", "slash_command", "prompt")
# The prefixes of ``saves`` entries; the prefix is the kind and scope of the
# saved value, the rest its storage path.
_SAVE_PREFIXES = ("metadata.", "project_metadata.", "documents.", "item.field.")
# A dotted metadata path, e.g. "github.owner"; "github..owner" does not match.
_METADATA_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*")
# A variable name, dots and hyphens allowed, e.g. "ww.task-id".
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


def _parse_handler(
    mapping: dict[str, Any],
    path: str,
    *,
    inline: bool = False,
    transition: bool = False,
    allowed_extra: set[str] | None = None,
) -> HandlerDefinition:
    """Parse one handler; ``transition`` admits the ``handoff_to`` key on a step."""
    _only(
        mapping,
        _handler_keys()
        | ({"handoff_to"} if inline or transition else set())
        | (allowed_extra or set()),
        path,
    )
    if "handlers" in mapping:
        return _parse_automatic_group(mapping, path, inline=inline)
    if (inline or transition) and "handoff_to" in mapping:
        extra = set(mapping) - {
            "name",
            "handoff_to",
            "start_child",
            "description",
            "agent",
            "model",
            "reasoning",
        }
        if "children" in extra:
            raise ConfigurationError(
                f"{path} cannot combine children with handoff_to; "
                "name the child workflow under children.workflow"
            )
        if extra:
            raise ConfigurationError(
                f"{path} handoff_to cannot contain other handler fields"
            )
        return HandlerDefinition(
            _name(mapping, path) if "name" in mapping else "start-workflow",
            _description(mapping.get("description"), path),
            operation=WorkflowHandoff(_nonempty_string(mapping, "handoff_to", path)),
            agent=_optional_agent(mapping, "agent", path),
            model=_optional_string(mapping, "model", path),
            reasoning=_optional_string(mapping, "reasoning", path),
        )
    if inline and "name" not in mapping:
        if "argv" in mapping:
            name = "inline-argv"
        elif "shell" in mapping:
            name = "inline-shell"
        elif "mcp" in mapping and isinstance(mapping["mcp"], str):
            name = f"mcp-{mapping['mcp']}"
        else:
            raise ConfigurationError(f"{path}.name is required for this handler")
    else:
        name = _name(mapping, path)
    description = _description(mapping.get("description"), f"handler {name!r}")
    if "action" in mapping:
        shorthand_keys = {
            "kind",
            "mcp",
            "argv",
            "shell",
            "args",
            "env",
            "assert",
            "idempotent",
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
                "`handoff_to` or `children` on the step instead"
            )
        implementation = actions.get(identifier)
        payload = implementation.parse(source, name, description, f"{path}.action")
        implementation.validate(payload, f"{path}.action")
        action = DefinedAction(identifier, payload)
        return HandlerDefinition(
            name=name,
            description=description,
            action=action,
            **_failure_policy(mapping, path),
            **handler_values(mapping, f"handler {name!r}"),
            agent=_optional_agent(mapping, "agent", f"handler {name!r}"),
            model=_optional_string(mapping, "model", f"handler {name!r}"),
            reasoning=_optional_string(mapping, "reasoning", f"handler {name!r}"),
            workdir=_optional_workdir(mapping, f"handler {name!r}"),
        )
    explicit: list[RequestedActionKind] = []
    if "kind" in mapping:
        kind = mapping["kind"]
        if kind not in ACTION_KINDS:
            raise ConfigurationError(
                f"handler {name!r} kind must be one of: " + ", ".join(ACTION_KINDS)
            )
        explicit.append(cast(RequestedActionKind, kind))
    mcp_value = mapping.get("mcp")
    has_command = bool({"argv", "shell"} & set(mapping))
    if {"args", "env", "assert", "idempotent"} & set(mapping) and not has_command:
        actions.get("cli").parse(mapping, name, description, f"handler {name!r}")
    if has_command and explicit:
        raise ConfigurationError(f"handler {name!r} cannot combine a command and kind")
    if mcp_value is not None:
        if not isinstance(mcp_value, str) or not mcp_value.strip():
            raise ConfigurationError(f"handler {name!r} mcp must be non-empty")
        if has_command or explicit:
            raise ConfigurationError(
                f"handler {name!r} cannot combine mcp with another action"
            )
        requested_kind: RequestedActionKind | None = "mcp"
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
        **_failure_policy(mapping, path),
        **handler_values(mapping, f"handler {name!r}"),
        agent=_optional_agent(mapping, "agent", f"handler {name!r}"),
        model=_optional_string(mapping, "model", f"handler {name!r}"),
        reasoning=_optional_string(mapping, "reasoning", f"handler {name!r}"),
        workdir=_optional_workdir(mapping, f"handler {name!r}"),
    )


def _parse_automatic_group(
    mapping: dict[str, Any], path: str, *, inline: bool
) -> HandlerDefinition:
    allowed = {
        "name",
        "description",
        "handlers",
        "workdir",
        "agent",
        "model",
        "reasoning",
        "on_failure",
        "on_failure_instruction",
        "handler",
        "hooks",
        "profile",
        "subagents",
        "artifact",
    }
    extra = set(mapping) - allowed
    if extra:
        raise ConfigurationError(
            f"{path}.handlers cannot combine with: " + ", ".join(sorted(extra))
        )
    entries = mapping["handlers"]
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError(f"{path}.handlers must be a non-empty list")
    members = tuple(
        _parse_hook_handler(entry, f"{path}.handlers[{index}]")
        for index, entry in enumerate(entries)
    )
    return HandlerDefinition(
        _name(mapping, path) if "name" in mapping else "inline-handlers",
        description=_description(mapping.get("description"), path),
        handlers=members,
        workdir=_optional_workdir(mapping, path),
        agent=_optional_agent(mapping, "agent", path),
        model=_optional_string(mapping, "model", path),
        reasoning=_optional_string(mapping, "reasoning", path),
        **_failure_policy(mapping, path),
    )


def _failure_policy(mapping: dict[str, Any], path: str) -> dict[str, Any]:
    return {
        "on_failure": _on_failure(mapping, path, "operator")
        if "on_failure" in mapping
        else None,
        "on_failure_instruction": _optional_string(
            mapping, "on_failure_instruction", path
        ),
    }


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
        "handlers",
        "kind",
        "mcp",
        "argv",
        "shell",
        "args",
        "env",
        "assert",
        "idempotent",
        "on_failure",
        "on_failure_instruction",
        "agent",
        "variables",
        "saves",
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
    allowed = _handler_keys() | {"handlers", "handoff_to", "on_failure"}
    if scope in {"global", "workflow"} and phase not in {
        "before_start_workflow",
        "before_complete_workflow",
    }:
        allowed.add("steps")
    if scope == "global":
        allowed.add("workflows")
    mapping = _named_entry(
        mapping,
        path,
        allowed=allowed,
        ignored={"workflows", "steps", "on_failure", "on_failure_instruction"},
    )
    _only(mapping, allowed, path)
    on_failure = _on_failure(mapping, path, "operator")
    _optional_string(mapping, "on_failure_instruction", path)
    if "handlers" in mapping:
        action_keys = set(mapping) & (
            (_handler_keys() - {"handlers", "on_failure", "on_failure_instruction"})
            | {"handoff_to"}
        )
        if action_keys:
            raise ConfigurationError(
                f"{path} cannot combine handlers with handler key(s): "
                + ", ".join(sorted(action_keys))
            )
        raw_handlers = mapping["handlers"]
        if not isinstance(raw_handlers, list) or not raw_handlers:
            raise ConfigurationError(f"{path}.handlers must be a non-empty list")
        references = tuple(
            _parse_hook_member(
                item,
                f"{path}.handlers[{index}]",
                on_failure,
                mapping.get("on_failure_instruction"),
            )
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
    data: Any,
    path: str,
    group_failure: HookFailure,
    group_instruction: str | None = None,
) -> tuple[HandlerDefinition, HookFailure]:
    """One member of a hook's ``handlers`` list and its own ``on_failure``."""
    mapping = _named_entry(
        _mapping(data, path),
        path,
        allowed=_handler_keys() | {"handoff_to", "on_failure"},
        ignored={"on_failure"},
    )
    failure = _on_failure(mapping, path, group_failure)
    handler = {key: value for key, value in mapping.items() if key != "on_failure"}
    if "on_failure_instruction" not in handler and group_instruction is not None:
        handler["on_failure_instruction"] = group_instruction
    return _parse_hook_handler(handler, path), failure


def _parse_hook_handler(data: Any, path: str) -> HandlerDefinition:
    mapping = _named_entry(
        _mapping(data, path),
        path,
        allowed=_handler_keys() | {"handoff_to"},
    )
    if _bare_extension_reference(mapping):
        return extension_reference(mapping, path)
    return _parse_handler(mapping, path, inline=True)


def _bare_extension_reference(mapping: dict[str, Any]) -> bool:
    """Whether ``mapping`` names an extension handler as is.

    Such an entry carries at most the directory it works in and the
    arguments it runs with; everything else about the handler is the
    extension's to define.
    """
    name = mapping.get("name")
    return (
        isinstance(name, str)
        and is_extension_reference(name)
        and set(mapping) <= {"name", "workdir", "args"}
    )


def extension_reference(mapping: dict[str, Any], path: str) -> HandlerDefinition:
    """A bare reference to an extension handler, with its ``args``."""
    arguments = mapping.get("args", [])
    if not isinstance(arguments, list) or not all(
        isinstance(argument, str) for argument in arguments
    ):
        raise ConfigurationError(f"{path}.args must be a list of strings")
    return HandlerDefinition(
        mapping["name"],
        workdir=_optional_workdir(mapping, path),
        extension_arguments=tuple(arguments),
    )


def handler_values(mapping: dict[str, Any], path: str) -> dict[str, Any]:
    """The ``variables`` and ``saves`` of a handler, as its definition fields."""
    provide, outputs = parse_variables(mapping.get("variables", []), path)
    save_metadata, update_document, update_item = parse_saves(
        mapping.get("saves", []), path
    )
    return {
        "provide": provide,
        "outputs": outputs,
        "save_metadata": save_metadata,
        "update_document": update_document,
        "update_item": update_item,
    }


def parse_variables(
    data: Any, path: str
) -> tuple[tuple[ProvidedVariable, ...], tuple[str, ...]]:
    """Parse ``variables``: what the step hands back, read as ``{{name}}``.

    ``- name: description`` is a value the performer supplies with
    ``--variable``; a bare ``- name`` is one an automatic action returns
    itself.  A name may not start with ``ww`` (or ``__``): those are ww's.
    """
    if data is None:
        return (), ()
    if not isinstance(data, list):
        raise ConfigurationError(f"{path}.variables must be a list")
    provided: list[ProvidedVariable] = []
    returned: list[str] = []
    for index, item in enumerate(data):
        item_path = f"{path}.variables[{index}]"
        if isinstance(item, str):
            name = item
            if not _VARIABLE_NAME.fullmatch(name):
                raise ConfigurationError(
                    f"{item_path} must be a normalized variable name"
                )
        else:
            mapping = _named_entry(_mapping(item, item_path), item_path)
            _only(mapping, {"name", "description"}, item_path)
            name = _name(mapping, item_path)
        if is_reserved_name(name):
            raise ConfigurationError(
                f"{path}.variables name {name!r} is reserved: names starting "
                "with ww are ww's own values"
            )
        if isinstance(item, str):
            returned.append(name)
        else:
            provided.append(
                ProvidedVariable(
                    name, _description(mapping.get("description"), item_path)
                )
            )
    _unique((*(item.name for item in provided), *returned), f"variable in {path}")
    return tuple(provided), tuple(returned)


def parse_saves(
    data: Any, path: str
) -> tuple[
    tuple[SavedMetadata, ...], tuple[DocumentUpdate, ...], tuple[ItemFieldUpdate, ...]
]:
    """Parse ``saves``: what the step writes to metadata, documents, or its item.

    Each entry names a prefixed path, ``metadata.<path>``,
    ``project_metadata.<path>``, ``documents.<name>`` or
    ``item.field.<name>``, with the text saying what to put there; a
    metadata entry may add ``append: true``.
    """
    if data is None:
        return (), (), ()
    if not isinstance(data, list):
        raise ConfigurationError(f"{path}.saves must be a list")
    metadata: list[SavedMetadata] = []
    documents: list[DocumentUpdate] = []
    fields: list[ItemFieldUpdate] = []
    for index, item in enumerate(data):
        item_path = f"{path}.saves[{index}]"
        mapping = _named_entry(_mapping(item, item_path), item_path)
        _only(mapping, {"name", "description", "append"}, item_path)
        target = mapping.get("name")
        if not isinstance(target, str) or not target.startswith(_SAVE_PREFIXES):
            hint = (
                "; saves take no ww. prefix"
                if isinstance(target, str) and target.startswith("ww.")
                else ""
            )
            raise ConfigurationError(
                f"{item_path} must name metadata.<path>, project_metadata.<path>, "
                f"documents.<name>, or item.field.<name>{hint}"
            )
        description = _description(mapping.get("description"), item_path)
        append = _optional_bool(mapping, "append", item_path)
        prefix = next(prefix for prefix in _SAVE_PREFIXES if target.startswith(prefix))
        rest = target.removeprefix(prefix)
        if append is not None and prefix not in {"metadata.", "project_metadata."}:
            raise ConfigurationError(f"{item_path}.append applies to metadata only")
        try:
            if prefix in {"metadata.", "project_metadata."}:
                if not _METADATA_PATH.fullmatch(rest):
                    raise ConfigurationError(
                        f"{item_path} must name a dotted metadata path"
                    )
                project = prefix == "project_metadata."
                metadata.append(
                    SavedMetadata(
                        target if project else rest,
                        rest,
                        description,
                        "project" if project else "task",
                        append=append or False,
                    )
                )
            elif prefix == "documents.":
                documents.append(DocumentUpdate(rest, description))
            else:
                fields.append(ItemFieldUpdate(rest, description))
        except ValueError as error:
            raise ConfigurationError(f"{item_path}: {error}") from error
    _unique((item.name for item in metadata), f"saved metadata path in {path}")
    _unique((item.name for item in documents), f"document in {path}")
    _unique((item.name for item in fields), f"item field in {path}")
    for scope in ("task", "project"):
        keys = [item.key for item in metadata if item.scope == scope]
        for index, key in enumerate(keys):
            if any(
                key.startswith(f"{other}.") or other.startswith(f"{key}.")
                for other in keys[index + 1 :]
            ):
                raise ConfigurationError(
                    f"saved metadata paths in {path} cannot overlap within "
                    f"the {scope} scope"
                )
    return tuple(metadata), tuple(documents), tuple(fields)
