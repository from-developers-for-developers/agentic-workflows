# SPDX-License-Identifier: GPL-3.0-or-later
"""The project configuration file, ``ww.json``.

ww's settings file, separate from ``ww.yaml``. The YAML
describes what a workflow *does*, while this describes how the tools around
it behave. It
contains ww-wide settings, built-in execution hints, and extension settings.

```json
{
  "enabled": true,
  "runtime": "single",
  "update_check": true,
  "executable": "ww-agentic-workflows-dev",
  "limits": {"rounds": 3, "fixes": 3},
  "pages": {"worker_requirements": "pointer"},
  "workflows": {"ww-suggest": {"enabled": false}},
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

``limits`` holds positive integers ``rounds`` and ``fixes`` and the non-negative
``auto_retries``. ``rounds`` is the round limit of a
step ``loop`` that sets no ``max_rounds`` of its own. ``fixes`` is how many
times a step's completion may be rejected for a failed check before ww stops
for the operator, unless a rule sets its own ``max_fixes``. ``auto_retries``
(default 0) is how many times ww retries a failed automatic step itself,
recording each failure on the step, before the step's own failure handling (a
repair assignment, or the operator) applies.

``agent_hooks`` holds the keys ``check_unfinished`` (default ``true``) and
``recent_days`` (default 3). Both are validated and accepted so existing files
keep loading, and neither has any effect: ww no longer lists unfinished tasks
or interruptions.

``pages`` tunes what pages print. ``worker_requirements`` is ``full`` (the
default: the first page of every delegated worker assignment prints the task
requirements in full) or ``pointer`` (it carries the pointer to ``ww
requirements`` instead); the manager's pages are unaffected. ``init`` writes the
key only once it is set.

``workflows`` switches off the workflows ww provides to every project, such
as ``ww-suggest``; each is on unless its entry says ``"enabled": false``. An
entry for ``catchall``, a workflow ww no longer provides, is ignored and
``lint`` warns about it.

``projects`` are the directories, usually repositories, a task may work in.
They are optional and machine-specific, which is why they live here rather than
in ``ww.yaml``: the same workflows can run in checkouts laid out
differently on each machine.

An extension's settings are handed to it untouched. ww validates the shape of
the file — that ``extensions`` is a mapping of mappings — and nothing about what
is inside a section, because it cannot know a third party's schema. Each
extension validates its own settings and reports its own errors.

``task_format`` is the generated task ID format: a template over the
``{{timestamp}}``, ``{{digit}}``, and ``{{uuid}}`` placeholders, or ``explicit`` to
require an ID for every task. It is a setting of the checkout and of the
tracker a repository uses, not of what a workflow does, so it lives here.

A configured project may carry its own ``ww.json`` and
``ww.local.json``. Of those files ww reads only the keys in
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

from ww.builtin_workflows import builtin_workflow_names
from ww.config_files import (
    SETTINGS_FILE,
    ConfigurationLevel,
    configuration_file_exists,
    display_path,
    project_settings_levels,
    read_configuration_file,
    settings_levels,
)
from ww.errors import ConfigurationError
from ww.runtimes import DEFAULT_RUNTIME, RUNTIME_INSTRUCTIONS
from ww.validation import expect_normalized_name, is_positive_int, is_strict_int

FILE_NAME = SETTINGS_FILE
BUILTIN_NAMES = frozenset({"init", "workflow_summary"})
# Workflows ww once provided; a ``workflows`` entry for one is ignored, with a
# warning from ``lint``, rather than refused.
RETIRED_WORKFLOWS = frozenset({"catchall"})
# ``init`` only restates requirements; the workflow summary is what people
# read, so it follows the run's ordinary worker selection.
BUILTIN_DEFAULTS: dict[str, dict[str, str]] = {
    "init": {"model": "cheapest", "reasoning": "low"},
    "workflow_summary": {"model": "auto", "reasoning": "auto"},
}
DEFAULT_ROUNDS = 3
DEFAULT_FIXES = 3
DEFAULT_RECENT_DAYS = 3
# A ``task_format`` that forbids generated IDs: every task is started with an
# explicit ID, or binds one in its workflow's first step.
EXPLICIT_TASK_FORMAT = "explicit"
TASK_FORMAT_PLACEHOLDERS = frozenset({"{{digit}}", "{{timestamp}}", "{{uuid}}"})
# A placeholder in double braces, e.g. "{{digit}}" in "TASK-{{digit}}".
_TASK_FORMAT_TOKEN = re.compile(r"\{\{[^{}]*\}\}")
# The keys ww takes from a configured project's own settings files. Anything
# else in such a file describes the project as a ww root of its own.
PROJECT_FILE_KEYS = ("extensions", "task_format")
# ``enabled``: ww is used by default (``true``), never (``false``), or only
# when the user explicitly asks for it (``"on_request"``).
ON_REQUEST: Literal["on_request"] = "on_request"
Enabled = bool | Literal["on_request"]


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
class Limits:
    """The ``limits`` setting: how far ww goes before the operator decides."""

    # A step loop's rounds when it sets no ``max_rounds`` of its own.
    rounds: int = DEFAULT_ROUNDS
    # Rejected completions a check allows when its rule sets no ``max_fixes``.
    fixes: int = DEFAULT_FIXES
    # How often ww itself retries a failed automatic step before the step's
    # failure handling (a repair assignment, or the operator) applies.
    auto_retries: int = 0

    def to_dict(self) -> dict[str, int]:
        # The retries appear once set, so a default file stays as ``init`` wrote it.
        return {
            "rounds": self.rounds,
            "fixes": self.fixes,
            **({"auto_retries": self.auto_retries} if self.auto_retries else {}),
        }


@dataclass(frozen=True)
class AgentHooks:
    """The ``agent_hooks`` setting; accepted for compatibility and ignored."""

    # Both keys are kept so older files load; nothing reads them.
    check_unfinished: bool = True
    recent_days: int = DEFAULT_RECENT_DAYS

    def to_dict(self) -> dict[str, object]:
        return {
            "check_unfinished": self.check_unfinished,
            "recent_days": self.recent_days,
        }


WORKER_REQUIREMENTS = ("full", "pointer")


@dataclass(frozen=True)
class Pages:
    """The ``pages`` setting: what the pages ww prints carry."""

    # ``full`` prints the task requirements on a delegated worker assignment's
    # first page; ``pointer`` names the command that prints them instead.
    worker_requirements: Literal["full", "pointer"] = "full"

    def to_dict(self) -> dict[str, str]:
        # Written once set, so a default file stays as ``init`` wrote it.
        return (
            {"worker_requirements": self.worker_requirements}
            if self.worker_requirements != "full"
            else {}
        )


@dataclass(frozen=True)
class DebugSettings:
    """``debug`` in ``ww.json``: ww's self-assessment of each run, kept locally.

    ``collect`` asks the agent, at the end of every run, how ww itself behaved
    and keeps the answer under ``.ww/debug/``; ``report`` makes ``discover``
    offer to publish collected records to ww's GitHub issues. Both are off by
    default: nothing is collected or sent unless the operator switches it on.
    """

    collect: bool = False
    report: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {"collect": self.collect, "report": self.report}


@dataclass(frozen=True)
class FeedbackSettings:
    """``feedback`` in ``ww.json``: the agent's assessment of the workflow.

    ``collect`` asks the agent, at the end of every run, how well the workflow
    that ran was composed and keeps the answer under ``.ww/feedback/``, for
    the operator's own review; it is never sent anywhere. Off by default.
    """

    collect: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {"collect": self.collect}


@dataclass(frozen=True)
class ProjectConfig:
    """Settings that apply to a project rather than to one workflow."""

    extensions: dict[str, dict[str, Any]] = field(default_factory=dict)
    builtins: dict[str, dict[str, str]] = field(default_factory=dict)
    limits: Limits = Limits()
    agent_hooks: AgentHooks = AgentHooks()
    pages: Pages = Pages()
    # ``false`` tells agents not to use ww in this project; ``start`` refuses.
    # ``"on_request"`` keeps ww available, but agents use it only when the
    # user explicitly asks for it.
    enabled: Enabled = True
    # ``rules.check_guidance``: the operator's own words for the commands
    # ``ww-scriptize-rules`` builds, which it reads from ``ww rules --json``.
    rule_check_guidance: str | None = None
    projects: tuple[ProjectDefinition, ...] = ()
    # The runtime ``start`` uses when ``--runtime`` is omitted.
    runtime: str = DEFAULT_RUNTIME
    # ``false`` silences the notice that the ww checkout is behind its remote.
    update_check: bool = True
    # Analyse operator feedback into candidates, never automatic rules.
    feedback_learning: bool = True
    # Local collection of ww's self-assessment and of workflow feedback.
    debug: DebugSettings = DebugSettings()
    feedback: FeedbackSettings = FeedbackSettings()
    # Built-in workflows switched off for this project.
    disabled_workflows: frozenset[str] = frozenset()
    # Names under ``workflows`` of workflows ww no longer provides; ignored.
    retired_workflows: tuple[str, ...] = ()
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
        """Whether a built-in workflow is offered in this project."""
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

    The user, repo, and local files apply in that order, each optional;
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
    if not configuration_file_exists(level.path):
        return None
    try:
        raw = json.loads(read_configuration_file(level.path))
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


def _parse_switches(
    raw: object, path: str, key: str, names: tuple[str, ...]
) -> dict[str, bool]:
    """An object of boolean switches, each defaulting to ``false``."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path}.{key} must be an object")
    unknown = set(raw) - set(names)
    if unknown:
        raise ConfigurationError(
            f"{path}.{key} has unknown key(s): " + ", ".join(sorted(unknown))
        )
    for name, value in raw.items():
        if not isinstance(value, bool):
            raise ConfigurationError(f"{path}.{key}.{name} must be true or false")
    return {name: bool(value) for name, value in raw.items()}


