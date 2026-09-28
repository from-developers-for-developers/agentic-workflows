# SPDX-License-Identifier: GPL-3.0-or-later
"""The project configuration file, ``agentic-workflows.json``.

This is ww's settings file, separate from ``workflows.yaml``: the YAML describes
what a workflow *does*, while this describes how the tools around it behave. It
contains ww-wide settings, built-in execution hints, and extension settings.

```json
{
  "enabled": true,
  "runtime": "single",
  "update_check": true,
  "loop_max_times": 3,
  "workflows": {"catchall": {"enabled": false}},
  "projects": [
    {"name": "backend", "path": "./backend", "description": "Python API service."}
  ],
  "extensions": {
    "ww/git": {"base_branches": {"default": "main"}, "use_separate_branch": true}
  }
}
```

``workflows`` switches off the workflows ww provides to every project, such
as ``catchall``; each is on unless its entry says ``"enabled": false``.

``projects`` are the directories, usually repositories, a task may work in.
They are optional and machine-specific, which is why they live here rather than
in ``workflows.yaml``: the same workflows can run in checkouts laid out
differently on each machine.

An extension's settings are handed to it untouched. ww validates the shape of
the file — that ``extensions`` is a mapping of mappings — and nothing about what
is inside a section, because it cannot know a third party's schema. Each
extension validates its own settings and reports its own errors.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ww.core_workflows import CORE_WORKFLOW_NAMES
from ww.errors import ConfigurationError
from ww.runtimes import DEFAULT_RUNTIME, RUNTIME_INSTRUCTIONS
from ww.validation import expect_normalized_name, is_positive_int

FILE_NAME = "agentic-workflows.json"
BUILTIN_NAMES = frozenset({"init", "workflow_summary"})
# ``init`` only restates requirements; the workflow summary is what people
# read, so it follows the run's ordinary worker selection.
BUILTIN_DEFAULTS: dict[str, dict[str, str]] = {
    "init": {"model": "cheapest", "reasoning": "low"},
    "workflow_summary": {"model": "auto", "reasoning": "auto"},
}
DEFAULT_LOOP_MAX_TIMES = 3


@dataclass(frozen=True)
class ProjectDefinition:
    """One directory, usually a repository, a task may work in.

    Without projects every task works in the project root, which is also
    where ww keeps its configuration and state.
    """

    name: str
    path: str
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "path": self.path, "description": self.description}


@dataclass(frozen=True)
class ProjectConfig:
    """Settings that apply to a project rather than to one workflow."""

    extensions: dict[str, dict[str, Any]] = field(default_factory=dict)
    builtins: dict[str, dict[str, str]] = field(default_factory=dict)
    loop_max_times: int = DEFAULT_LOOP_MAX_TIMES
    # ``false`` tells agents not to use ww in this project; ``start`` refuses.
    enabled: bool = True
    projects: tuple[ProjectDefinition, ...] = ()
    # The runtime ``start`` uses when ``--runtime`` is omitted.
    runtime: str = DEFAULT_RUNTIME
    # ``false`` silences the notice that the ww checkout is behind its remote.
    update_check: bool = True
    # Core workflows switched off for this project.
    disabled_workflows: frozenset[str] = frozenset()

    @property
    def projects_by_name(self) -> dict[str, ProjectDefinition]:
        return {project.name: project for project in self.projects}

    def workflow_enabled(self, name: str) -> bool:
        """Whether a core workflow is offered in this project."""
        return name not in self.disabled_workflows

    def builtin_settings(self, name: str) -> dict[str, str]:
        """Return a built-in's complete model/reasoning request."""
        return {**BUILTIN_DEFAULTS[name], **self.builtins.get(name, {})}

    def settings_for(self, identifier: str) -> dict[str, Any]:
        """Return one extension's settings, addressed by id or by bare name.

        ``"ww/git"`` is the canonical key, because two vendors may each ship an
        extension called ``git``. A bare ``"git"`` also resolves, as long as it
        is unambiguous for the extension being asked about.
        """
        if identifier in self.extensions:
            return deepcopy(self.extensions[identifier])
        _, _, name = identifier.partition("/")
        bare = [key for key in self.extensions if "/" not in key and key == name]
        if bare:
            return deepcopy(self.extensions[bare[0]])
        return {}

    def validate_against(self, identifiers: tuple[str, ...]) -> None:
        """Reject sections that name no installed extension, or name two.

        A settings block that silently applies to nothing is worse than an
        error: the file looks configured and the behaviour never changes.
        """
        for key in sorted(self.extensions):
            if "/" in key:
                if key not in identifiers:
                    raise ConfigurationError(
                        f"{FILE_NAME} configures unknown extension {key!r}; "
                        + _available(identifiers)
                    )
                continue
            matches = [
                identifier
                for identifier in identifiers
                if identifier.partition("/")[2] == key
            ]
            if not matches:
                raise ConfigurationError(
                    f"{FILE_NAME} configures unknown extension {key!r}; "
                    + _available(identifiers)
                )
            if len(matches) > 1:
                raise ConfigurationError(
                    f"{FILE_NAME} key {key!r} is ambiguous; use the full "
                    "identifier: " + ", ".join(sorted(matches))
                )


