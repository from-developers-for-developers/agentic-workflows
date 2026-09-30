# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict loading for the normalized ``ww-agentic-workflows.yaml`` schema."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import yaml

from ww.config_files import task_format_moved
from ww.errors import ConfigurationError
from ww.extensions import ExtensionRegistry
from ww.runtimes import RUNTIME_INSTRUCTIONS
from ww.workflow_config import (
    DocumentDefinition,
    HandlerDefinition,
    HookDefinition,
    MetadataScope,
    ModeDefinition,
    ProfileDefinition,
    RuleGroup,
    StepDefinition,
    WorkflowConfiguration,
    WorkflowDefinition,
)
from ww.workflow_validation import validate_configuration

from .actions import _parse_hooks
from .composition import compose_configuration
from .rules import RuleGroupContribution, parse_rules_root, resolve_step_rules
from .steps import _parse_handlers, _parse_step
from .values import (
    _NAME,
    _description,
    _description_items,
    _mapping,
    _name,
    _named_entry,
    _only,
    _optional_agent,
    _optional_string,
    _profile,
    _required_list,
    _role,
    _string_list,
    _subagents,
    _unique,
)


@dataclass(frozen=True)
class YamlConfigurationLoader:
    """The built-in ``ww-agentic-workflows.yaml`` notation frontend.

    ``extensions`` supply the rule groups the project's configured extensions
    ship, merged under the root ``rules`` before any step names them.
    """

    path: Path
    extensions: ExtensionRegistry | None = None

    def __call__(self) -> WorkflowConfiguration:
        return parse_yaml_configuration(self.path, self.extensions)


def parse_yaml_configuration(
    path: Path, extensions: ExtensionRegistry | None = None
) -> WorkflowConfiguration:
    """Load a normalized configuration without deriving execution behavior.

    Parsing checks the YAML notation's shape. Cross-definition semantics are
    deliberately checked by :func:`validate_configuration` after any frontend
    has produced this same normalized model. The files ``path`` imports are
    composed into it first, so the parser reads one document; rule paths
    resolve against ``path``'s directory, where composition rebases them.
    """
    if not path.is_file():
        raise ConfigurationError(f"workflow configuration not found: {path}")
    return parse_yaml_text(
        compose_configuration(path).text,
        str(path),
        base=path.parent,
        extension_rule_groups=(
            extensions.rule_groups() if extensions is not None else ()
        ),
    )


def parse_yaml_text(
    text: str,
    source: str = "<string>",
    *,
    base: Path | None = None,
    extension_rule_groups: tuple[tuple[str, RuleGroupContribution], ...] = (),
) -> WorkflowConfiguration:
    """Parse ``ww-agentic-workflows.yaml`` notation held in memory, named ``source``.

    ``base`` is the directory rule paths resolve against; it defaults to the
    current directory.
    """
    raw = _raw_from_text(text, source)
    if "tasks" in raw:
        raise ConfigurationError("configuration uses legacy 'tasks'; use 'handlers'")
    base = base if base is not None else Path.cwd()
    modes = _parse_modes(raw.get("modes", []))
    profiles = _parse_profiles(raw.get("profiles", {}))
    rule_groups = parse_rules_root(raw.get("rules"), base, extension_rule_groups)
    handlers = _parse_handlers(raw.get("handlers", []))
    workflows_raw = _required_list(raw, "workflows", "configuration")
    global_hooks = _parse_hooks(raw.get("hooks", {}), "global", "hooks")
    handlers_by_name = {handler.name: handler for handler in handlers}
    parsed = tuple(
        _parse_workflow(item, f"workflows[{index}]", handlers_by_name)
        for index, item in enumerate(workflows_raw)
    )
    workflows, handlers = resolve_step_rules(
        _resolve_inheritance(parsed), handlers, rule_groups, base
    )
    return WorkflowConfiguration(
        modes,
        profiles,
        handlers,
        _extend_to_heirs(global_hooks, workflows),
        workflows,
        documents=_parse_documents(raw.get("documents", [])),
        rule_groups=_extend_groups_to_heirs(rule_groups, workflows),
    )


def load_configuration(
    path: Path, extensions: ExtensionRegistry | None = None
) -> WorkflowConfiguration:
    """Load and semantically validate the built-in YAML notation."""
    return validate_configuration(
        YamlConfigurationLoader(path, extensions)(), extensions
    )


def load_modes(
    path: Path, extensions: ExtensionRegistry | None = None
) -> dict[str, ModeDefinition]:
    return {mode.name: mode for mode in load_configuration(path, extensions).modes}


