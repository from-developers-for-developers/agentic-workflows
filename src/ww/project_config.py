# SPDX-License-Identifier: GPL-3.0-or-later
"""The project configuration file, ``ww-agentic-workflows.json``.

ww's settings file, separate from ``ww-agentic-workflows.yaml``. The YAML
describes what a workflow *does*, while this describes how the tools around
it behave. It
contains ww-wide settings, built-in execution hints, and extension settings.

```json
{
  "enabled": true,
  "runtime": "single",
  "update_check": true,
  "executable": "ww-agentic-workflows-dev",
  "max_rounds": 3,
  "max_fixes": 3,
  "workflows": {"catchall": {"enabled": false}},
  "projects": [
    {"name": "backend", "path": "./backend", "description": "Python API service."}
  ],
  "extensions": {
    "ww/git": {"base_branches": {"default": "main"}, "separate_branch": true}
  }
}
```

``enabled`` is ``true`` (agents use ww for project work), ``false`` (they
never do), or ``"on_request"`` (ww is available, but agents use it only when
the user explicitly asks for it).

``max_fixes`` is how many times a step's completion may be rejected for a
failed check before ww stops for the operator; a rule may set its own.

``workflows`` switches off the workflows ww provides to every project, such
as ``catchall``; each is on unless its entry says ``"enabled": false``.

``projects`` are the directories, usually repositories, a task may work in.
They are optional and machine-specific, which is why they live here rather than
in ``ww-agentic-workflows.yaml``: the same workflows can run in checkouts laid out
differently on each machine.

An extension's settings are handed to it untouched. ww validates the shape of
the file — that ``extensions`` is a mapping of mappings — and nothing about what
is inside a section, because it cannot know a third party's schema. Each
extension validates its own settings and reports its own errors.

``task_format`` is the generated task ID format: a template over the
``{{timestamp}}``, ``{{digit}}``, and ``{{uuid}}`` placeholders, or ``explicit`` to
require an ID for every task. It is a setting of the checkout and of the
tracker a repository uses, not of what a workflow does, so it lives here.

A configured project may carry its own ``ww-agentic-workflows.json`` and
``ww-agentic-workflows.local.json``. Of those files ww reads only the keys in
``PROJECT_FILE_KEYS``, ``extensions`` applied over the root's and
``task_format`` replacing it, for work done in that project; every other key
describes the project as a ww root of its own, and the workspace root owns
those.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ww.config_files import (
    SETTINGS_FILE,
    ConfigurationLevel,
    display_path,
    project_settings_levels,
    settings_levels,
)
from ww.core_workflows import CORE_WORKFLOW_NAMES
from ww.errors import ConfigurationError
from ww.runtimes import DEFAULT_RUNTIME, RUNTIME_INSTRUCTIONS
from ww.validation import expect_normalized_name, is_positive_int

FILE_NAME = SETTINGS_FILE
BUILTIN_NAMES = frozenset({"init", "workflow_summary"})
# ``init`` only restates requirements; the workflow summary is what people
# read, so it follows the run's ordinary worker selection.
BUILTIN_DEFAULTS: dict[str, dict[str, str]] = {
    "init": {"model": "cheapest", "reasoning": "low"},
    "workflow_summary": {"model": "auto", "reasoning": "auto"},
}
DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_FIXES = 3
# A ``task_format`` that forbids generated IDs: every task is started with an
# explicit ID, or binds one in its workflow's first step.
EXPLICIT_TASK_FORMAT = "explicit"
TASK_FORMAT_PLACEHOLDERS = frozenset({"{{digit}}", "{{timestamp}}", "{{uuid}}"})
_OLD_TASK_FORMAT_PLACEHOLDER = re.compile(r"(?<!\{)\{(digit|timestamp|uuid)\}(?!\})")
# The keys ww takes from a configured project's own settings files. Anything
# else in such a file describes the project as a ww root of its own.
PROJECT_FILE_KEYS = ("extensions", "task_format")
# ``enabled``: ww is used by default (``true``), never (``false``), or only
# when the user explicitly asks for it (``"on_request"``).
ON_REQUEST: Literal["on_request"] = "on_request"
Enabled = bool | Literal["on_request"]
# ``rules.approval``: who approves what a rule verifier proposes. The operator
# approves approaches and checks (``operator``), only checks (``check``), or
# nothing, a proven check being approved automatically (``auto``).
RuleApproval = Literal["operator", "check", "auto"]
RULE_APPROVALS: tuple[RuleApproval, ...] = ("operator", "check", "auto")
DEFAULT_RULE_APPROVAL: RuleApproval = "operator"


@dataclass(frozen=True)
class ProjectDefinition:
    """One directory, usually a repository, a task may work in.

    Without projects every task works in the project root, which is also
    where ww keeps its configuration and state.
    """

    name: str
    path: str
    description: str = ""

    def directory(self, root: Path) -> Path:
        """The project's directory, resolved against ``root`` when relative."""
        return (root / self.path).resolve()

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "path": self.path, "description": self.description}


