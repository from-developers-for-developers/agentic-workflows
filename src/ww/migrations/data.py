# SPDX-License-Identifier: GPL-3.0-or-later
"""Load-time migrations of the data ww persists.

Every file ww persists records its format's schema version and, as
``written_by``, the version of ww that wrote it. A schema version changes
only on a backward-incompatible change: a field removed or renamed, a
changed type or meaning, a removed value of a closed set, or a changed
default (the task state omits defaults on disk, so a new default would
silently change what an old file says). A field added with a default that
keeps the old behaviour never changes it: the decoders read every
defaulted field as optional, so an older file simply lacks it.

When a version does change, the format declared here gains a migration: a
pure function from the mapping at version ``N`` to the mapping at ``N + 1``.
A reader hands the raw mapping to :func:`upgrade` before decoding it, which
applies every migration from the file's version to the current one, so the
decoders and the engine only ever see the current shape; writers always
write the current version. Open tasks are thereby upgraded on their next
save, and closed ones are migrated each time they are read. A file newer than
this build reads, or older than the oldest version a format still migrates,
is refused with an error naming the ww version to use instead.

A migration lives in this package, beside the format it upgrades, and is
registered on that format's :class:`DataFormat` with the version it upgrades
from. Dropping old migrations raises the format's ``oldest``.

The task state is stored as one index document and one document per run
(:mod:`ww.storage_adapters.task_document`), and a run document nests the
run's plan snapshot and execution state, each with its own version. Each is
a format of its own here, versioned independently, because each is written
independently: a commit rewrites only the run documents whose content
changed, so an index can be newer than a run document beside it. The
document formats migrate the compact on-disk mapping before its defaults
are restored, which is where a changed default must be handled; the nested
formats migrate their own mapping after that, when it is decoded. Only the
files carry ``written_by``: a nested record is written with its run
document.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ww import __version__
from ww.validation import is_strict_int

# The key every persisted file records the version of ww that wrote it
# under. It is informational: no digest or comparison includes it.
WRITTEN_BY = "written_by"

FormatName = Literal[
    "task state",
    "run state",
    "plan snapshot",
    "execution state",
    "rule automation store",
    "rule dispute log",
    "rule stats",
    "feedback store",
    "run report",
    "usage state",
]
# One step of a format's chain: the mapping at version N, owned by the
# migration, to the mapping at N + 1. The version key is set by the chain.
Migration = Callable[[dict[str, Any]], dict[str, Any]]


class UnsupportedSchemaError(ValueError):
    """A persisted file at a schema version this build does not read.

    ``newer`` tells a file a later ww wrote from one older than the oldest
    version this build still migrates.
    """

    def __init__(
        self, data_format: DataFormat, version: int, written_by: str | None
    ) -> None:
        self.format = data_format.name
        self.version = version
        self.written_by = written_by
        self.newer = version > data_format.current
        writer = f"ww {written_by}" if written_by else None
        if self.newer and writer is None and data_format.records_writer:
            # Every ww that records its version writes ``written_by`` in each
            # file, so a file at a higher schema without it is from a beta
            # before the schema versions were renumbered, not from a newer
            # ww. A nested record never carries it: its enclosing file was
            # checked first.
            self.newer = False
            message = (
                f"{data_format.name} written by a beta ww from before the schema "
                f"versions were renumbered (schema {version}); finish or reset "
                "it with that ww"
            )
        elif self.newer:
            message = (
                f"{data_format.name} written by {writer or 'a newer ww'} "
                f"(schema {version}); this ww reads up to schema "
                f"{data_format.current}: upgrade ww"
            )
        else:
            message = (
                f"{data_format.name} written by {writer or 'an older ww'} "
                f"(schema {version}); this ww reads schema {data_format.oldest} "
                f"and later: finish or reset it with "
                f"{writer or 'the older ww that wrote it'}"
            )
        super().__init__(message)


@dataclass(frozen=True)
class DataFormat:
    """One persisted format: its current and oldest readable versions.

    ``migrations`` maps each version from ``oldest`` up to, not including,
    ``current`` to the migration that upgrades it by one. ``version_key`` is
    the field the format records its version in. ``records_writer`` is false
    for a record nested in another file, which carries no ``written_by``.
    """

    name: FormatName
    current: int
    oldest: int = 1
    version_key: str = "schema_version"
    migrations: Mapping[int, Migration] = field(default_factory=dict)
    records_writer: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.oldest <= self.current:
            raise ValueError(
                f"{self.name}: the oldest readable version must lie between 1 "
                "and the current one"
            )
        if set(self.migrations) != set(range(self.oldest, self.current)):
            raise ValueError(
                f"{self.name}: a migration must upgrade each version from "
                f"{self.oldest} to {self.current}, one at a time"
            )

    def header(self) -> dict[str, object]:
        """The fields a writer of this format puts first in every file."""
        return {self.version_key: self.current, WRITTEN_BY: __version__}

    def upgrade(self, data: dict[str, Any]) -> dict[str, Any]:
        """``data`` at the current version, upgraded from the one it records.

        A mapping already current is returned as it is; an older one is
        copied before the first migration, so the caller's is never changed.
        """
        version = data.get(self.version_key)
        if not is_strict_int(version):
            raise ValueError(f"unsupported {self.name} schema: {version!r}")
        if not self.oldest <= version <= self.current:
            written_by = data.get(WRITTEN_BY)
            raise UnsupportedSchemaError(
                self, version, written_by if isinstance(written_by, str) else None
            )
        if version == self.current:
            return data
        upgraded = copy.deepcopy(data)
        for source in range(version, self.current):
            upgraded = self.migrations[source](upgraded)
            upgraded[self.version_key] = source + 1
        return upgraded


def _registry(*formats: DataFormat) -> dict[str, DataFormat]:
    return {data_format.name: data_format for data_format in formats}


# Every persisted format and its migrations. Each owning module reads its
# current version from here, so a version cannot change without its chain.
FORMATS: dict[str, DataFormat] = _registry(
    DataFormat("task state", current=1),
    DataFormat("run state", current=1),
    # Nested in a run document, which records the writer for both.
    DataFormat("plan snapshot", current=1, records_writer=False),
    DataFormat("execution state", current=1, records_writer=False),
    DataFormat("rule automation store", current=1),
    DataFormat("rule dispute log", current=1),
    # The rule stats, the feedback store and the run reports have always
    # recorded their version as ``schema``.
    DataFormat("rule stats", current=1, version_key="schema"),
    DataFormat("feedback store", current=1, version_key="schema"),
    DataFormat("run report", current=1, version_key="schema"),
    DataFormat("usage state", current=1, version_key="schema"),
)


def current_version(name: FormatName) -> int:
    """The version a writer of ``name`` writes."""
    return FORMATS[name].current


def header(name: FormatName) -> dict[str, object]:
    """The version and ``written_by`` fields of a new file of ``name``."""
    return FORMATS[name].header()


def upgrade(name: FormatName, data: dict[str, Any]) -> dict[str, Any]:
    """``data``, a file of format ``name``, upgraded to the current version.

    Raises :class:`UnsupportedSchemaError` (a ``ValueError``) for a version
    this build does not read, and ``ValueError`` for one that is not an
    integer.
    """
    return FORMATS[name].upgrade(data)
