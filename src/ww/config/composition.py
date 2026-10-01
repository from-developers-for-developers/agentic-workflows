# SPDX-License-Identifier: GPL-3.0-or-later
"""Compose the workflow configuration levels and their imports into one document.

Workflows come from up to three levels, applied top to bottom: the user's
``ww-agentic-workflows.yaml`` in the user configuration directory, the repo's
``ww-agentic-workflows.yaml`` (required), and the checkout's
``ww-agentic-workflows.local.yaml``. A level is its root file plus the files
that root lists under ``imports``, which come before any other key but
``extends``. An import may define anything a root can, except further imports,
and resolves next to the file that lists it.

Files fold in order, each level's imports before its root, so a later file
overrides an earlier one and a lower level overrides the ones above it:

- named catalogs (``modes``, ``documents``, ``handlers``, ``workflows``) and
  ``profiles`` replace an entry of the same name where it first appeared, so
  handlers that reuse an overridden one still find it earlier in the list;
- ``hooks`` add each phase's entries after those already folded, since hook
  entries carry no name to override;
- ``rules`` groups replace a group of the same name as a whole;
- any other key takes the later value.

Rule paths, in the root ``rules`` mapping and in a step's ``rules`` list,
resolve next to the file that declares them. A file outside the repo file's
directory has its relative rule paths rewritten against that directory, so
the parser resolves every one against a single base. An absolute rule path is
accepted and reported as a notice, because it ties the configuration to one
machine.

A level extends the ones above unless one of its files says ``extends: false``;
then folding starts again at that level.

The composed document is ordinary ``ww-agentic-workflows.yaml`` notation:
the parser reads it exactly as it would a single file, and nothing is
written to disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ww.config_files import (
    ConfigurationLevel,
    configuration_file_exists,
    display_path,
    read_configuration_file,
    workflow_levels,
)
from ww.errors import ConfigurationError

IMPORTS_KEY = "imports"
EXTENDS_KEY = "extends"

_NAMED_CATALOGS = {
    "modes": "mode",
    "documents": "document",
    "handlers": "handler",
    "workflows": "workflow",
}


@dataclass(frozen=True)
class Override:
    """One definition that a higher-priority file replaced."""

    kind: str
    name: str | None
    overridden_in: str
    overridden_by: str

    @property
    def notice(self) -> str:
        subject = self.kind if self.name is None else f"{self.kind} {self.name!r}"
        return (
            f"{subject} from {self.overridden_in} is overridden by "
            f"{self.overridden_by}."
        )


@dataclass(frozen=True)
class ComposedConfiguration:
    """Composed ``ww-agentic-workflows.yaml`` text, its mapping, and overrides.

    ``sources`` lists the files folded in, in order; ``ignored`` the files a
    lower level's ``extends: false`` left out.
    """

    text: str
    raw: dict[str, Any]
    overrides: tuple[Override, ...] = ()
    sources: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    # One notice per absolute rule path, and per manager step that also asks
    # for worker settings, in the files folded in.
    rule_notices: tuple[str, ...] = ()

    @property
    def notices(self) -> tuple[str, ...]:
        return (
            *(f"{label} is not applied: a lower level sets extends: false."
              for label in self.ignored),
            *(override.notice for override in self.overrides),
            *self.rule_notices,
        )


@dataclass(frozen=True)
class _Level:
    """One level's files, imports first, and whether it extends the ones above."""

    files: tuple[tuple[str, dict[str, Any], Path], ...]
    extends: bool


def compose_configuration(path: Path) -> ComposedConfiguration:
    """Read the repo file ``path`` with its levels and imports as one document.

    A repo file standing alone, without ``imports`` or ``extends``, passes
    through untouched, so the parser reports its errors exactly as before.
    """
    base = path.parent
    label = display_path(path, base)
    text = read_configuration_file(path)
    present = tuple(
        level
        for level in workflow_levels(path)
        if level.name == "repo" or configuration_file_exists(level.path)
    )
    if len(present) == 1:
        try:
            root = yaml.safe_load(text)
        except yaml.YAMLError:
            return ComposedConfiguration(text, {}, sources=(label,))
        if not isinstance(root, dict) or not {IMPORTS_KEY, EXTENDS_KEY} & set(root):
            raw = root if isinstance(root, dict) else {}
            return ComposedConfiguration(
                text,
                raw,
                sources=(label,),
                rule_notices=(
                    *_absolute_rule_notices(raw, label),
                    *_manager_setting_notices(raw, label),
                ),
            )
    seen = {level.path.resolve() for level in present}
    levels = [_read_level(level, base, seen) for level in present]
    start = max(
        (index for index, level in enumerate(levels) if not level.extends),
        default=0,
    )
    applied = [file for level in levels[start:] for file in level.files]
    group_names = {
        name
        for _, raw, _ in applied
        if isinstance(raw.get("rules"), dict)
        for name in raw["rules"]
    }
    composer = _Composer()
    notices: list[str] = []
    for file_label, raw, file in applied:
        notices.extend(_absolute_rule_notices(raw, file_label))
        notices.extend(_manager_setting_notices(raw, file_label))
        composer.apply(
            _rebase_rule_paths(raw, file.parent, base, group_names), file_label
        )
    return ComposedConfiguration(
        yaml.safe_dump(composer.raw, sort_keys=False, allow_unicode=True),
        composer.raw,
        tuple(composer.overrides),
        tuple(file_label for file_label, _, _ in applied),
        tuple(
            file_label for level in levels[:start] for file_label, _, _ in level.files
        ),
        tuple(notices),
    )


