# SPDX-License-Identifier: GPL-3.0-or-later
"""Surgical edits of YAML text, located through PyYAML's node marks.

A configuration file is composed into nodes (``yaml.compose``), whose start
and end marks give each key's and value's exact place in the text. A change
replaces only those characters, or whole lines it removes or adds, so every
comment, blank line, quote and indent elsewhere stays byte for byte.

Some places cannot be edited that way without guessing, and are refused with
a :class:`EditRefusedError` naming the line, for the operator to change by hand: a
flow collection (``{...}``, ``[...]``) at the edit, an anchor or alias on the
way to it or inside what changes, a merge key (``<<``), a block scalar
(``|``, ``>``) that would move or be replaced, and a file with more than one
YAML document. Whatever is edited is then checked by the caller, which
parses the result again and compares it with the in-memory migration.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from .paths import Location, render


class EditRefusedError(Exception):
    """A change ww cannot make to the text by itself; the operator makes it."""

    def __init__(self, reason: str, line: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.line = line


@dataclass(frozen=True)
class TextEdit:
    """Replace ``text[start:end]`` with ``replacement``."""

    start: int
    end: int
    replacement: str


@dataclass(frozen=True)
class Entry:
    """One key of a block mapping and its value, as nodes."""

    parent: yaml.MappingNode
    key: yaml.ScalarNode
    value: yaml.Node


class YamlText:
    """One YAML document's text and its nodes, for locating edits.

    ``offset`` is how many lines precede the document in its file, as the
    frontmatter of a rule file has its ``---`` line, so line numbers are the
    file's.
    """

    def __init__(self, text: str, offset: int = 0) -> None:
        self.text = text
        self.offset = offset
        documents = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
        self.several = len(documents) > 1
        self.root: yaml.Node | None = documents[0] if documents else None
        # Where an anchor is declared or an alias used; a node that carries an
        # anchor starts at that mark.
        self.anchored: set[int] = set()
        self.aliases: list[int] = []
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                self.aliases.append(event.start_mark.index)
            elif getattr(event, "anchor", None) is not None:
                self.anchored.add(event.start_mark.index)

    def line_of(self, location: Location) -> int | None:
        """The 1-based line of ``location``'s key, or ``None`` if not found."""
        try:
            parent, key, node = self._walk(location, check=False)
        except EditRefusedError:
            return None
        return self.line(key if key is not None else node)

    def entry(self, location: Location, *, check: bool = True) -> Entry:
        """The block mapping entry of the key ``location`` ends with.

        With ``check``, a place that cannot be edited safely is refused: an
        anchor or alias on the way, a flow mapping holding the key, a merge
        key in a mapping on the way, or a file of several documents.
        """
        parent, key, node = self._walk(location, check)
        if not isinstance(parent, yaml.MappingNode) or key is None:
            raise EditRefusedError(f"{render(location)} is not a key in the file")
        entry = Entry(parent, key, node)
        if check:
            if parent.flow_style:
                raise EditRefusedError(
                    f"{render(location)} is in a flow mapping", self.line(key)
                )
            self._check_plain(node, location)
        return entry

    def node(self, location: Location, *, check: bool = True) -> yaml.Node:
        """The node at ``location``: the root, a key's value or a list item."""
        node = self._walk(location, check)[2]
        if check:
            self._check_plain(node, location)
        return node

    def _walk(
        self, location: Location, check: bool
    ) -> tuple[yaml.Node | None, yaml.ScalarNode | None, yaml.Node]:
        """The container, key (for a mapping) and node at ``location``."""
        if self.several and check:
            raise EditRefusedError("the file holds more than one YAML document")
        missing = EditRefusedError(f"{render(location)} is not in the file")
        node = self.root
        if node is None:
            raise missing
        parent: yaml.Node | None = None
        key: yaml.ScalarNode | None = None
        for step in location:
            if check:
                self._check_plain(node, location)
            parent = node
            if isinstance(step, int):
                if not isinstance(node, yaml.SequenceNode) or step >= len(node.value):
                    raise missing
                key, node = None, node.value[step]
                continue
            if not isinstance(node, yaml.MappingNode):
                raise missing
            found = next(
                (
                    (key_node, value_node)
                    for key_node, value_node in node.value
                    if isinstance(key_node, yaml.ScalarNode)
                    and key_node.value == str(step)
                ),
                None,
            )
            if found is None:
                raise missing
            key, node = found
        return parent, key, node

    def line(self, node: yaml.Node) -> int:
        """The 1-based line in the file where ``node`` starts."""
        return int(node.start_mark.line) + 1 + self.offset

    def _check_plain(self, node: yaml.Node, location: Location) -> None:
        """Refuse a node that carries an anchor, or a mapping with a merge key."""
        if node.start_mark.index in self.anchored:
            raise EditRefusedError(
                f"{render(location)} is reached through an anchor or alias",
                self.line(node),
            )
        if isinstance(node, yaml.MappingNode) and any(
            key.tag == "tag:yaml.org,2002:merge" for key, _ in node.value
        ):
            raise EditRefusedError(
                f"{render(location)} is in a mapping with a merge key (<<)",
                self.line(node),
            )

    def check_span(self, start: int, end: int, location: Location, line: int) -> None:
        """Refuse an edit whose text declares an anchor or uses an alias."""
        if any(start <= index < end for index in (*self.anchored, *self.aliases)):
            raise EditRefusedError(
                f"{render(location)} holds an anchor or alias in what changes", line
            )

    # ----- spans ---------------------------------------------------------

    def content_end(self, node: yaml.Node) -> int:
        """Where ``node``'s own text ends, before trailing comments and blanks.

        A block collection's end mark lies past the comments and blank lines
        after it; its content ends with its last value's.
        """
        if isinstance(node, yaml.ScalarNode) or getattr(node, "flow_style", False):
            return int(node.end_mark.index)
        if isinstance(node, yaml.MappingNode):
            if not node.value:
                return int(node.end_mark.index)
            key, value = node.value[-1]
            return max(int(key.end_mark.index), self.content_end(value))
        if isinstance(node, yaml.SequenceNode) and node.value:
            return self.content_end(node.value[-1])
        return int(node.end_mark.index)

    def line_start(self, index: int) -> int:
        return self.text.rfind("\n", 0, index) + 1

    def line_end(self, index: int) -> int:
        """The index just past the newline ending the line ``index`` is on."""
        end = self.text.find("\n", index)
        return len(self.text) if end == -1 else end + 1

    def starts_line(self, node: yaml.Node) -> bool:
        """Whether only indentation precedes ``node`` on its line."""
        index = int(node.start_mark.index)
        return not self.text[self.line_start(index) : index].strip()

    def entry_span(self, entry: Entry, location: Location) -> tuple[int, int]:
        """The text of one entry of a block mapping, to remove it whole.

        From the start of the key's line to the end of the line its value
        ends on, inline comment included. A key that follows a list item's
        ``- `` keeps the dash: its entry runs to the next key instead, which
        takes its place on that line.
        """
        if isinstance(entry.value, yaml.ScalarNode) and entry.value.style in {
            "|",
            ">",
        }:
            raise EditRefusedError(
                f"{render(location)} has a block scalar value", self.line(entry.key)
            )
        if len(entry.parent.value) == 1:
            raise EditRefusedError(
                f"{render(location)} is the only key of its mapping",
                self.line(entry.key),
            )
        end = self.line_end(self.content_end(entry.value))
        if self.starts_line(entry.key):
            start = self.line_start(int(entry.key.start_mark.index))
        else:
            keys = [key for key, _ in entry.parent.value]
            position = keys.index(entry.key)
            if position + 1 >= len(keys):
                raise EditRefusedError(
                    f"{render(location)} shares its line with a list item's dash",
                    self.line(entry.key),
                )
            following = keys[position + 1]
            start = int(entry.key.start_mark.index)
            between = self.text[end : following.start_mark.index]
            if between.strip():
                raise EditRefusedError(
                    f"{render(location)} has comments before the next key",
                    self.line(entry.key),
                )
            end = int(following.start_mark.index)
        self.check_span(start, end, location, self.line(entry.key))
        return start, end


def apply_edits(text: str, edits: list[TextEdit]) -> str:
    """``text`` with non-overlapping ``edits`` made, latest first."""
    for edit in sorted(edits, key=lambda edit: (edit.start, edit.end), reverse=True):
        text = text[: edit.start] + edit.replacement + text[edit.end :]
    return text


def scalar_text(value: object, style: str | None) -> str:
    """``value`` written as one YAML scalar, in the quote ``style`` if any."""
    if isinstance(value, str) and style == "'":
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, str) and style == '"':
        dumped = str(yaml.safe_dump(value, default_style='"', width=10**6))
        return dumped.removesuffix("\n")
    dumped = str(yaml.safe_dump(value, default_flow_style=True, width=10**6))
    return dumped.removesuffix("\n...\n").removesuffix("\n")
