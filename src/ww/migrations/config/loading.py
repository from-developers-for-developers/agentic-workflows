# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration migrations applied in memory, as ww loads each file.

Every loader hands the mapping it parsed from one file to
:func:`migrate_loaded` before validating it: ``ww.yaml`` and the files it
imports, ``ww.local.yaml`` and the user's ``ww.yaml``, each rule file's
frontmatter, and each settings file. A migration in its deprecation window
changes the mapping to the current shape, so the parser and the engine never
see the old one, and yields a notice for the operator; a construct past its
removal is an error that names the removal and the upgrade guide, instead of
the parser's bare unknown key. The file itself is never written here, so a
configuration snapshot under ``.ww/config/<sha>/`` is read the same way and
stays as its commit has it; ``ww migrate`` changes the files.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from ww.errors import ConfigurationError

from .catalog import MigrationSet
from .changes import MigrationConflictError
from .migration import ConfigMigration
from .paths import Location, render
from .yaml_text import YamlText


@dataclass(frozen=True)
class Migrated:
    """A file's mapping in the current shape, and the notices it earned."""

    data: Any
    notices: tuple[str, ...] = ()


def migrate_loaded(
    data: Any,
    label: str,
    sets: Sequence[MigrationSet],
    *,
    text: str | None = None,
    offset: int = 0,
    prefix: tuple[str, ...] = (),
) -> Migrated:
    """``data``, parsed from the file ``label``, in the current shape.

    ``text`` is the file's text, read for line numbers only when something
    is found; ``offset`` the lines before it in the file, as a rule file's
    frontmatter has. ``prefix`` is where ``data`` lies in the file, such as
    an extension's ``extensions.<id>`` section, for messages. The caller's
    ``data`` is never changed: a migrated copy is returned.

    Raises :class:`ConfigurationError` for a removed construct, and for a
    file holding a construct in both its old and its new shape.
    """
    notices: list[str] = []
    lines = _Lines(text, offset)
    for migration_set in sets:
        for migration in migration_set.migrations:
            status = migration.status(migration_set.version)
            if status == "expired":
                continue
            locations = migration.find(data)
            if not locations:
                continue
            for location in locations:
                where = _where(label, (*prefix, *location), lines)
                message = migration.message(status, location, migration_set.owner)
                if status == "removed":
                    raise ConfigurationError(f"{where}: {message}")
                notices.append(f"Deprecated: {where}: {message}")
            data = _transform(migration, data, label, prefix)
    return Migrated(data, tuple(notices))


def _transform(
    migration: ConfigMigration, data: Any, label: str, prefix: tuple[str, ...]
) -> Any:
    if migration.kind == "manual":
        return data
    try:
        return migration.transform(data)
    except MigrationConflictError as conflict:
        where = f"{label}:{render(prefix)}" if prefix else label
        raise ConfigurationError(f"{where}: {conflict}") from conflict


class _Lines:
    """Line numbers of locations in a file's text, composed when first asked."""

    def __init__(self, text: str | None, offset: int) -> None:
        self._text = text
        self._offset = offset
        self._source: YamlText | None = None

    def of(self, location: Location) -> int | None:
        if self._text is None:
            return None
        if self._source is None:
            try:
                # JSON is YAML too, near enough to find a key's line.
                self._source = YamlText(self._text, self._offset)
            except yaml.YAMLError:
                self._text = None
                return None
        return self._source.line_of(location)


def _where(label: str, location: Location, lines: _Lines) -> str:
    line = lines.of(location)
    return f"{label}:{render(location)}" + (f" (line {line})" if line else "")
