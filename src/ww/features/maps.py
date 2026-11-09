# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading ``.ww/features/<name>.yaml`` and checking each map's shape.

Loading never stops at the first mistake: every map that reads is kept, and
every problem is collected, so ``ww features check`` reports them all at once.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ww.config_files import FEATURES_DIRECTORY

from .pointers import Pointer, pointer_problems

# The most entries one map holds: past it, the feature should be split.
MAX_ENTRIES = 25
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_POINTER_FIELDS = ("parts", "touches", "tests", "docs")
_TEXT_FIELDS = ("aliases", "reads", "uses")
_FIELDS = frozenset({"name", "summary", "entry", *_POINTER_FIELDS, *_TEXT_FIELDS})

TEMPLATE = """\
# A feature map: where one feature lives, one line per place. Not
# documentation: why the code is shaped this way stays in the docs.
# A pointer is a path, a directory, a glob ({a,b} alternatives too), or
# path#Name (a word the file contains) or file.md#Heading.
name: example                      # the file is .ww/features/example.yaml
summary: One line on what the feature does.
aliases: [other names, a task might use]
entry: src/package/example.py      # where to start reading
parts:                             # the feature's own code
  - src/package/example/
  - src/package/registry.py#ExampleRegistry
touches:                           # code elsewhere the feature relies on or changes
  - src/package/cli.py#example
reads: [state files, settings keys]  # data shared with other features (free text)
uses: [other-map]                  # other feature maps, by name
tests: [tests/test_example.py]
docs:
  - documentation/architecture.md#Example
"""


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a map: where, and what."""

    file: str
    message: str
    map: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"map": self.map, "file": self.file, "message": self.message}

    def __str__(self) -> str:
        return f"{self.file}: {self.message}"


@dataclass(frozen=True)
class FeatureMap:
    """One feature's map, as its file states it."""

    name: str
    summary: str
    file: Path
    aliases: tuple[str, ...] = ()
    entry: Pointer | None = None
    parts: tuple[Pointer, ...] = ()
    touches: tuple[Pointer, ...] = ()
    reads: tuple[str, ...] = ()
    uses: tuple[str, ...] = ()
    tests: tuple[Pointer, ...] = ()
    docs: tuple[Pointer, ...] = ()

    def pointers(self) -> Iterator[tuple[str, Pointer]]:
        """Every pointer with the field it stands under, in file order."""
        if self.entry is not None:
            yield "entry", self.entry
        for name in _POINTER_FIELDS:
            for index, pointer in enumerate(getattr(self, name)):
                yield f"{name}[{index}]", pointer

    def covers(self, path: str) -> bool:
        """Whether any pointer covers the project-relative ``path``."""
        return any(pointer.covers(path) for _, pointer in self.pointers())

    def names(self) -> tuple[str, ...]:
        """The name and every alias, as a lookup matches them."""
        return (self.name, *self.aliases)

    @property
    def entries(self) -> int:
        """How many lines the map holds, against :data:`MAX_ENTRIES`."""
        return sum(1 for _ in self.pointers()) + len(self.reads) + len(self.uses)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "aliases": list(self.aliases),
            "entry": str(self.entry) if self.entry is not None else None,
            **{
                name: [str(pointer) for pointer in getattr(self, name)]
                for name in _POINTER_FIELDS
            },
            "reads": list(self.reads),
            "uses": list(self.uses),
        }


@dataclass(frozen=True)
class Coverage:
    """What a set of changed files means for the maps.

    ``affected`` are the maps a pointer of which covers a changed file;
    ``unmapped`` are the changed files no map covers. The maps' own files
    are neither.
    """

    affected: tuple[FeatureMap, ...] = ()
    unmapped: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeatureMaps:
    """A project's maps that read, and every problem found reading them."""

    root: Path
    maps: tuple[FeatureMap, ...] = ()
    problems: tuple[Problem, ...] = ()

    def get(self, name: str) -> FeatureMap | None:
        return next((found for found in self.maps if found.name == name), None)

    def pointer_problems(self, feature: FeatureMap) -> tuple[Problem, ...]:
        """The pointers of ``feature`` that no longer resolve."""
        file = _relative(feature.file, self.root)
        return tuple(
            Problem(file, f"{where} {pointer}: {message}", feature.name)
            for where, pointer in feature.pointers()
            for message in pointer_problems(pointer, self.root)
        )

    def coverage(self, paths: Iterable[str]) -> Coverage:
        """Which maps ``paths`` touch, and which of them no map covers."""
        changed = [path for path in paths if not _is_map_file(path)]
        return Coverage(
            tuple(
                feature
                for feature in self.maps
                if any(feature.covers(path) for path in changed)
            ),
            tuple(
                path
                for path in changed
                if not any(feature.covers(path) for feature in self.maps)
            ),
        )

    def lookup(self, query: str) -> tuple[FeatureMap, ...]:
        """The maps ``query`` finds, those it names first.

        A query names a map by its name or an alias, whatever the case. A
        query that is a path, or ``path#Name``, finds the maps a pointer of
        which covers that path, or lies inside it as a directory. Any other
        query finds the maps whose name, aliases and summary hold each of its
        words.
        """
        wanted = query.strip()
        if not wanted:
            return ()
        named = tuple(
            feature
            for feature in self.maps
            if wanted.casefold() in (name.casefold() for name in feature.names())
        )
        if _is_path(wanted):
            pointer = Pointer.parse(wanted)
            found = tuple(
                feature
                for feature in self.maps
                if feature.covers(pointer.path)
                or any(
                    ptr.path.startswith(pointer.path.rstrip("/") + "/")
                    for _, ptr in feature.pointers()
                )
            )
        else:
            words = wanted.casefold().split()
            found = tuple(
                feature
                for feature in self.maps
                if all(
                    re.search(rf"(?<!\w){re.escape(word)}", text)
                    for text in (
                        " ".join((*feature.names(), feature.summary)).casefold(),
                    )
                    for word in words
                )
            )
        return tuple(dict.fromkeys((*named, *found)))

    def check(self) -> tuple[Problem, ...]:
        """Every shape problem, then every pointer that does not resolve."""
        return self.problems + tuple(
            problem
            for feature in self.maps
            for problem in self.pointer_problems(feature)
        )