@dataclass(frozen=True)
class ExtensionSections:
    """The ``extensions`` section of one settings source, keyed by extension.

    ``source`` names the file or files in messages, so a section that applies
    to nothing is reported where it was written: at the root or in a project.
    """

    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: str = SETTINGS_FILE

    def settings_for(self, identifier: str) -> dict[str, Any]:
        """Return one extension's settings, addressed by id or by bare name.

        ``"ww/git"`` is the canonical key, because two vendors may each ship an
        extension called ``git``. A bare ``"git"`` also resolves, as long as it
        is unambiguous for the extension being asked about.
        """
        if identifier in self.sections:
            return deepcopy(self.sections[identifier])
        _, _, name = identifier.partition("/")
        bare = [key for key in self.sections if "/" not in key and key == name]
        if bare:
            return deepcopy(self.sections[bare[0]])
        return {}

    def lists(self, identifier: str) -> bool:
        """Whether a section names the extension, even an empty one."""
        _, _, name = identifier.partition("/")
        return identifier in self.sections or name in self.sections

    def validate_against(self, identifiers: tuple[str, ...]) -> None:
        """Reject sections that name no installed extension, or name two.

        A settings block that silently applies to nothing is worse than an
        error: the file looks configured and the behaviour never changes.
        """
        for key in sorted(self.sections):
            if "/" in key:
                if key not in identifiers:
                    raise ConfigurationError(
                        f"{self.source} configures unknown extension {key!r}; "
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
                    f"{self.source} configures unknown extension {key!r}; "
                    + _available(identifiers)
                )
            if len(matches) > 1:
                raise ConfigurationError(
                    f"{self.source} key {key!r} is ambiguous; use the full "
                    "identifier: " + ", ".join(sorted(matches))
                )


@dataclass(frozen=True)
class ProjectSettings:
    """What a configured project's own settings files contribute.

    Exactly the keys in ``PROJECT_FILE_KEYS``: the project's extension
    sections, and its task ID format when it sets one (``None`` means the
    root's applies).
    """

    project: str
    sections: ExtensionSections
    task_format: str | None = None
    # The files read, repo level then local; empty when the project has none.
    sources: tuple[Path, ...] = ()
    # Each file's own section, in the same order, so a section that applies
    # to nothing is reported in the file that holds it.
    levels: tuple[ExtensionSections, ...] = ()

    def validate_against(self, identifiers: tuple[str, ...]) -> None:
        """Reject sections that name no installed extension, file by file."""
        for level in self.levels:
            level.validate_against(identifiers)


@dataclass(frozen=True)
class ProjectConfig:
    """Settings that apply to a project rather than to one workflow."""

    extensions: dict[str, dict[str, Any]] = field(default_factory=dict)
    builtins: dict[str, dict[str, str]] = field(default_factory=dict)
    max_rounds: int = DEFAULT_MAX_ROUNDS
    # Rejected completions a check allows before the operator decides.
    max_fixes: int = DEFAULT_MAX_FIXES
    # ``false`` tells agents not to use ww in this project; ``start`` refuses.
    # ``"on_request"`` keeps ww available, but agents use it only when the
    # user explicitly asks for it.
    enabled: Enabled = True
    # Who approves rule verifiers' proposals; read when a verification
    # completes, never frozen into a run.
    rule_approval: RuleApproval = DEFAULT_RULE_APPROVAL
    projects: tuple[ProjectDefinition, ...] = ()
    # The runtime ``start`` uses when ``--runtime`` is omitted.
    runtime: str = DEFAULT_RUNTIME
    # ``false`` silences the notice that the ww checkout is behind its remote.
    update_check: bool = True
    # Core workflows switched off for this project.
    disabled_workflows: frozenset[str] = frozenset()
    # The ww binary this project runs: a command on PATH or a path. ``None``
    # means the project launcher, ``./ww``, which falls back to the standard
    # name.
    executable: str | None = None
    # The generated task ID format; ``None`` keeps ww's ``TASK-{{timestamp}}``.
    task_format: str | None = None

    @property
    def projects_by_name(self) -> dict[str, ProjectDefinition]:
        return {project.name: project for project in self.projects}

    @property
    def disabled(self) -> bool:
        """Whether agents must not use ww here at all."""
        return self.enabled is False

    @property
    def on_request(self) -> bool:
        """Whether agents use ww only when the user explicitly asks for it."""
        return self.enabled == ON_REQUEST

    def workflow_enabled(self, name: str) -> bool:
        """Whether a core workflow is offered in this project."""
        return name not in self.disabled_workflows

    def builtin_settings(self, name: str) -> dict[str, str]:
        """Return a built-in's complete model/reasoning request."""
        return {**BUILTIN_DEFAULTS[name], **self.builtins.get(name, {})}

    @property
    def sections(self) -> ExtensionSections:
        return ExtensionSections(self.extensions, FILE_NAME)

    def settings_for(self, identifier: str) -> dict[str, Any]:
        """Return one extension's settings, addressed by id or by bare name."""
        return self.sections.settings_for(identifier)

    def validate_against(self, identifiers: tuple[str, ...]) -> None:
        """Reject sections that name no installed extension, or name two."""
        self.sections.validate_against(identifiers)

    def unknown_project(self, project: str) -> str:
        """The message for a ``--project`` value no entry of ``projects`` has."""
        configured = ", ".join(entry.name for entry in self.projects)
        return f"unknown project {project!r}; " + (
            f"configured projects: {configured}"
            if configured
            else f"no projects are configured in {FILE_NAME}"
        )

    def project_settings(self, root: Path, project: str) -> ProjectSettings:
        """Load what the configured ``project``'s own settings files contribute."""
        definition = self.projects_by_name.get(project)
        if definition is None:
            raise ConfigurationError(self.unknown_project(project))
        return load_project_settings(root, definition)


