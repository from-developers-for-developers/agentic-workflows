# SPDX-License-Identifier: GPL-3.0-or-later
"""Compose ``workflows.yaml`` and the files it imports into one document.

The root file may list other YAML files under ``imports``, its first key. Each
import may define anything the root can, except further imports. Definitions
fold in import order and the root file last, so a later file overrides an
earlier one and the root overrides every import:

- named catalogs (``modes``, ``documents``, ``handlers``, ``workflows``) and
  ``profiles`` replace an entry of the same name where it first appeared, so
  handlers that reuse an overridden one still find it earlier in the list;
- ``hooks`` add each phase's entries after those already folded, since hook
  entries carry no name to override;
- any other key, such as ``task_format``, takes the later value.

The composed document is ordinary ``workflows.yaml`` notation: the parser reads
it exactly as it would a single file, and nothing is written to disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ww.errors import ConfigurationError

IMPORTS_KEY = "imports"

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
    """The composed ``workflows.yaml`` text, its mapping, and its overrides."""

    text: str
    raw: dict[str, Any]
    overrides: tuple[Override, ...] = ()


def compose_configuration(path: Path) -> ComposedConfiguration:
    """Read ``path`` and fold the files it imports into one document.

    A root file without ``imports`` passes through untouched, so the parser
    reports its errors exactly as before.
    """
    text = path.read_text(encoding="utf-8")
    try:
        root = yaml.safe_load(text)
    except yaml.YAMLError:
        return ComposedConfiguration(text, {})
    if not isinstance(root, dict) or IMPORTS_KEY not in root:
        return ComposedConfiguration(text, root if isinstance(root, dict) else {})
    if next(iter(root)) != IMPORTS_KEY:
        raise ConfigurationError(f"{IMPORTS_KEY} must be the first key in {path.name}")
    imports = root.pop(IMPORTS_KEY)
    composer = _Composer()
    for label, file in _import_files(imports, path):
        composer.apply(_read_import(file, label), label)
    composer.apply(root, path.name)
    return ComposedConfiguration(
        yaml.safe_dump(composer.raw, sort_keys=False, allow_unicode=True),
        composer.raw,
        tuple(composer.overrides),
    )


def _import_files(imports: Any, root: Path) -> list[tuple[str, Path]]:
    if not isinstance(imports, list):
        raise ConfigurationError(f"{IMPORTS_KEY} must be a list of file paths")
    result: list[tuple[str, Path]] = []
    seen = {root.resolve()}
    for index, entry in enumerate(imports):
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigurationError(
                f"{IMPORTS_KEY}[{index}] must be a non-empty file path"
            )
        file = root.parent / entry
        if not file.is_file():
            raise ConfigurationError(f"imported file not found: {entry}")
        resolved = file.resolve()
        if resolved in seen:
            raise ConfigurationError(
                f"{IMPORTS_KEY}[{index}] repeats {entry}"
                if resolved != root.resolve()
                else f"{IMPORTS_KEY}[{index}] imports {root.name} itself"
            )
        seen.add(resolved)
        result.append((entry, file))
    return result


def _read_import(file: Path, label: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(f"cannot read {label}: {error}") from error
    except yaml.YAMLError as error:
        raise ConfigurationError(f"invalid YAML in {label}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{label} must contain a mapping")
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