def _raw_from_text(text: str, source: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigurationError(f"invalid YAML in {source}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError("workflow configuration must be a mapping")
    allowed = {
        "modes",
        "profiles",
        "documents",
        "handlers",
        "hooks",
        "workflows",
        "tasks",
        "rules",
    }
    if "task_format" in raw:
        raise ConfigurationError(task_format_moved(source))
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigurationError(
            f"configuration has unknown key(s): {', '.join(sorted(unknown))}"
        )
    return raw


def _parse_modes(data: Any) -> tuple[ModeDefinition, ...]:
    if not isinstance(data, list):
        raise ConfigurationError("modes must be a list")
    result = []
    for index, item in enumerate(data):
        mapping = _named_entry(_mapping(item, f"modes[{index}]"), f"modes[{index}]")
        _only(mapping, {"name", "description"}, f"modes[{index}]")
        name = _name(mapping, f"modes[{index}]")
        result.append(
            ModeDefinition(
                name,
                _description_items(mapping.get("description"), f"mode {name!r}"),
            )
        )
    return tuple(result)


def _parse_documents(data: Any) -> tuple[DocumentDefinition, ...]:
    """Parse root ``documents``: named entries with a description and scope."""
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ConfigurationError("documents must be a list")
    result = []
    for index, item in enumerate(data):
        path = f"documents[{index}]"
        mapping = _named_entry(_mapping(item, path), path)
        _only(mapping, {"name", "description", "scope", "path"}, path)
        scope = mapping.get("scope", "task")
        if scope not in {"task", "project"}:
            raise ConfigurationError(f"{path}.scope must be 'task' or 'project'")
        try:
            result.append(
                DocumentDefinition(
                    _name(mapping, path),
                    _description(mapping.get("description"), path),
                    cast(MetadataScope, scope),
                    path=_optional_string(mapping, "path", path),
                )
            )
        except ValueError as error:
            raise ConfigurationError(f"{path}: {error}") from error
    _unique((item.name for item in result), "document")
    return tuple(result)


def _parse_profiles(data: Any) -> tuple[ProfileDefinition, ...]:
    if data is None:
        return ()
    if not isinstance(data, dict):
        raise ConfigurationError("profiles must be a mapping")
    result = []
    for name, description in data.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ConfigurationError("profiles keys must be normalized names")
        if description is not None and (
            not isinstance(description, str) or not description.strip()
        ):
            raise ConfigurationError(f"profiles.{name} must be a string or null")
        result.append(ProfileDefinition(name, description))
    return tuple(result)


# The workflow keys an inheriting workflow may set, and the definition fields
# each one overrides; everything else comes from the workflow it inherits.
_OVERRIDES = {
    "description": ("description",),
    "modes": ("modes",),
    "agent": ("agent",),
    "model": ("model",),
    "reasoning": ("reasoning",),
    "profile": ("profile", "profile_description"),
    "role": ("role",),
    "subagents": ("subagents",),
    "handoff": ("handoff",),
    "runtime": ("runtime",),
    "restartable": ("restartable",),
    "recommended_next_workflow": ("recommended_next_workflow",),
}


@dataclass(frozen=True)
class _ParsedWorkflow:
    """One workflow entry, and which of its fields the entry set itself."""

    definition: WorkflowDefinition
    overrides: frozenset[str]


def _parse_workflow(
    data: Any, path: str, handlers_by_name: dict[str, HandlerDefinition]
) -> _ParsedWorkflow:
    workflow_keys = {
        "name",
        "description",
        "steps",
        "hooks",
        "modes",
        "agent",
        "model",
        "reasoning",
        "handoff",
        "profile",
        "role",
        "subagents",
        "runtime",
        "restartable",
        "inherit",
        "recommended_next_workflow",
    }
    mapping = _named_entry(
        _mapping(data, path),
        path,
        allowed=workflow_keys,
        ignored={"steps"},
    )
    if "workflows" in mapping:
        raise ConfigurationError(
            f"{path}.workflows defines nested workflows, which are not supported"
        )
    _only(mapping, workflow_keys, path)
    name = _name(mapping, path)
    handoff = mapping.get("handoff", False)
    if not isinstance(handoff, bool):
        raise ConfigurationError(f"{path}.handoff must be true or omitted")
    runtime = mapping.get("runtime")
    if runtime is not None and runtime not in RUNTIME_INSTRUCTIONS:
        raise ConfigurationError(
            f"{path}.runtime must be one of: " + ", ".join(RUNTIME_INSTRUCTIONS)
        )
    restartable = mapping.get("restartable", False)
    if not isinstance(restartable, bool):
        raise ConfigurationError(f"{path}.restartable must be true or false")
    inherits = mapping.get("inherit")
    if inherits is not None and (not isinstance(inherits, str) or not inherits):
        raise ConfigurationError(f"{path}.inherit must name a workflow")
    if inherits is not None:
        # A workflow that needs other steps or hooks is a workflow of its
        # own, not a copy of another one.
        declared = sorted({"steps", "hooks"}.intersection(mapping))
        if declared:
            raise ConfigurationError(
                f"workflow {name!r} inherits {inherits!r} and cannot declare "
                + " or ".join(declared)
            )
        steps: tuple[StepDefinition, ...] = ()
    else:
        steps_data = _required_list(mapping, "steps", f"workflow {name!r}")
        steps = tuple(
            _parse_step(item, f"{path}.steps[{index}]", handlers_by_name)
            for index, item in enumerate(steps_data)
        )
    recommended = mapping.get("recommended_next_workflow")
    if recommended is not None and (
        not isinstance(recommended, str) or not recommended.strip()
    ):
        raise ConfigurationError(
            f"{path}.recommended_next_workflow must name a workflow"
        )
    definition = WorkflowDefinition(
        name=name,
        description=_description(mapping.get("description"), f"workflow {name!r}"),
        steps=steps,
        hooks=_parse_hooks(
            mapping.get("hooks", {}), "workflow", f"workflow {name!r} hooks"
        ),
        modes=_string_list(mapping.get("modes", []), f"workflow {name!r} modes"),
        agent=_optional_agent(mapping, "agent", f"workflow {name!r}"),
        model=_optional_string(mapping, "model", f"workflow {name!r}"),
        reasoning=_optional_string(mapping, "reasoning", f"workflow {name!r}"),
        **_profile(mapping, f"workflow {name!r}"),
        role=_role(mapping, f"workflow {name!r}"),
        subagents=_subagents(mapping, f"workflow {name!r}"),
        handoff=handoff,
        runtime=runtime,
        restartable=restartable,
        inherits=inherits,
        recommended_next_workflow=recommended,
    )
    return _ParsedWorkflow(
        definition,
        frozenset(
            field
            for key, fields in _OVERRIDES.items()
            if key in mapping
            for field in fields
        ),
    )


def _resolve_inheritance(
    parsed: tuple[_ParsedWorkflow, ...],
) -> tuple[WorkflowDefinition, ...]:
    """Complete every inheriting workflow from the one it names, in order.

    The copy takes everything, steps and workflow hooks included, and the
    entry's own settings replace the copied ones. Chains resolve from the
    root; a cycle or an unknown name is an error.
    """
    entries = {entry.definition.name: entry for entry in parsed}
    if len(entries) != len(parsed):
        # Duplicate names are reported by validation, with its usual message.
        return tuple(entry.definition for entry in parsed)
    resolved: dict[str, WorkflowDefinition] = {}

    def resolve(name: str, chain: tuple[str, ...]) -> WorkflowDefinition:
        if name in resolved:
            return resolved[name]
        entry = entries[name]
        parent = entry.definition.inherits
        if parent is None:
            resolved[name] = entry.definition
            return entry.definition
        if parent not in entries:
            raise ConfigurationError(
                f"workflow {name!r} inherits unknown workflow {parent!r}"
            )
        if parent in chain:
            cycle = " -> ".join((*chain, name, parent))
            raise ConfigurationError(f"workflow inheritance cycle: {cycle}")
        base = resolve(parent, (*chain, name))
        resolved[name] = replace(
            base,
            name=name,
            inherits=parent,
            **{
                field: getattr(entry.definition, field)
                for field in sorted(entry.overrides)
            },
        )
        return resolved[name]

    return tuple(resolve(entry.definition.name, ()) for entry in parsed)


def _extend_to_heirs(
    hooks: tuple[HookDefinition, ...], workflows: tuple[WorkflowDefinition, ...]
) -> tuple[HookDefinition, ...]:
    """Let a global hook filtered to a workflow also run for its heirs.

    Inheriting a workflow means behaving like it, so a hook written for
    ``hotfix`` also runs for a ``bugfix`` that inherits it.
    """
    return tuple(
        replace(hook, workflow_names=_with_heirs(hook.workflow_names, workflows))
        if hook.workflow_names
        else hook
        for hook in hooks
    )


def _extend_groups_to_heirs(
    groups: tuple[RuleGroup, ...], workflows: tuple[WorkflowDefinition, ...]
) -> tuple[RuleGroup, ...]:
    """Let a rule group filtered to a workflow also apply to its heirs."""
    return tuple(
        replace(group, workflows=_with_heirs(group.workflows, workflows))
        if group.workflows
        else group
        for group in groups
    )


def _with_heirs(
    names: tuple[str, ...], workflows: tuple[WorkflowDefinition, ...]
) -> tuple[str, ...]:
    """``names`` followed by every workflow that inherits one of them."""
    parents = {workflow.name: workflow.inherits for workflow in workflows}

    def lineage(name: str) -> set[str]:
        found: set[str] = set()
        current: str | None = name
        while current is not None and current not in found:
            found.add(current)
            current = parents.get(current)
        return found

    return (
        *names,
        *(
            workflow.name
            for workflow in workflows
            if workflow.name not in names and lineage(workflow.name).intersection(names)
        ),
    )