def load_project_config(path: Path) -> ProjectConfig:
    """Load the settings levels around the repo file ``path``, deep-merged.

    The machine, repo, and local files apply in that order, each optional;
    without any of them the defaults apply.
    """
    raw, sources = compose_settings(path)
    if not sources:
        return ProjectConfig()
    return _parse_settings(raw, " + ".join(str(source) for source in sources))


def compose_settings(path: Path) -> tuple[dict[str, Any], tuple[Path, ...]]:
    """Deep-merge the settings levels around ``path`` and name the files read.

    Nested objects merge key by key; any other value, lists included, replaces
    the one above it.
    """
    return _compose_levels(settings_levels(path))


def load_project_settings(root: Path, project: ProjectDefinition) -> ProjectSettings:
    """Read the keys in ``PROJECT_FILE_KEYS`` from a project's own settings files.

    The project's repo and local files apply in that order, each optional:
    extension sections merge key by key and a later ``task_format`` replaces
    an earlier one. Every other key describes the project as a ww root of its
    own and is left alone. Errors name the project so a mistake is found in
    the right directory.
    """
    sources: list[Path] = []
    levels: list[ExtensionSections] = []
    merged: dict[str, dict[str, Any]] = {}
    task_format: str | None = None
    for level in project_settings_levels(project.directory(root)):
        raw = _read_level(level)
        if raw is None:
            continue
        label = f"{display_path(level.path, root)} (project {project.name!r})"
        sections = ExtensionSections(_parse_extensions(raw, label), label)
        _deep_merge(merged, sections.sections)
        if "task_format" in raw:
            task_format = _parse_task_format(raw["task_format"], label)
        sources.append(level.path)
        levels.append(sections)
    source = " + ".join(display_path(path, root) for path in sources)
    return ProjectSettings(
        project.name,
        ExtensionSections(merged, f"{source} (project {project.name!r})"),
        task_format,
        tuple(sources),
        tuple(levels),
    )