def load_project_config(path: Path) -> ProjectConfig:
    """Load ``agentic-workflows.json``, or return defaults when it is absent."""
    if not path.is_file():
        return ProjectConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"invalid {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path} must contain a JSON object")
    unknown = set(raw) - {
        "enabled",
        "runtime",
        "extensions",
        "builtins",
        "loop_max_times",
        "projects",
        "update_check",
        "workflows",
    }
    if unknown:
        raise ConfigurationError(
            f"{path} has unknown key(s): {', '.join(sorted(unknown))}"
        )
    extensions = raw.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ConfigurationError(f"{path}.extensions must be an object")
    for key, value in extensions.items():
        if not isinstance(value, dict):
            raise ConfigurationError(
                f"{path}.extensions[{key!r}] must be an object of settings"
            )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigurationError(f"{path}.enabled must be true or false")
    update_check = raw.get("update_check", True)
    if not isinstance(update_check, bool):
        raise ConfigurationError(f"{path}.update_check must be true or false")
    runtime = raw.get("runtime", DEFAULT_RUNTIME)
    if runtime not in RUNTIME_INSTRUCTIONS:
        raise ConfigurationError(
            f"{path}.runtime must be one of: " + ", ".join(RUNTIME_INSTRUCTIONS)
        )
    loop_max_times = raw.get("loop_max_times", DEFAULT_LOOP_MAX_TIMES)
    if not is_positive_int(loop_max_times):
        raise ConfigurationError(f"{path}.loop_max_times must be a positive integer")
    builtins = raw.get("builtins", {})
    if not isinstance(builtins, dict):
        raise ConfigurationError(f"{path}.builtins must be an object")
    unknown_builtins = set(builtins) - BUILTIN_NAMES
    if unknown_builtins:
        raise ConfigurationError(
            f"{path}.builtins has unknown name(s): "
            + ", ".join(sorted(unknown_builtins))
        )
    normalized: dict[str, dict[str, str]] = {}
    for name, value in builtins.items():
        if not isinstance(value, dict):
            raise ConfigurationError(f"{path}.builtins.{name} must be an object")
        unknown_fields = set(value) - {"model", "reasoning"}
        if unknown_fields:
            raise ConfigurationError(
                f"{path}.builtins.{name} has unknown key(s): "
                + ", ".join(sorted(unknown_fields))
            )
        for hint_name, hint in value.items():
            if not isinstance(hint, str) or not hint.strip():
                raise ConfigurationError(
                    f"{path}.builtins.{name}.{hint_name} must be a non-empty string"
                )
        normalized[name] = dict(value)
    return ProjectConfig(
        dict(extensions),
        normalized,
        loop_max_times,
        enabled,
        _parse_projects(raw.get("projects"), path),
        runtime,
        update_check,
        _parse_workflows(raw.get("workflows"), path),
    )


def _parse_workflows(data: Any, path: Path) -> frozenset[str]:
    """The core workflows switched off, from ``{"<name>": {"enabled": false}}``."""
    if data is None:
        return frozenset()
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}.workflows must be an object")
    unknown = set(data) - CORE_WORKFLOW_NAMES
    if unknown:
        raise ConfigurationError(
            f"{path}.workflows has unknown name(s): {', '.join(sorted(unknown))}; "
            "core workflows: " + ", ".join(sorted(CORE_WORKFLOW_NAMES))
        )
    disabled: set[str] = set()
    for name, value in data.items():
        context = f"{path}.workflows.{name}"
        if not isinstance(value, dict):
            raise ConfigurationError(f"{context} must be an object")
        unknown_keys = set(value) - {"enabled"}
        if unknown_keys:
            raise ConfigurationError(
                f"{context} has unknown key(s): {', '.join(sorted(unknown_keys))}"
            )
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigurationError(f"{context}.enabled must be true or false")
        if not enabled:
            disabled.add(name)
    return frozenset(disabled)


def _parse_projects(data: Any, path: Path) -> tuple[ProjectDefinition, ...]:
    if data is None:
        return ()
    if not isinstance(data, list):
        raise ConfigurationError(f"{path}.projects must be a list")
    result: list[ProjectDefinition] = []
    for index, item in enumerate(data):
        context = f"{path}.projects[{index}]"
        if not isinstance(item, dict):
            raise ConfigurationError(f"{context} must be an object")
        unknown_keys = set(item) - {"name", "path", "description"}
        if unknown_keys:
            raise ConfigurationError(
                f"{context} has unknown key(s): {', '.join(sorted(unknown_keys))}"
            )
        name = expect_normalized_name(
            item.get("name"), f"{context}.name", error=ConfigurationError
        )
        if any(project.name == name for project in result):
            raise ConfigurationError(f"{path}.projects has duplicate name {name!r}")
        location = item.get("path")
        if not isinstance(location, str) or not location.strip():
            raise ConfigurationError(f"{context}.path must be a non-empty string")
        description = item.get("description", "")
        if not isinstance(description, str):
            raise ConfigurationError(f"{context}.description must be a string")
        result.append(ProjectDefinition(name, location.strip(), description))
    return tuple(result)


def _available(identifiers: tuple[str, ...]) -> str:
    names = ", ".join(sorted(identifiers))
    return f"installed: {names}" if names else "no extensions are installed"
