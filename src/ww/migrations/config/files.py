# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration migrations applied to the files themselves.

:func:`migrate_file` reads one configuration file and returns a
:class:`FileMigration`: what each migration found there, as
:class:`Finding` values, and the file's new text with every automatic fix
made. Nothing is written until the caller asks: :meth:`FileMigration.diff`
shows the change, :func:`write_files` writes several files together or none.

A YAML file (``ww.yaml``, an import, a local or user file, a rule file's
frontmatter) is edited in place, change by change, at the characters its
nodes occupy, so comments, blank lines and quoting elsewhere stay exactly as
they were. A place ww cannot edit safely (see :mod:`.yaml_text`) is left for
the operator: its finding becomes ``manual``, with what to change by hand.
After a migration's edits the text is parsed again and must equal what the
same migration makes of the file in memory; if it does not, none of that
migration's edits is kept and its findings become ``manual``.

A JSON settings file is parsed, migrated and written again as ww writes
``ww.json``: two-space indentation and a final newline.

A choice migration changes a file only once the caller passes the chosen
option in ``choices``, by migration ID; until then its findings are
``choice``.
"""

from __future__ import annotations

import copy
import difflib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from ww.config_writes import FileWrite, Transaction

from .catalog import MigrationSet, ww_migrations
from .changes import Change, MigrationConflictError
from .migration import ConfigMigration, FileKind, Finding, Status
from .paths import Location, render
from .yaml_text import EditRefusedError, YamlText, apply_edits

# The key in a settings file that holds each extension's section.
EXTENSIONS_KEY = "extensions"


@dataclass(frozen=True)
class ConfigFile:
    """A configuration file to migrate: where, what kind, and its name in messages."""

    path: Path
    kind: FileKind
    label: str


@dataclass(frozen=True)
class FileMigration:
    """One file's findings and its text with the automatic fixes made."""

    file: ConfigFile
    original: str
    text: str
    findings: tuple[Finding, ...] = ()

    @property
    def changed(self) -> bool:
        return self.text != self.original

    def diff(self) -> str:
        """A unified diff of the change, empty when there is none."""
        return "".join(
            difflib.unified_diff(
                self.original.splitlines(True),
                self.text.splitlines(True),
                f"a/{self.file.label}",
                f"b/{self.file.label}",
            )
        )

    def write(self) -> FileWrite:
        return FileWrite(self.file.path, self.text)


def migrate_file(
    file: ConfigFile,
    *,
    sets: Sequence[MigrationSet] | None = None,
    extensions: Mapping[str, MigrationSet] | None = None,
    choices: Mapping[str, str] | None = None,
) -> FileMigration:
    """``file`` read and migrated, without writing it.

    ``sets`` defaults to ww's own migrations for the file's kind.
    ``extensions`` maps an extension identifier to its settings migrations,
    applied in a settings file inside the section that names it, by
    identifier or bare name. ``choices`` maps a choice migration's ID to the
    option to apply. A file that does not parse is returned unchanged with
    no findings: loading it reports the error.
    """
    original = file.path.read_text(encoding="utf-8")
    sets = tuple(sets) if sets is not None else (ww_migrations(file.kind),)
    match file.kind:
        case "settings":
            text, findings = _migrate_json(
                original, file.label, sets, extensions or {}, choices or {}
            )
        case "rules":
            text, findings = _migrate_frontmatter(
                original, file.label, sets, choices or {}
            )
        case _:
            text, findings = _migrate_yaml(original, file.label, sets, choices or {})
    return FileMigration(file, original, text, findings)


def migrate_files(
    files: Iterable[ConfigFile],
    *,
    extensions: Mapping[str, MigrationSet] | None = None,
    choices: Mapping[str, str] | None = None,
) -> tuple[FileMigration, ...]:
    """Every file of ``files`` migrated with ww's own migrations for its kind."""
    return tuple(
        migrate_file(file, extensions=extensions, choices=choices) for file in files
    )


