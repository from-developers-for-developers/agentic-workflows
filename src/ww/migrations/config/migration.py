# SPDX-License-Identifier: GPL-3.0-or-later
"""One configuration construct that is deprecated or removed, and its fix.

A :class:`ConfigMigration` names the construct (``id``), the version that
deprecated it and the one that removes it, why, where the upgrade guide
explains it, which files it can appear in, how to find it, and how to change
it. It is one of three kinds:

- *automatic*: ``changes`` make the new shape from the old one, in memory at
  load and in the file under ``ww migrate``;
- *choice*: ``options`` each change it differently, with a consequence the
  operator weighs; ``ww migrate`` asks which one. Until then ww applies the
  first option in memory, which must keep the old behaviour;
- *manual*: ``options`` describe what the operator can do, with nothing ww
  can apply; ``detect`` finds the construct. Since the engine reads only the
  current shape, a manual migration suits a construct that still reads
  during its window (one about to change meaning, say) or one already
  removed.

A migration only finds what is still in the old shape, so it is idempotent:
no ledger records what ran.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

from packaging.version import InvalidVersion, Version

from ww import __version__

from .changes import Change
from .paths import KeyPath, Location, render

FileKind = Literal["workflows", "rules", "settings"]
FILE_KINDS: tuple[FileKind, ...] = ("workflows", "rules", "settings")
FindingKind = Literal["automatic", "choice", "manual", "removed"]
# Where a migration stands for one version: still read (``deprecated``),
# refused with a named error (``removed``), or forgotten (``expired``).
Status = Literal["deprecated", "removed", "expired"]
# The upgrade guide ww's own migrations link to, by their anchors: generated
# from :data:`~ww.migrations.config.catalog.CONFIG_MIGRATIONS`, and printed
# whole, or one section with ``#<anchor>``, by this command.
UPGRADE_GUIDE = "ww docs upgrade"


@dataclass(frozen=True)
class Option:
    """One way to resolve a choice or manual migration, and what it means.

    A choice's options carry the ``changes`` ww applies once chosen; a manual
    migration's carry none.
    """

    name: str
    consequence: str
    changes: tuple[Change, ...] = ()


@dataclass(frozen=True)
class ConfigMigration:
    """One deprecated or removed configuration construct.

    ``files`` names the kinds of file it applies to: ``workflows`` (``ww.yaml``,
    its imports, ``ww.local.yaml`` and the user's ``ww.yaml``), ``rules`` (a
    rule file's frontmatter) and ``settings`` (``ww.json``, ``ww.local.json``,
    the user's and a project's). ``anchor`` is the section of the upgrade
    ``guide`` that explains it; an extension names its own guide.
    ``removed_in`` is ``None`` while no removal is scheduled.

    ``title``, ``notes`` and ``example`` only feed the upgrade guide: its
    section's heading (by default the ID), any guidance beyond ``why``, and
    a YAML or JSON excerpt before and after the change.
    """

    id: str
    deprecated_in: str
    why: str
    anchor: str
    files: tuple[FileKind, ...]
    changes: tuple[Change, ...] = ()
    options: tuple[Option, ...] = ()
    # Where the construct is, for a choice or manual migration; an automatic
    # one finds it with its changes.
    detect: tuple[KeyPath, ...] = field(default=())
    removed_in: str | None = None
    guide: str = UPGRADE_GUIDE
    title: str = ""
    notes: str = ""
    example: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "detect",
            tuple(
                KeyPath.parse(path) if isinstance(path, str) else path
                for path in self.detect
            ),
        )
        object.__setattr__(self, "files", tuple(self.files))
        if not self.files or not set(self.files) <= set(FILE_KINDS):
            raise ValueError(
                f"migration {self.id!r} must apply to some of: {', '.join(FILE_KINDS)}"
            )
        for version in (self.deprecated_in, self.removed_in):
            if version is not None:
                Version(version)
        if self.changes and self.options:
            raise ValueError(f"migration {self.id!r} has changes or options, not both")
        if not self.changes and not self.options:
            raise ValueError(f"migration {self.id!r} needs changes or options")
        chosen = [bool(option.changes) for option in self.options]
        if any(chosen) and not all(chosen):
            raise ValueError(
                f"migration {self.id!r}: every option of a choice needs changes"
            )
        if not self.changes and not any(chosen) and not self.detect:
            raise ValueError(
                f"migration {self.id!r} needs detect paths to find its construct"
            )

    @property
    def kind(self) -> Literal["automatic", "choice", "manual"]:
        if self.changes:
            return "automatic"
        return "choice" if self.options[0].changes else "manual"

    @property
    def reference(self) -> str:
        """The upgrade guide's section for this migration."""
        return f"{self.guide}#{self.anchor}"

    def status(self, version: str | None = None) -> Status:
        """Where this migration stands for ``version``, by default ww's own.

        Before ``removed_in`` the old construct is still read; from it on it
        is an error that names the removal, until the next major version
        after the removal, from which the migration is forgotten. A
        prerelease of ``removed_in`` counts as it, since the removal lands in
        its first prerelease. A version that is not a version number, such as
        an extension's ``dev``, counts as before every removal.
        """
        if self.removed_in is None:
            return "deprecated"
        try:
            current = Version(version if version is not None else __version__)
        except InvalidVersion:
            return "deprecated"
        current = Version(current.base_version)
        removed = Version(self.removed_in)
        if current < removed:
            return "deprecated"
        return "removed" if current.major <= removed.major else "expired"

    def find(self, document: Any) -> tuple[Location, ...]:
        """Every location in ``document`` still in the old shape."""
        paths = self.detect
        found: dict[Location, None] = {}
        if paths:
            for path in paths:
                found.update(dict.fromkeys(path.find(document)))
            return tuple(found)
        for change in self.changes_for(None):
            found.update(dict.fromkeys(change.locations(document)))
        return tuple(found)

    def changes_for(self, choice: str | None) -> tuple[Change, ...]:
        """The changes ww makes: its own, or a choice's chosen option's.

        ``choice`` ``None`` takes a choice's first option, as ww does in
        memory. A manual migration has none.
        """
        if self.changes:
            return self.changes
        if choice is None:
            return self.options[0].changes
        for option in self.options:
            if option.name == choice:
                return option.changes
        known = ", ".join(option.name for option in self.options)
        raise ValueError(
            f"migration {self.id!r} has no option {choice!r}; choose one of: {known}"
        )

    def transform(self, document: Any, choice: str | None = None) -> Any:
        """A copy of ``document`` in the new shape; ``document`` stays.

        Raises :class:`~ww.migrations.config.changes.MigrationConflictError` when
        the old and the new shape are both there.
        """
        changed = copy.deepcopy(document)
        for change in self.changes_for(choice):
            for location in change.locations(changed):
                change.apply(changed, location)
        return changed

    def under(self, prefix: tuple[str, ...]) -> ConfigMigration:
        """This migration applied inside the mapping at ``prefix``.

        An extension's settings migrations are declared against its own
        section and placed under ``extensions.<id>`` this way.
        """
        return dataclasses.replace(
            self,
            changes=tuple(change.under(prefix) for change in self.changes),
            options=tuple(
                dataclasses.replace(
                    option,
                    changes=tuple(change.under(prefix) for change in option.changes),
                )
                for option in self.options
            ),
            detect=tuple(path.under(prefix) for path in self.detect),
        )

    # ----- messages --------------------------------------------------------

    def message(self, status: Status, location: Location, owner: str = "ww") -> str:
        """What a finding at ``location`` says, short of where it is."""
        if status == "removed":
            return (
                f"{self.why}; removed in {owner} {self.removed_in}: "
                f"{self._remedy(location)}. See {self.reference}."
            )
        return (
            f"{self.why} (deprecated in {owner} {self.deprecated_in}); "
            f"{self._remedy(location)}. See {self.reference}."
        )

    def _remedy(self, location: Location) -> str:
        match self.kind:
            case "automatic":
                return "run ww migrate to fix"
            case "choice":
                return "run ww migrate to choose: " + self._options()
            case _:
                return f"change {render(location)} by hand: " + self._options()

    def _options(self) -> str:
        return "; ".join(
            f"{option.name} ({option.consequence})" for option in self.options
        )

    def instructions(self, location: Location) -> str:
        """What the operator changes by hand at ``location``, step by step."""
        changes = self.changes_for(None) if self.kind != "manual" else ()
        if not changes:
            return f"change {render(location)} by hand: {self._options()}"
        return "; then ".join(
            dict.fromkeys(change.instruction(location) for change in changes)
        )


@dataclass(frozen=True)
class Finding:
    """One construct a migration found in one file.

    ``line`` is 1-based, ``None`` when ww cannot tell it. ``fixed`` says
    whether ``ww migrate``'s new text for the file already changes it.
    """

    file: str
    line: int | None
    migration: str
    kind: FindingKind
    location: str
    message: str
    fixed: bool = False

    @property
    def where(self) -> str:
        """``<file>:<path>``, with the line when it is known."""
        line = f" (line {self.line})" if self.line is not None else ""
        return f"{self.file}:{self.location}{line}"

    def render(self) -> str:
        return f"{self.where}: {self.message}"