def _parse_debug(raw: object, path: str) -> DebugSettings:
    return DebugSettings(**_parse_switches(raw, path, "debug", ("collect", "report")))


def _parse_feedback(raw: object, path: str) -> FeedbackSettings:
    return FeedbackSettings(**_parse_switches(raw, path, "feedback", ("collect",)))


def _parse_settings(raw: dict[str, Any], path: str) -> ProjectConfig:
    unknown = set(raw) - {
        "enabled",
        "runtime",
        "extensions",
        "builtins",
        "limits",
        "agent_hooks",
        "pages",
        "projects",
        "update_check",
        "feedback_learning",
        "debug",
        "feedback",
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
    feedback_learning = raw.get("feedback_learning", True)
    if not isinstance(feedback_learning, bool):
        raise ConfigurationError(f"{path}.feedback_learning must be true or false")
    debug = _parse_debug(raw.get("debug"), path)
    feedback = _parse_feedback(raw.get("feedback"), path)
    runtime = raw.get("runtime", DEFAULT_RUNTIME)
    if runtime not in RUNTIME_INSTRUCTIONS:
        raise ConfigurationError(
            f"{path}.runtime must be one of: " + ", ".join(RUNTIME_INSTRUCTIONS)
        )
    disabled_workflows, retired_workflows = _parse_workflows(raw.get("workflows"), path)
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
        limits=_parse_limits(raw.get("limits"), path),
        agent_hooks=_parse_agent_hooks(raw.get("agent_hooks"), path),
        pages=_parse_pages(raw.get("pages"), path),
        enabled=enabled,
        projects=_parse_projects(raw.get("projects"), path),
        runtime=runtime,
        update_check=update_check,
        feedback_learning=feedback_learning,
        debug=debug,
        feedback=feedback,
        disabled_workflows=disabled_workflows,
        retired_workflows=retired_workflows,
        executable=_parse_executable(raw.get("executable"), path),
        task_format=_parse_task_format(raw.get("task_format"), path),
        **_parse_rules(raw.get("rules"), path),
    )