def write_files(migrations: Iterable[FileMigration]) -> tuple[Path, ...]:
    """Write every changed file together, or none of them; their paths."""
    changed = [migration for migration in migrations if migration.changed]
    with Transaction() as transaction:
        transaction.apply(migration.write() for migration in changed)
    return tuple(migration.file.path for migration in changed)


# ----- YAML ----------------------------------------------------------------


def _migrate_yaml(
    text: str,
    label: str,
    sets: Sequence[MigrationSet],
    choices: Mapping[str, str],
    offset: int = 0,
) -> tuple[str, tuple[Finding, ...]]:
    findings: list[Finding] = []
    for migration_set in sets:
        for migration in migration_set.migrations:
            status = migration.status(migration_set.version)
            if status == "expired":
                continue
            try:
                source = YamlText(text, offset)
                # A file of several documents is found in its first, and its
                # edits are refused: ww reads only single documents.
                document = next(yaml.safe_load_all(text), None)
            except yaml.YAMLError:
                return text, tuple(findings)
            locations = migration.find(document)
            if not locations:
                continue
            report = _Report(
                label, migration, status, migration_set.owner, choices.get(migration.id)
            )
            changes = _changes(migration, choices)
            if changes is None:
                findings.extend(
                    sorted(
                        (
                            report.finding(location, source.line_of(location))
                            for location in locations
                        ),
                        key=_by_line,
                    )
                )
                continue
            text, found = _edit_yaml(text, offset, document, changes, report)
            findings.extend(sorted(found, key=_by_line))
    return text, tuple(findings)


def _edit_yaml(
    text: str,
    offset: int,
    document: Any,
    changes: tuple[Change, ...],
    report: _Report,
) -> tuple[str, list[Finding]]:
    """``text`` with ``changes`` made where they can be; the findings."""
    original = YamlText(text, offset)
    locations = report.migration.find(document)
    try:
        report.migration.transform(document, report.choice)
    except MigrationConflictError as conflict:
        return text, [
            report.manual(location, original.line_of(location), str(conflict))
            for location in locations
        ]
    edited = text
    applied: list[tuple[Change, Location]] = []
    refused: dict[Location, tuple[Change, EditRefusedError]] = {}
    for change in changes:
        for location in change.locations(next(yaml.safe_load_all(edited), None)):
            try:
                candidate = apply_edits(
                    edited, change.edit(YamlText(edited, offset), location)
                )
                yaml.safe_load(candidate)
            except EditRefusedError as refusal:
                refused.setdefault(location, (change, refusal))
                continue
            except yaml.YAMLError:
                unparsable = EditRefusedError("the edit would not parse as YAML")
                refused.setdefault(location, (change, unparsable))
                continue
            edited = candidate
            applied.append((change, location))
    expected = copy.deepcopy(document)
    for change, location in applied:
        change.apply(expected, location)
    if applied and yaml.safe_load(edited) != expected:
        reason = "the edited file would not read as the migrated configuration"
        return text, [
            report.manual(location, original.line_of(location), reason)
            for location in locations
        ]
    found = []
    for location in locations:
        line = original.line_of(location)
        if location in refused:
            change, blocked = refused[location]
            found.append(
                report.manual(
                    location,
                    line or blocked.line,
                    blocked.reason,
                    change.instruction(location),
                )
            )
        else:
            found.append(report.finding(location, line, fixed=True))
    return edited, found