def load_feature_maps(root: Path) -> FeatureMaps:
    """Read every map under ``root``'s :data:`FEATURES_DIRECTORY`."""
    directory = root / FEATURES_DIRECTORY
    files = sorted(directory.glob("*.yaml")) if directory.is_dir() else []
    maps: list[FeatureMap] = []
    problems: list[Problem] = []
    for path in files:
        feature, found = _read(path, root)
        problems.extend(found)
        if feature is not None:
            maps.append(feature)
    problems.extend(_across(maps, root))
    return FeatureMaps(root, tuple(maps), tuple(problems))


def _read(path: Path, root: Path) -> tuple[FeatureMap | None, list[Problem]]:
    file = _relative(path, root)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return None, [Problem(file, f"cannot be read: {error}")]
    if not isinstance(raw, dict):
        return None, [Problem(file, "must hold a mapping of the map's fields")]
    problems: list[str] = []
    unknown = sorted(str(key) for key in set(raw) - _FIELDS)
    if unknown:
        problems.append(f"unknown field(s): {', '.join(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        problems.append("name must be lowercase letters, digits and dashes")
        name = path.stem
    elif name != path.stem:
        problems.append(f"name {name!r} must match the file name {path.stem!r}")
        # Keyed by its file, the map never shadows the one of that name.
        name = path.stem
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary must be one line of text")
        summary = ""
    elif "\n" in summary.strip():
        problems.append("summary must be one line")
    entry = raw.get("entry")
    if entry is not None and not isinstance(entry, str):
        problems.append("entry must be one pointer")
        entry = None
    lists = {
        key: _strings(raw.get(key), key, problems)
        for key in (*_POINTER_FIELDS, *_TEXT_FIELDS)
    }
    if not lists["parts"]:
        problems.append("parts must list at least one pointer")
    feature = FeatureMap(
        name=name,
        summary=summary.strip(),
        file=path,
        aliases=lists["aliases"],
        entry=Pointer.parse(entry) if entry is not None else None,
        parts=tuple(Pointer.parse(text) for text in lists["parts"]),
        touches=tuple(Pointer.parse(text) for text in lists["touches"]),
        reads=lists["reads"],
        uses=lists["uses"],
        tests=tuple(Pointer.parse(text) for text in lists["tests"]),
        docs=tuple(Pointer.parse(text) for text in lists["docs"]),
    )
    for where, pointer in feature.pointers():
        malformed = pointer.shape_problem()
        if malformed is not None:
            problems.append(f"{where} {pointer}: {malformed}")
    if feature.entries > MAX_ENTRIES:
        problems.append(
            f"holds {feature.entries} entries, more than {MAX_ENTRIES}: split "
            "the feature"
        )
    return feature, [Problem(file, message, feature.name) for message in problems]


def _strings(value: Any, key: str, problems: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(entry, str) and entry.strip() for entry in value
    ):
        problems.append(f"{key} must be a list of non-empty strings")
        return ()
    entries = tuple(entry.strip() for entry in value)
    repeated = sorted({entry for entry in entries if entries.count(entry) > 1})
    if repeated:
        problems.append(f"{key} repeats {', '.join(repeated)}")
    return tuple(dict.fromkeys(entries))


def _across(maps: list[FeatureMap], root: Path) -> list[Problem]:
    """Problems between maps: shared aliases and ``uses`` of missing maps."""
    problems: list[Problem] = []
    names = {feature.name for feature in maps}
    owners: dict[str, str] = {name.lower(): name for name in names}
    for feature in maps:
        file = _relative(feature.file, root)
        for alias in feature.aliases:
            key = alias.lower()
            owner = owners.setdefault(key, feature.name)
            if owner != feature.name:
                problems.append(
                    Problem(
                        file,
                        f"alias {alias!r} also names feature map {owner!r}",
                        feature.name,
                    )
                )
        for used in feature.uses:
            if used not in names:
                problems.append(
                    Problem(
                        file, f"uses {used!r}, which is no feature map", feature.name
                    )
                )
            elif used == feature.name:
                problems.append(Problem(file, "uses itself", feature.name))
    return problems


def _is_map_file(path: str) -> bool:
    return PurePosixPath(path).parts[:2] == FEATURES_DIRECTORY.parts


def _is_path(query: str) -> bool:
    """Whether a lookup query reads as a path rather than words."""
    return " " not in query.split("#", 1)[0] and ("/" in query or "." in query)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)