def _parse_limits(data: Any, path: str) -> Limits:
    """``limits``: optional positive ``rounds`` and ``fixes``, and ``auto_retries``."""
    if data is None:
        return Limits()
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}.limits must be an object")
    unknown = set(data) - {"rounds", "fixes", "auto_retries"}
    if unknown:
        raise ConfigurationError(
            f"{path}.limits has unknown key(s): {', '.join(sorted(unknown))}"
        )
    for key, value in data.items():
        if key == "auto_retries":
            if not is_strict_int(value) or value < 0:
                raise ConfigurationError(
                    f"{path}.limits.auto_retries must be a non-negative integer"
                )
        elif not is_positive_int(value):
            raise ConfigurationError(f"{path}.limits.{key} must be a positive integer")
    return Limits(**data)


def _parse_pages(data: Any, path: str) -> Pages:
    """``pages``: an optional ``worker_requirements``, ``full`` or ``pointer``."""
    if data is None:
        return Pages()
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}.pages must be an object")
    unknown = set(data) - {"worker_requirements"}
    if unknown:
        raise ConfigurationError(
            f"{path}.pages has unknown key(s): {', '.join(sorted(unknown))}"
        )
    value = data.get("worker_requirements", "full")
    if value not in WORKER_REQUIREMENTS:
        raise ConfigurationError(
            f"{path}.pages.worker_requirements must be one of: "
            + ", ".join(WORKER_REQUIREMENTS)
        )
    return Pages(**data)


