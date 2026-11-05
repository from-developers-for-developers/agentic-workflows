# SPDX-License-Identifier: GPL-3.0-or-later
"""Where in a configuration file a migration looks: key paths and locations.

A key path is written with dots between mapping keys, the way messages name
configuration, and has four wildcards:

- ``*`` is every key of a mapping;
- ``[*]`` is every item of a list, e.g. ``workflows[*].steps[*]``;
- ``**`` is any depth, none included: ``**.assess.question`` finds every
  ``assess`` mapping holding ``question``, wherever steps nest (in steps,
  hooks, handlers, items, children or assessment branches);
- ``[key=value]`` keeps only a mapping whose ``key`` is ``value``, e.g.
  ``**[name=assess].question``.

A path always ends with a key, literal or ``*``: the key a change renames,
deletes, moves or sets. Matching a path against a parsed document gives its
*locations*: each the concrete keys and list indexes from the root to one key
that is there, such as ``("workflows", 0, "steps", 2, "assess", "question")``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

# One concrete step from a document's root: a mapping key or a list index.
Location = tuple[str | int, ...]

# The tokens of a key path: the dot between segments, the wildcards, a filter
# ``[key=value]``, and a plain key.
_TOKEN = re.compile(
    r"(?P<dot>\.)|(?P<deep>\*\*)|(?P<any>\*)|(?P<item>\[\*\])"
    r"|\[(?P<field>[^=\]]+)=(?P<value>[^\]]*)\]|(?P<key>[^.\[\]*]+)"
)


@dataclass(frozen=True)
class Key:
    """A literal mapping key."""

    name: str


@dataclass(frozen=True)
class AnyKey:
    """Every key of a mapping: ``*``."""


@dataclass(frozen=True)
class AnyItem:
    """Every item of a list: ``[*]``."""


@dataclass(frozen=True)
class AnyDepth:
    """Any number of keys and items, none included: ``**``."""


@dataclass(frozen=True)
class Where:
    """Only a mapping whose ``field`` holds ``value``: ``[field=value]``."""

    field: str
    value: str


Segment = Key | AnyKey | AnyItem | AnyDepth | Where


@dataclass(frozen=True)
class KeyPath:
    """A parsed key path; :meth:`parse` reads the dotted notation."""

    segments: tuple[Segment, ...]

    @classmethod
    def parse(cls, text: str) -> KeyPath:
        segments: list[Segment] = []
        position = 0
        while position < len(text):
            token = _TOKEN.match(text, position)
            if token is None:
                raise ValueError(f"invalid key path {text!r} at {position}")
            position = token.end()
            match token.lastgroup:
                case "dot":
                    continue
                case "deep":
                    segments.append(AnyDepth())
                case "any":
                    segments.append(AnyKey())
                case "item":
                    segments.append(AnyItem())
                case "value":
                    segments.append(Where(token["field"], token["value"]))
                case _:
                    segments.append(Key(token["key"]))
        if not segments or not isinstance(segments[-1], Key | AnyKey):
            raise ValueError(f"key path {text!r} must end with a key")
        return cls(tuple(segments))

    def under(self, prefix: tuple[str, ...]) -> KeyPath:
        """This path inside the mapping at the literal keys ``prefix``."""
        return KeyPath((*(Key(name) for name in prefix), *self.segments))

    def find(self, document: Any) -> tuple[Location, ...]:
        """Every location of the path's key in ``document``, in document order."""
        found: dict[Location, None] = {}
        for location in _match(document, self.segments, (), set()):
            found.setdefault(location, None)
        return tuple(found)

    def __str__(self) -> str:
        rendered = ""
        for segment in self.segments:
            match segment:
                case Key(name):
                    rendered += f".{name}"
                case AnyKey():
                    rendered += ".*"
                case AnyDepth():
                    rendered += ".**"
                case AnyItem():
                    rendered += "[*]"
                case Where(field, value):
                    rendered += f"[{field}={value}]"
        return rendered.removeprefix(".")


def _match(
    value: Any, segments: tuple[Segment, ...], at: Location, active: set[int]
) -> Iterator[Location]:
    """The locations below ``value``, reached at ``at``, that ``segments`` match.

    ``active`` holds the containers being walked, so a recursive document an
    anchor builds is not followed forever.
    """
    if id(value) in active:
        return
    segment, rest = segments[0], segments[1:]
    match segment:
        case Key(name):
            if isinstance(value, dict) and name in value:
                if not rest:
                    yield (*at, name)
                else:
                    yield from _descend(value[name], rest, (*at, name), value, active)
        case AnyKey():
            if isinstance(value, dict):
                for key, nested in value.items():
                    if not rest:
                        yield (*at, key)
                    else:
                        yield from _descend(nested, rest, (*at, key), value, active)
        case AnyItem():
            if isinstance(value, list):
                for index, nested in enumerate(value):
                    yield from _descend(nested, rest, (*at, index), value, active)
        case Where(field, expected):
            if isinstance(value, dict) and value.get(field) == expected:
                yield from _match(value, rest, at, active)
        case AnyDepth():
            yield from _match(value, rest, at, active)
            children: list[tuple[str | int, Any]] = (
                list(value.items())
                if isinstance(value, dict)
                else list(enumerate(value))
                if isinstance(value, list)
                else []
            )
            for step, nested in children:
                yield from _descend(nested, segments, (*at, step), value, active)


def _descend(
    value: Any,
    segments: tuple[Segment, ...],
    at: Location,
    container: Any,
    active: set[int],
) -> Iterator[Location]:
    active.add(id(container))
    try:
        yield from _match(value, segments, at, active)
    finally:
        active.discard(id(container))


def render(location: Location) -> str:
    """``location`` as messages write it, e.g. ``workflows[0].steps[2].assess``."""
    rendered = ""
    for step in location:
        rendered += f"[{step}]" if isinstance(step, int) else f".{step}"
    return rendered.removeprefix(".")


def resolve(document: Any, location: Location) -> Any:
    """The value at ``location``; ``KeyError`` when a step is missing."""
    value = document
    for step in location:
        if isinstance(step, int):
            if not isinstance(value, list) or not 0 <= step < len(value):
                raise KeyError(step)
        elif not isinstance(value, dict) or step not in value:
            raise KeyError(step)
        value = value[step]
    return value
