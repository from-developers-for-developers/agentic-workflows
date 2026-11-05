# SPDX-License-Identifier: GPL-3.0-or-later
"""The configuration files of a project, and what the migrations find there.

:func:`project_files` names every file a project's configuration is read
from: the YAML files composition folds (the user's ``ww.yaml``, the repo's
and its imports, ``ww.local.yaml``), the rule files the configuration's own
rule groups and steps use (an extension's shipped rules are its own), and the
settings files (the user's ``ww.json``, the repo's, ``ww.local.json``, and
each configured project's own). :func:`project_findings` runs the
migrations over them without writing anything, for ``ww lint``.

:func:`migrate_project` does the same for ``ww migrate``, which must work
when the configuration no longer loads, since a removed construct is what
stops it loading. It reads the configuration and the settings when they
load; when one does not, it finds the files from the files themselves: the
imports each workflow file lists, the rule files and directories its rule
groups and steps name that exist, the projects ``ww.json`` lists, and the
extensions the settings files configure.

This module reads the configuration, which itself loads through
:mod:`ww.migrations.config`, so the package does not import it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ww.config import load_configuration
from ww.config.composition import IMPORTS_KEY, configuration_files
from ww.config.rules import rule_files
from ww.config_files import (
    checkout_directory,
    display_path,
    project_settings_levels,
    settings_levels,
    workflow_levels,
)
from ww.errors import WwError
from ww.extensions import ExtensionRegistry
from ww.workflow_config import RuleDefinition, WorkflowConfiguration, every_step

from . import catalog
from .catalog import MigrationSet
from .files import EXTENSIONS_KEY, ConfigFile, FileMigration, migrate_files
from .migration import ConfigMigration, Finding

# The origin of a rule group the project's own configuration declares.
_OWN_RULES = "configuration"
_RULES_KEY = "rules"
_PROJECTS_KEY = "projects"


def project_files(
    config_path: Path,
    settings_path: Path,
    configuration: WorkflowConfiguration | None = None,
    extensions: ExtensionRegistry | None = None,
) -> tuple[ConfigFile, ...]:
    """Every configuration file of the project whose repo files are given.

    Rule files are listed when ``configuration`` is, and configured projects'
    own settings files when ``extensions`` is, whose settings name them.
    """
    root = checkout_directory(config_path.parent)
    return _files(
        config_path,
        configuration_files(config_path),
        _rule_files(configuration) if configuration is not None else (),
        _settings_files(
            settings_path,
            tuple(project.directory(root) for project in extensions.config.projects)
            if extensions is not None
            else (),
        ),
    )


def _files(
    config_path: Path,
    workflows: tuple[Path, ...],
    rules: tuple[Path, ...],
    settings: tuple[Path, ...],
) -> tuple[ConfigFile, ...]:
    base = config_path.parent
    return (
        *(
            ConfigFile(path, "workflows", display_path(path, base))
            for path in workflows
        ),
        *(ConfigFile(path, "rules", display_path(path, base)) for path in rules),
        *(ConfigFile(path, "settings", display_path(path, base)) for path in settings),
    )


def _settings_files(
    settings_path: Path, projects: tuple[Path, ...]
) -> tuple[Path, ...]:
    """The settings files there are: the root's levels, then each project's."""
    levels = [
        *settings_levels(settings_path),
        *(
            level
            for directory in projects
            for level in project_settings_levels(directory)
        ),
    ]
    return tuple(level.path for level in levels if level.path.is_file())


def _rule_files(configuration: WorkflowConfiguration) -> tuple[Path, ...]:
    """The rule files of the configuration's own groups and steps, once each."""
    rules: list[RuleDefinition] = [
        rule
        for group in configuration.rule_groups
        if group.origin == _OWN_RULES
        for rule in group.rules
    ]
    rules.extend(
        entry
        for step in every_step(configuration)
        for entry in step.rules
        if isinstance(entry, RuleDefinition)
    )
    found: dict[Path, None] = {}
    for rule in rules:
        if rule.source is not None and Path(rule.source).is_file():
            found.setdefault(Path(rule.source), None)
    return tuple(found)


def project_findings(
    config_path: Path,
    settings_path: Path,
    configuration: WorkflowConfiguration | None = None,
    extensions: ExtensionRegistry | None = None,
    choices: Mapping[str, str] | None = None,
) -> tuple[FileMigration, ...]:
    """Every project file migrated in memory, with what each migration found.

    With ``extensions``, the configured extensions are loaded for their
    settings migrations.
    """
    extension_sets: dict[str, MigrationSet] = {}
    if extensions is not None:
        extension_sets.update(extensions.settings_migrations())
        for project in extensions.config.projects:
            extension_sets.update(extensions.settings_migrations(project.name))
    return migrate_files(
        project_files(config_path, settings_path, configuration, extensions),
        extensions=extension_sets,
        choices=choices,
    )


# ----- ww migrate ----------------------------------------------------------