def _parse_agent_hooks(data: Any, path: str) -> AgentHooks:
    """``agent_hooks``: an optional ``check_unfinished`` and ``recent_days``."""
    if data is None:
        return AgentHooks()
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}.agent_hooks must be an object")
    unknown = set(data) - {"check_unfinished", "recent_days"}
    if unknown:
        raise ConfigurationError(
            f"{path}.agent_hooks has unknown key(s): {', '.join(sorted(unknown))}"
        )
    if not isinstance(data.get("check_unfinished", True), bool):
        raise ConfigurationError(
            f"{path}.agent_hooks.check_unfinished must be true or false"
        )
    if not is_positive_int(data.get("recent_days", DEFAULT_RECENT_DAYS)):
        raise ConfigurationError(
            f"{path}.agent_hooks.recent_days must be a positive integer"
        )
    return AgentHooks(**data)


def _parse_rules(data: Any, path: str) -> dict[str, Any]:
    """``rules``: an optional ``check_guidance`` text."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}.rules must be an object")
    unknown = set(data) - {"check_guidance"}
    if unknown:
        raise ConfigurationError(
            f"{path}.rules has unknown key(s): {', '.join(sorted(unknown))}"
        )
    guidance = data.get("check_guidance")
    if guidance is not None and not isinstance(guidance, str):
        raise ConfigurationError(f"{path}.rules.check_guidance must be a string")
    return {
        # Blank text means unset.
        "rule_check_guidance": (guidance or "").strip() or None,
    }


def _parse_enabled(data: Any, path: str) -> Enabled:
    if isinstance(data, bool):
        return data
    if data == ON_REQUEST:
        return ON_REQUEST
    raise ConfigurationError(f'{path}.enabled must be true, false, or "{ON_REQUEST}"')


def _parse_task_format(data: Any, path: str) -> str | None:
    if data is None:
        return None
    if not isinstance(data, str) or not data:
        raise ConfigurationError(f"{path}.task_format must be a non-empty string")
    if data == EXPLICIT_TASK_FORMAT:
        return data
    tokens = _TASK_FORMAT_TOKEN.findall(data)
    rest = _TASK_FORMAT_TOKEN.sub("", data)
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


def _parse_workflows(data: Any, path: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """The built-ins switched off by ``enabled: false`` and the retired names."""
    if data is None:
        return frozenset(), ()
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}.workflows must be an object")
    known = builtin_workflow_names()
    retired = tuple(sorted(set(data) & RETIRED_WORKFLOWS))
    unknown = set(data) - known - RETIRED_WORKFLOWS
    if unknown:
        raise ConfigurationError(
            f"{path}.workflows has unknown name(s): {', '.join(sorted(unknown))}; "
            "built-in workflows: " + ", ".join(sorted(known))
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
        if not enabled and name not in RETIRED_WORKFLOWS:
            disabled.add(name)
    return frozenset(disabled), retired


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
