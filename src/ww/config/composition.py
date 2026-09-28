# SPDX-License-Identifier: GPL-3.0-or-later
"""Compose the workflow configuration levels and their imports into one document.

Workflows come from up to three levels, applied top to bottom: the machine's
``ww-agentic-workflows.machine.yaml``, the repo's ``ww-agentic-workflows.yaml``
(required), and the checkout's ``ww-agentic-workflows.local.yaml``. A level is
its root file plus the files that root lists under ``imports``, which come
before any other key but ``extends``. An import may define anything a root can,
except further imports, and resolves next to the file that lists it.

Files fold in order, each level's imports before its root, so a later file
overrides an earlier one and a lower level overrides the ones above it:

- named catalogs (``modes``, ``documents``, ``handlers``, ``workflows``) and
  ``profiles`` replace an entry of the same name where it first appeared, so
  handlers that reuse an overridden one still find it earlier in the list;
- ``hooks`` add each phase's entries after those already folded, since hook
  entries carry no name to override;
- any other key takes the later value.

A level extends the ones above unless one of its files says ``extends: false``;
then folding starts again at that level.

The composed document is ordinary ``ww-agentic-workflows.yaml`` notation:
the parser reads it exactly as it would a single file, and nothing is
written to disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ww.config_files import (
    ConfigurationLevel,
    display_path,
    task_format_moved,
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

    @property
    def notices(self) -> tuple[str, ...]:
        return (
            *(f"{label} is not applied: a lower level sets extends: false."
              for label in self.ignored),
            *(override.notice for override in self.overrides),
        )


@dataclass(frozen=True)
class _Level:
    """One level's files, imports first, and whether it extends the ones above."""

    files: tuple[tuple[str, dict[str, Any]], ...]
    extends: bool


def compose_configuration(path: Path) -> ComposedConfiguration:
    """Read the repo file ``path`` with its levels and imports as one document.

    A repo file standing alone, without ``imports`` or ``extends``, passes
    through untouched, so the parser reports its errors exactly as before.
    """
    base = path.parent
    label = display_path(path, base)
    text = path.read_text(encoding="utf-8")
    present = tuple(
        level
        for level in workflow_levels(path)
        if level.name == "repo" or level.path.is_file()
    )
    if len(present) == 1:
        try:
            root = yaml.safe_load(text)
        except yaml.YAMLError:
            return ComposedConfiguration(text, {}, sources=(label,))
        if not isinstance(root, dict) or not {IMPORTS_KEY, EXTENDS_KEY} & set(root):
            raw = root if isinstance(root, dict) else {}
            if "task_format" in raw:
                raise ConfigurationError(task_format_moved(label))
            return ComposedConfiguration(text, raw, sources=(label,))
    seen = {level.path.resolve() for level in present}
    levels = [_read_level(level, base, seen) for level in present]
    start = max(
        (index for index, level in enumerate(levels) if not level.extends),
        default=0,
    )
    composer = _Composer()
    for level in levels[start:]:
        for file_label, raw in level.files:
            composer.apply(raw, file_label)
    return ComposedConfiguration(
        yaml.safe_dump(composer.raw, sort_keys=False, allow_unicode=True),
        composer.raw,
        tuple(composer.overrides),
        tuple(file_label for level in levels[start:] for file_label, _ in level.files),
        tuple(file_label for level in levels[:start] for file_label, _ in level.files),
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
        (file_label, _read_import(file, file_label))
        for file_label, file in _import_files(imports, level.path, base, seen)
    ]
    files.append((root_label, root))
    extends = [_extends(raw, file_label) for file_label, raw in files]
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
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(f"cannot read {label}: {error}") from error
    except yaml.YAMLError as error:
        raise ConfigurationError(f"invalid YAML in {label}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{label} must contain a mapping")
    if "task_format" in raw:
        raise ConfigurationError(task_format_moved(label))
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
        if not file.is_file():
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
                case _:
                    self._record(key, None, key, label)
                    self.raw[key] = value
                    self._note_origins(key, value, label)

    def _note_origins(self, key: str, value: Any, label: str) -> None:
        self._origins[(key, None)] = label
        if key in _NAMED_CATALOGS and isinstance(value, list):
            for entry in value:
                self._origins[(key, _entry_name(entry))] = label
        elif key == "profiles" and isinstance(value, dict):
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
            if (name := _entry_name(entry)) is not None
        }
        for entry in entries:
            name = _entry_name(entry)
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

    def _record(self, key: str, name: str | None, kind: str, label: str) -> None:
        self.overrides.append(
            Override(
                kind,
                name,
                self._origins[(key, name)],
                label,
            )
        )


def _entry_name(entry: Any) -> str | None:
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