def overlay_settings(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Apply one extension's project settings over its root settings.

    The same rule as between configuration levels: nested objects merge key
    by key, any other value replaces the one above it, so a project states
    only what differs.
    """
    merged = deepcopy(base)
    _deep_merge(merged, overlay)
    return merged


def _compose_levels(
    levels: tuple[ConfigurationLevel, ...],
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    merged: dict[str, Any] = {}
    sources: list[Path] = []
    for level in levels:
        raw = _read_level(level)
        if raw is None:
            continue
        _deep_merge(merged, raw)
        sources.append(level.path)
    return merged, tuple(sources)


def _read_level(level: ConfigurationLevel) -> dict[str, Any] | None:
    """The JSON object one settings file holds, or ``None`` when it is absent."""
    if not level.path.is_file():
        return None
    try:
        raw = json.loads(level.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"invalid {level.path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{level.path} must contain a JSON object")
    return raw


def _deep_merge(target: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(current, value)
        else:
            target[key] = deepcopy(value)


def _parse_extensions(raw: dict[str, Any], path: str) -> dict[str, dict[str, Any]]:
    extensions = raw.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ConfigurationError(f"{path}.extensions must be an object")
    for key, value in extensions.items():
        if not isinstance(value, dict):
            raise ConfigurationError(
                f"{path}.extensions[{key!r}] must be an object of settings"
            )
    return dict(extensions)


def _parse_settings(raw: dict[str, Any], path: str) -> ProjectConfig:
    if "loop_max_times" in raw:
        raise ConfigurationError(
            f'{path}.loop_max_times was renamed to max_rounds: "max_rounds": '
            f"{raw['loop_max_times']}"
        )
    unknown = set(raw) - {
        "enabled",
        "runtime",
        "extensions",
        "builtins",
        "max_rounds",
        "max_fixes",
        "projects",
        "update_check",
        "workflows",
        "executable",
        "task_format",
        "rules",
    }
    if unknown:
        raise ConfigurationError(
            f"{path} has unknown key(s): {', '.join(sorted(unknown))}"
        )
    extensions = _parse_extensions(raw, path)
    enabled = _parse_enabled(raw.get("enabled", True), path)
    update_check = raw.get("update_check", True)
    if not isinstance(update_check, bool):
        raise ConfigurationError(f"{path}.update_check must be true or false")
    runtime = raw.get("runtime", DEFAULT_RUNTIME)
    if runtime not in RUNTIME_INSTRUCTIONS:
        raise ConfigurationError(
            f"{path}.runtime must be one of: " + ", ".join(RUNTIME_INSTRUCTIONS)
        )
    max_rounds = raw.get("max_rounds", DEFAULT_MAX_ROUNDS)
    if not is_positive_int(max_rounds):
        raise ConfigurationError(f"{path}.max_rounds must be a positive integer")
    max_fixes = raw.get("max_fixes", DEFAULT_MAX_FIXES)
    if not is_positive_int(max_fixes):
        raise ConfigurationError(f"{path}.max_fixes must be a positive integer")
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
        extensions=extensions,
        builtins=normalized,
        max_rounds=max_rounds,
        max_fixes=max_fixes,
        enabled=enabled,
        projects=_parse_projects(raw.get("projects"), path),
        runtime=runtime,
        update_check=update_check,
        disabled_workflows=_parse_workflows(raw.get("workflows"), path),
        executable=_parse_executable(raw.get("executable"), path),
        task_format=_parse_task_format(raw.get("task_format"), path),
        rule_approval=_parse_rules(raw.get("rules"), path),
    )


def _parse_rules(data: Any, path: str) -> RuleApproval:
    """``rules``: an object whose one key, ``approval``, names the approver."""
    if data is None:
        return DEFAULT_RULE_APPROVAL
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}.rules must be an object")
    unknown = set(data) - {"approval"}
    if unknown:
        raise ConfigurationError(
            f"{path}.rules has unknown key(s): {', '.join(sorted(unknown))}"
        )
    approval = data.get("approval", DEFAULT_RULE_APPROVAL)
    for value in RULE_APPROVALS:
        if approval == value:
            return value
    raise ConfigurationError(
        f"{path}.rules.approval must be one of: "
        + ", ".join(f'"{value}"' for value in RULE_APPROVALS)
    )


def _parse_enabled(data: Any, path: str) -> Enabled:
    if isinstance(data, bool):
        return data
    if data == ON_REQUEST:
        return ON_REQUEST
    raise ConfigurationError(
        f'{path}.enabled must be true, false, or "{ON_REQUEST}"'
    )


def _parse_task_format(data: Any, path: str) -> str | None:
    if data is None:
        return None
    if not isinstance(data, str) or not data:
        raise ConfigurationError(f"{path}.task_format must be a non-empty string")
    if data == EXPLICIT_TASK_FORMAT:
        return data
    old = _OLD_TASK_FORMAT_PLACEHOLDER.search(data)
    if old is not None:
        renamed = _OLD_TASK_FORMAT_PLACEHOLDER.sub(r"{{\1}}", data)
        raise ConfigurationError(
            f"{path}.task_format placeholder {old.group(0)} was renamed to "
            f"{{{{{old.group(1)}}}}}: task_format: {renamed}"
        )
    tokens = re.findall(r"\{\{[^{}]*\}\}", data)
    rest = re.sub(r"\{\{[^{}]*\}\}", "", data)
    if "{" in rest or "}" in rest:
        raise ConfigurationError(f"{path}.task_format has invalid placeholders")
    unknown = set(tokens) - TASK_FORMAT_PLACEHOLDERS
    if unknown:
        raise ConfigurationError(
            f"{path}.task_format has unknown placeholder(s): "
            + ", ".join(sorted(unknown))
        )
    return data


def _parse_executable(data: Any, path: str) -> str | None:
    if data is None:
        return None
    if not isinstance(data, str) or not data.strip():
        raise ConfigurationError(
            f"{path}.executable must be a command name or a path to the ww binary"
        )
    return data.strip()


def _parse_workflows(data: Any, path: str) -> frozenset[str]:
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


def _parse_projects(data: Any, path: str) -> tuple[ProjectDefinition, ...]:
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