@dataclass(frozen=True)
class ProjectMigration:
    """What the migrations find in every configuration file of a project.

    ``files`` are migrated in memory; nothing is written. ``migrations`` holds
    every migration that can apply, ww's own and the configured extensions'
    (with their qualified IDs), by ID. ``unloaded`` says why the
    configuration or the settings did not load, when their files were found
    from the files themselves instead.
    """

    files: tuple[FileMigration, ...]
    migrations: Mapping[str, ConfigMigration]
    unloaded: tuple[str, ...] = ()

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(finding for file in self.files for finding in file.findings)

    @property
    def changed(self) -> tuple[FileMigration, ...]:
        return tuple(file for file in self.files if file.changed)


def _extension_migrations(
    extensions: ExtensionRegistry, settings_path: Path
) -> tuple[dict[str, MigrationSet], tuple[Path, ...], tuple[str, ...]]:
    """The configured extensions' settings migrations and the projects' directories.

    Read from the settings when they load, else from the settings files
    themselves; the third value says why they did not load.
    """
    root = checkout_directory(settings_path.parent)
    sets: dict[str, MigrationSet] = {}
    try:
        projects = extensions.config.projects
        sets.update(extensions.settings_migrations())
        for project in projects:
            sets.update(extensions.settings_migrations(project.name))
        return sets, tuple(project.directory(root) for project in projects), ()
    except (WwError, OSError) as error:
        unloaded = (str(error),)
    directories = tuple(_raw_projects(settings_path, root))
    keys = {
        key
        for path in _settings_files(settings_path, directories)
        for key in _mapping(_read_json(path).get(EXTENSIONS_KEY))
    }
    return extensions.section_migrations(sorted(keys)), directories, unloaded


def migrate_project(
    config_path: Path,
    settings_path: Path,
    extensions: ExtensionRegistry,
    choices: Mapping[str, str] | None = None,
) -> ProjectMigration:
    """Every configuration file of the project, migrated in memory.

    ``config_path`` and ``settings_path`` are the checkout's repo files,
    where edits are made; a configuration snapshot is never migrated.
    ``choices`` maps a choice migration's ID to the option to apply.
    """
    unloaded: list[str] = []
    try:
        workflows = configuration_files(config_path)
    except (WwError, OSError) as error:
        unloaded.append(str(error))
        workflows = tuple(_raw_workflow_files(config_path))
    try:
        rules = _rule_files(load_configuration(config_path, extensions))
    except (WwError, OSError) as error:
        if str(error) not in unloaded:
            unloaded.append(str(error))
        rules = tuple(dict.fromkeys(_raw_rule_files(workflows)))
    sets, directories, settings_error = _extension_migrations(extensions, settings_path)
    unloaded.extend(settings_error)
    files = _files(
        config_path, workflows, rules, _settings_files(settings_path, directories)
    )
    known: dict[str, ConfigMigration] = {
        migration.id: migration for migration in catalog.CONFIG_MIGRATIONS
    }
    for migration_set in sets.values():
        known.update(
            (migration.id, migration) for migration in migration_set.migrations
        )
    return ProjectMigration(
        migrate_files(files, extensions=sets, choices=choices),
        known,
        tuple(unloaded),
    )


# ----- files found without loading -----------------------------------------


def _raw_workflow_files(config_path: Path) -> Iterator[Path]:
    """Each level's file there is, after the imports it lists that exist."""
    for level in workflow_levels(config_path):
        if not level.path.is_file():
            continue
        for entry in _list(_read_yaml(level.path).get(IMPORTS_KEY)):
            if isinstance(entry, str) and (level.path.parent / entry).is_file():
                yield level.path.parent / entry
        yield level.path


def _raw_rule_files(workflows: tuple[Path, ...]) -> Iterator[Path]:
    """The rule files the workflow files' rule groups and steps name.

    A rule item is a group name, a rule file or directory next to the file
    naming it, or a rule's own text; only the paths that exist are rules.
    """
    for workflow in workflows:
        document = _read_yaml(workflow)
        items = [
            item
            for group in _mapping(document.get(_RULES_KEY)).values()
            for item in _list(
                group if isinstance(group, list) else _mapping(group).get(_RULES_KEY)
            )
        ]
        items.extend(_step_rules({**document, _RULES_KEY: None}))
        for item in items:
            if not isinstance(item, str) or not item.strip():
                continue
            path = workflow.parent / item.strip()
            if path.is_dir() or (path.is_file() and path.suffix == ".md"):
                yield from rule_files(path)


def _step_rules(value: Any) -> Iterator[Any]:
    """Every entry of every ``rules`` list in ``value``, at any depth."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == _RULES_KEY and isinstance(nested, list):
                yield from nested
            yield from _step_rules(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _step_rules(nested)


def _raw_projects(settings_path: Path, root: Path) -> Iterator[Path]:
    """The directories the settings files' ``projects`` entries name."""
    for level in settings_levels(settings_path):
        for entry in _list(_read_json(level.path).get(_PROJECTS_KEY)):
            path = _mapping(entry).get("path")
            if isinstance(path, str) and path.strip():
                yield (root / path.strip()).resolve()


def _read_yaml(path: Path) -> dict[str, Any]:
    """A YAML file's first document when it is a mapping; else empty."""
    try:
        text = path.read_text(encoding="utf-8")
        return _mapping(next(yaml.safe_load_all(text), None))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}


def _read_json(path: Path) -> dict[str, Any]:
    """A JSON file's mapping; empty when it is missing or not one."""
    try:
        return _mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