def _read_level(
    level: ConfigurationLevel, base: Path, seen: set[Path]
) -> _Level:
    root_label = display_path(level.path, base)
    root = _read_file(level.path, root_label)
    keys = [key for key in root if key != EXTENDS_KEY]
    if IMPORTS_KEY in keys and keys[0] != IMPORTS_KEY:
        raise ConfigurationError(
            f"{IMPORTS_KEY} must come before every key but {EXTENDS_KEY} "
            f"in {root_label}"
        )
    imports = root.pop(IMPORTS_KEY, [])
    files = [
        (file_label, _read_import(file, file_label), file)
        for file_label, file in _import_files(imports, level.path, base, seen)
    ]
    files.append((root_label, root, level.path))
    extends = [_extends(raw, file_label) for file_label, raw, _ in files]
    return _Level(tuple(files), False not in extends)


def _extends(raw: dict[str, Any], label: str) -> bool | None:
    """Take a file's ``extends`` out of its definitions and validate it."""
    if EXTENDS_KEY not in raw:
        return None
    value = raw.pop(EXTENDS_KEY)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{label}: {EXTENDS_KEY} must be true or false")
    return value


def _read_file(file: Path, label: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(read_configuration_file(file))
    except OSError as error:
        raise ConfigurationError(f"cannot read {label}: {error}") from error
    except yaml.YAMLError as error:
        raise ConfigurationError(f"invalid YAML in {label}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{label} must contain a mapping")
    return raw


def _import_files(
    imports: Any, root: Path, base: Path, seen: set[Path]
) -> list[tuple[str, Path]]:
    root_label = display_path(root, base)
    if not isinstance(imports, list):
        raise ConfigurationError(
            f"{IMPORTS_KEY} in {root_label} must be a list of file paths"
        )
    result: list[tuple[str, Path]] = []
    for index, entry in enumerate(imports):
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigurationError(
                f"{root_label} {IMPORTS_KEY}[{index}] must be a non-empty file path"
            )
        file = root.parent / entry
        if not configuration_file_exists(file):
            raise ConfigurationError(
                f"imported file not found: {entry} (listed in {root_label})"
            )
        resolved = file.resolve()
        if resolved in seen:
            raise ConfigurationError(
                f"{root_label} {IMPORTS_KEY}[{index}] imports {entry}, which "
                "is already a configuration file or an import"
            )
        seen.add(resolved)
        result.append((display_path(file, base), file))
    return result


def _read_import(file: Path, label: str) -> dict[str, Any]:
    raw = _read_file(file, label)
    if IMPORTS_KEY in raw:
        raise ConfigurationError(
            f"{label} cannot import other files; list every import in the root file"
        )
    return raw


class _Composer:
    """Fold configuration mappings, each later one overriding the earlier."""

    def __init__(self) -> None:
        self.raw: dict[str, Any] = {}
        self.overrides: list[Override] = []
        # Which file supplied each definition, for override notices.
        self._origins: dict[tuple[str, str | None], str] = {}

    def apply(self, raw: dict[str, Any], label: str) -> None:
        for key, value in raw.items():
            current = self.raw.get(key)
            match key, current, value:
                case _, None, _:
                    self.raw[key] = value
                    self._note_origins(key, value, label)
                case str(), list(), list() if key in _NAMED_CATALOGS:
                    self._merge_named(key, current, value, label)
                case "profiles", dict(), dict():
                    self._merge_profiles(current, value, label)
                case "hooks", dict(), dict():
                    _merge_hooks(current, value)
                case "rules", dict(), dict():
                    self._merge_rule_groups(current, value, label)
                case _:
                    self._record(key, None, key, label)
                    self.raw[key] = value
                    self._note_origins(key, value, label)

    def _note_origins(self, key: str, value: Any, label: str) -> None:
        self._origins[(key, None)] = label
        if key in _NAMED_CATALOGS and isinstance(value, list):
            for entry in value:
                self._origins[(key, entry_name(entry))] = label
        elif key in {"profiles", "rules"} and isinstance(value, dict):
            for name in value:
                self._origins[(key, name)] = label

    def _merge_named(
        self, key: str, merged: list[Any], entries: list[Any], label: str
    ) -> None:
        """Replace same-named entries in place and append the rest.

        Only names from earlier files are replaced: a name repeated within one
        file stays repeated, so validation still reports the duplicate.
        """
        positions = {
            name: index
            for index, entry in enumerate(merged)
            if (name := entry_name(entry)) is not None
        }
        for entry in entries:
            name = entry_name(entry)
            if name is not None and name in positions:
                self._record(key, name, _NAMED_CATALOGS[key], label)
                merged[positions.pop(name)] = entry
            else:
                merged.append(entry)
            self._origins[(key, name)] = label

    def _merge_profiles(
        self, merged: dict[str, Any], profiles: dict[str, Any], label: str
    ) -> None:
        for name, description in profiles.items():
            if name in merged:
                self._record("profiles", name, "profile", label)
            merged[name] = description
            self._origins[("profiles", name)] = label

    def _merge_rule_groups(
        self, merged: dict[str, Any], groups: dict[str, Any], label: str
    ) -> None:
        """A later file's group replaces the earlier group of that name whole."""
        for name, group in groups.items():
            if name in merged:
                self._record("rules", name, "rule group", label)
            merged[name] = group
            self._origins[("rules", name)] = label

    def _record(self, key: str, name: str | None, kind: str, label: str) -> None:
        self.overrides.append(
            Override(
                kind,
                name,
                self._origins[(key, name)],
                label,
            )
        )


def entry_name(entry: Any) -> str | None:
    """The name a catalog entry declares, explicitly or by shorthand.

    An entry without a usable name is kept as it is, for the parser to report.
    """
    if not isinstance(entry, dict) or not entry:
        return None
    name = entry["name"] if "name" in entry else next(iter(entry))
    return name if isinstance(name, str) else None


def _merge_hooks(merged: dict[str, Any], hooks: dict[str, Any]) -> None:
    """Run a later file's hooks after the earlier ones, phase by phase."""
    for phase, entries in hooks.items():
        existing = merged.get(phase)
        if isinstance(existing, list) and isinstance(entries, list):
            merged[phase] = [*existing, *entries]
        else:
            merged[phase] = entries


def _rule_path_lists(raw: dict[str, Any]) -> list[list[Any]]:
    """Every list of rule items in one file: root groups and step ``rules``."""
    found: list[list[Any]] = []
    groups = raw.get("rules")
    if isinstance(groups, dict):
        for group in groups.values():
            if isinstance(group, list):
                found.append(group)
            elif isinstance(group, dict) and isinstance(group.get("rules"), list):
                found.append(group["rules"])

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "rules" and isinstance(nested, list):
                    found.append(nested)
                else:
                    walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(raw.get("workflows"))
    walk(raw.get("handlers"))
    return found


def _absolute_rule_notices(raw: dict[str, Any], label: str) -> tuple[str, ...]:
    return tuple(
        f"rule path {item} in {label} is absolute; it applies only on this machine."
        for items in _rule_path_lists(raw)
        for item in items
        if isinstance(item, str) and Path(item).is_absolute() and Path(item).exists()
    )


_WORKER_SETTINGS = ("agent", "model", "reasoning", "profile")


def _manager_setting_notices(raw: dict[str, Any], label: str) -> tuple[str, ...]:
    """A step or workflow with ``role: manager`` that also asks for a worker.

    The manager is whichever session runs the task, so those settings have no
    effect there; they are kept for the steps below that are delegated.
    """
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("role") == "manager":
                settings = [key for key in _WORKER_SETTINGS if key in value]
                if settings:
                    name = entry_name(value) or "a step"
                    found.append(
                        f"{name} in {label} has role: manager, so "
                        + ", ".join(settings)
                        + " has no effect on it; only nested steps that set "
                        "role: worker use it."
                    )
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(raw.get("workflows"))
    walk(raw.get("handlers"))
    return tuple(found)


def _rebase_rule_paths(
    raw: dict[str, Any], directory: Path, base: Path, group_names: set[str]
) -> dict[str, Any]:
    """Rewrite ``raw``'s relative rule paths from ``directory`` to ``base``.

    Only strings that exist as paths next to the declaring file and are not
    group names change; a sentence stays the rule text it is.
    """
    if directory.resolve() == base.resolve():
        return raw
    for items in _rule_path_lists(raw):
        for index, item in enumerate(items):
            if (
                isinstance(item, str)
                and item not in group_names
                and not Path(item).is_absolute()
                and (directory / item).exists()
            ):
                items[index] = os.path.relpath(directory / item, base)
    return raw