def _migrate_frontmatter(
    text: str,
    label: str,
    sets: Sequence[MigrationSet],
    choices: Mapping[str, str],
) -> tuple[str, tuple[Finding, ...]]:
    """A rule file with its YAML frontmatter migrated; the body stays."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text, ()
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            frontmatter = "".join(lines[1:index])
            migrated, findings = _migrate_yaml(
                frontmatter, label, sets, choices, offset=1
            )
            return "".join([lines[0], migrated, *lines[index:]]), findings
    return text, ()


# ----- JSON ----------------------------------------------------------------


def _migrate_json(
    text: str,
    label: str,
    sets: Sequence[MigrationSet],
    extensions: Mapping[str, MigrationSet],
    choices: Mapping[str, str],
) -> tuple[str, tuple[Finding, ...]]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return text, ()
    if not isinstance(document, dict):
        return text, ()
    try:
        source: YamlText | None = YamlText(text)
    except yaml.YAMLError:
        source = None
    findings: list[Finding] = []
    changed = False
    for migration_set in (*sets, *_extension_sets(document, extensions)):
        for migration in migration_set.migrations:
            status = migration.status(migration_set.version)
            if status == "expired":
                continue
            locations = migration.find(document)
            if not locations:
                continue
            report = _Report(
                label, migration, status, migration_set.owner, choices.get(migration.id)
            )
            changes = _changes(migration, choices)
            if changes is None:
                findings.extend(
                    report.finding(location, _line(source, location))
                    for location in locations
                )
                continue
            try:
                document = migration.transform(document, report.choice)
            except MigrationConflictError as conflict:
                findings.extend(
                    report.manual(location, _line(source, location), str(conflict))
                    for location in locations
                )
                continue
            changed = True
            findings.extend(
                report.finding(location, _line(source, location), fixed=True)
                for location in locations
            )
    if not changed:
        return text, tuple(findings)
    return json.dumps(document, indent=2) + "\n", tuple(findings)


def _line(source: YamlText | None, location: Location) -> int | None:
    """The line of ``location`` in a JSON file read as YAML, when it reads."""
    return source.line_of(location) if source is not None else None


def _extension_sets(
    document: dict[str, Any], extensions: Mapping[str, MigrationSet]
) -> tuple[MigrationSet, ...]:
    """Each extension's migrations, placed under the section that names it.

    A section is named by the extension's identifier, ``vendor/name``, or by
    its bare name when no other extension with migrations shares it.
    """
    sections = document.get(EXTENSIONS_KEY)
    if not isinstance(sections, dict):
        return ()
    found: list[MigrationSet] = []
    for key, section in sections.items():
        if not isinstance(section, dict):
            continue
        matches = [
            identifier
            for identifier in extensions
            if key == identifier or ("/" not in key and identifier.endswith(f"/{key}"))
        ]
        if len(matches) != 1:
            continue
        migration_set = extensions[matches[0]]
        found.append(
            replace(
                migration_set,
                migrations=tuple(
                    migration.under((EXTENSIONS_KEY, key))
                    for migration in migration_set.migrations
                ),
            )
        )
    return tuple(found)


# ----- findings ------------------------------------------------------------


def _by_line(finding: Finding) -> int:
    return finding.line or 0


def _changes(
    migration: ConfigMigration, choices: Mapping[str, str]
) -> tuple[Change, ...] | None:
    """The changes to make, or ``None`` when ww makes none.

    A manual migration makes none, and a choice none until it is chosen.
    """
    match migration.kind:
        case "automatic":
            return migration.changes
        case "choice" if migration.id in choices:
            return migration.changes_for(choices[migration.id])
        case _:
            return None


@dataclass(frozen=True)
class _Report:
    """How one migration's findings in one file are written."""

    label: str
    migration: ConfigMigration
    status: Status
    owner: str
    # The option chosen for a choice migration.
    choice: str | None = None

    def finding(
        self, location: Location, line: int | None, *, fixed: bool = False
    ) -> Finding:
        return Finding(
            self.label,
            line,
            self.migration.id,
            "removed" if self.status == "removed" else self.migration.kind,
            render(location),
            self.migration.message(self.status, location, self.owner),
            fixed,
        )

    def manual(
        self,
        location: Location,
        line: int | None,
        reason: str,
        instruction: str | None = None,
    ) -> Finding:
        """A finding ww could not fix: why, and what to change by hand.

        ``instruction`` is the change refused there; by default every
        change of the migration.
        """
        where = f" at line {line}" if line is not None else ""
        instruction = instruction or self.migration.instructions(location)
        return Finding(
            self.label,
            line,
            self.migration.id,
            "removed" if self.status == "removed" else "manual",
            render(location),
            f"{self.migration.why}; ww cannot change it itself ({reason}): "
            f"{instruction}{where} by hand. See {self.migration.reference}.",
        )
