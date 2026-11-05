# SPDX-License-Identifier: GPL-3.0-or-later
"""The changes a configuration migration is made of.

Each change is declared once, with a key path, and gives both of its forms:
:meth:`Change.apply` changes a parsed document in memory, as ww does when it
loads an old file, and :meth:`Change.edit` the text edits that make the same
change to a YAML file, as ``ww migrate`` does. The four primitives cover
renaming, deleting, moving a key, and replacing a value by a mapping of old
values to new ones; a migration that needs anything else subclasses
:class:`Change` and implements :meth:`~Change.apply` and
:meth:`~Change.instruction`, and :meth:`~Change.edit` when ww can make the
change to the text by itself. A JSON file is rewritten from the changed
document, so it needs only :meth:`~Change.apply`.

Every change is idempotent: it finds only what is still in the old shape, so
running it twice changes nothing the second time.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from .paths import KeyPath, Location, render, resolve
from .yaml_text import EditRefusedError, TextEdit, YamlText, scalar_text


class MigrationConflictError(Exception):
    """A document holds both the old and the new shape, or blocks the change.

    ww cannot tell which one the operator meant; at load this is an error,
    and ``ww migrate`` leaves it for the operator.
    """


@dataclass(frozen=True)
class Change(ABC):
    """One change of a migration, at every location its key path finds.

    ``path`` is a :class:`KeyPath` or its dotted notation.
    """

    path: KeyPath

    def __post_init__(self) -> None:
        if isinstance(self.path, str):
            object.__setattr__(self, "path", KeyPath.parse(self.path))

    def locations(self, document: Any) -> tuple[Location, ...]:
        """Every location in ``document`` still in the old shape."""
        return self.path.find(document)

    @abstractmethod
    def apply(self, document: Any, location: Location) -> None:
        """Change ``document`` in place at ``location``.

        Raises :class:`MigrationConflictError` when the new shape is already there
        too.
        """

    @abstractmethod
    def instruction(self, location: Location) -> str:
        """What the operator does by hand at ``location``, as one sentence."""

    def describe(self) -> str:
        """What the change does wherever its path finds the key, for the guide."""
        return f"change `{self.path}`"

    def edit(self, text: YamlText, location: Location) -> list[TextEdit]:
        """The text edits that make this change at ``location``.

        By default a change cannot edit text, and :class:`EditRefusedError` makes it
        the operator's.
        """
        raise EditRefusedError(
            "ww has no text edit for this change", text.line_of(location)
        )

    def under(self, prefix: tuple[str, ...]) -> Change:
        """This change applied inside the mapping at ``prefix``."""
        return dataclasses.replace(self, path=self.path.under(prefix))


def _parent(document: Any, location: Location) -> dict[Any, Any] | None:
    try:
        parent = resolve(document, location[:-1])
    except KeyError:
        return None
    return parent if isinstance(parent, dict) else None


@dataclass(frozen=True)
class RenameKey(Change):
    """Rename the key ``path`` ends with to ``to``, in place."""

    to: str

    def apply(self, document: Any, location: Location) -> None:
        parent = _parent(document, location)
        old = location[-1]
        if parent is None or old not in parent:
            return
        if self.to in parent:
            raise MigrationConflictError(
                f"cannot combine {self.to} and {old}, which it replaces; remove {old}"
            )
        items = list(parent.items())
        parent.clear()
        parent.update((self.to if key == old else key, value) for key, value in items)

    def instruction(self, location: Location) -> str:
        return f"rename {render(location)} to {self.to}"

    def describe(self) -> str:
        return f"rename `{self.path}` to `{self.to}`"

    def edit(self, text: YamlText, location: Location) -> list[TextEdit]:
        entry = text.entry(location)
        start, end = int(entry.key.start_mark.index), int(entry.key.end_mark.index)
        text.check_span(start, end, location, text.line(entry.key))
        if entry.key.style not in {None, "'", '"'}:
            raise EditRefusedError(
                f"{render(location)} is a block scalar key", text.line(entry.key)
            )
        return [TextEdit(start, end, scalar_text(self.to, entry.key.style))]


@dataclass(frozen=True)
class DeleteKey(Change):
    """Delete the key ``path`` ends with, and its value."""

    def apply(self, document: Any, location: Location) -> None:
        parent = _parent(document, location)
        if parent is not None:
            parent.pop(location[-1], None)

    def instruction(self, location: Location) -> str:
        return f"delete {render(location)}"

    def describe(self) -> str:
        return f"delete `{self.path}`"

    def edit(self, text: YamlText, location: Location) -> list[TextEdit]:
        start, end = text.entry_span(text.entry(location), location)
        return [TextEdit(start, end, "")]


@dataclass(frozen=True)
class SetValue(Change):
    """Replace the value of ``path`` by ``values[old]``; other values stay.

    Only scalar values are mapped: a value ``values`` does not name is not
    in the old shape.
    """

    values: Mapping[Hashable, Any] = field(hash=False)

    def locations(self, document: Any) -> tuple[Location, ...]:
        return tuple(
            location
            for location in self.path.find(document)
            if self._maps(resolve(document, location))
        )

    def _maps(self, value: Any) -> bool:
        return (
            isinstance(value, Hashable)
            and not isinstance(value, tuple)
            and any(value == old and type(value) is type(old) for old in self.values)
        )

    def apply(self, document: Any, location: Location) -> None:
        parent = _parent(document, location)
        key = location[-1]
        if parent is not None and key in parent and self._maps(parent[key]):
            parent[key] = self.values[parent[key]]

    def instruction(self, location: Location) -> str:
        changes = ", ".join(f"{old!r} to {new!r}" for old, new in self.values.items())
        return f"change the value of {render(location)} from {changes}"

    def describe(self) -> str:
        changes = ", ".join(
            f"`{old!r}` to `{new!r}`" for old, new in self.values.items()
        )
        return f"change the value of `{self.path}` from {changes}"

    def edit(self, text: YamlText, location: Location) -> list[TextEdit]:
        entry = text.entry(location)
        value = entry.value
        line = text.line(entry.key)
        if not isinstance(value, yaml.ScalarNode) or value.style in {"|", ">"}:
            raise EditRefusedError(f"{render(location)} is not a one-line scalar", line)
        start, end = int(value.start_mark.index), int(value.end_mark.index)
        text.check_span(start, end, location, line)
        new = self.values[yaml.safe_load(text.text[start:end])]
        style = value.style if isinstance(new, str) else None
        return [TextEdit(start, end, scalar_text(new, style))]


@dataclass(frozen=True)
class MoveKey(Change):
    """Move the key ``path`` ends with, and its value, to ``to``.

    ``to`` is a dotted path of literal keys from the mapping holding the key,
    after going ``up`` levels out of it (``up=1`` is the mapping that holds
    that mapping). Missing mappings on the way are created. ``to``'s last key
    is the key's new name.
    """

    to: str
    up: int = 0

    def _destination(self, location: Location) -> tuple[Location, tuple[str, ...]]:
        base = location[:-1]
        for _ in range(self.up):
            if not base:
                raise MigrationConflictError(
                    f"{render(location)} cannot move up that far"
                )
            base = base[:-1]
        return base, tuple(self.to.split("."))

    def apply(self, document: Any, location: Location) -> None:
        parent = _parent(document, location)
        key = location[-1]
        if parent is None or key not in parent:
            return
        base, keys = self._destination(location)
        target = resolve(document, base)
        for name in keys[:-1]:
            if not isinstance(target, dict):
                break
            target = target.setdefault(name, {})
        if not isinstance(target, dict):
            raise MigrationConflictError(
                f"cannot move {render(location)} to {self.to}: "
                f"{'.'.join(keys[:-1])} is not a mapping"
            )
        if keys[-1] in target:
            raise MigrationConflictError(
                f"cannot combine {render((*base, *keys))} and "
                f"{render(location)}, which it replaces; remove {key}"
            )
        target[keys[-1]] = parent.pop(key)

    def instruction(self, location: Location) -> str:
        base, keys = self._destination(location)
        return f"move {render(location)} to {render((*base, *keys))}"

    def describe(self) -> str:
        where = (
            f"{self.up} mapping{'s' if self.up != 1 else ''} out from the one "
            "holding it"
            if self.up
            else "in the mapping holding it"
        )
        return f"move `{self.path}` to `{self.to}`, {where}"

    def edit(self, text: YamlText, location: Location) -> list[TextEdit]:
        entry = text.entry(location)
        line = text.line(entry.key)
        removed = text.entry_span(entry, location)
        key_end = int(entry.key.end_mark.index)
        content_end = text.content_end(entry.value)
        first_end = text.line_end(key_end)
        value_line = entry.value.start_mark.line
        if value_line == entry.key.start_mark.line:
            if content_end > first_end:
                raise EditRefusedError(f"{render(location)} spans several lines", line)
            body: list[str] = []
        else:
            body = text.text[first_end : text.line_end(content_end)].splitlines(True)
        base, keys = self._destination(location)
        mapping, column, missing = self._insertion_point(text, base, keys, location)
        indent = column + 2 * len(missing)
        shift = indent - int(entry.key.start_mark.column)
        moved = [
            f"{' ' * (column + 2 * depth)}{name}:\n"
            for depth, name in enumerate(missing)
        ]
        moved.append(f"{' ' * indent}{keys[-1]}{text.text[key_end:first_end]}")
        for content in body:
            if not content.strip():
                moved.append(content)
            elif shift >= 0:
                moved.append(" " * shift + content)
            elif content[:-shift].strip():
                raise EditRefusedError(
                    f"{render(location)} cannot be re-indented", line
                )
            else:
                moved.append(content[-shift:])
        insertion = text.line_end(text.content_end(mapping))
        block = "".join(moved)
        if not block.endswith("\n"):
            block += "\n"
        if insertion and not text.text[:insertion].endswith("\n"):
            block = "\n" + block
        return [TextEdit(*removed, ""), TextEdit(insertion, insertion, block)]

    def _insertion_point(
        self,
        text: YamlText,
        base: Location,
        keys: tuple[str, ...],
        location: Location,
    ) -> tuple[yaml.MappingNode, int, tuple[str, ...]]:
        """The deepest block mapping of the destination that exists.

        Returns it, its keys' column, and the keys still to be created.
        """
        node: yaml.Node = text.node(base)
        line = text.line_of(location)
        for depth, name in enumerate(keys[:-1]):
            if not isinstance(node, yaml.MappingNode) or node.flow_style:
                raise EditRefusedError(
                    f"{render(location)} moves into something not a block mapping",
                    line,
                )
            nested = next(
                (value for key, value in node.value if key.value == name), None
            )
            if nested is None:
                return node, _column(node), keys[depth:-1]
            node = nested
        if not isinstance(node, yaml.MappingNode) or node.flow_style or not node.value:
            raise EditRefusedError(
                f"{render(location)} moves into something not a block mapping", line
            )
        return node, _column(node), ()


def _column(mapping: yaml.MappingNode) -> int:
    return int(mapping.value[0][0].start_mark.column) if mapping.value else 0


def rename_key(path: str, to: str) -> RenameKey:
    """Rename the key ``path`` ends with to ``to``."""
    return RenameKey(KeyPath.parse(path), to)


def delete_key(path: str) -> DeleteKey:
    """Delete the key ``path`` ends with."""
    return DeleteKey(KeyPath.parse(path))


def set_value(path: str, values: Mapping[Hashable, Any]) -> SetValue:
    """Replace each old value of ``path`` that ``values`` names by its new one."""
    return SetValue(KeyPath.parse(path), dict(values))


def move_key(path: str, to: str, *, up: int = 0) -> MoveKey:
    """Move the key ``path`` ends with to ``to``, ``up`` levels out first."""
    return MoveKey(KeyPath.parse(path), to, up)
