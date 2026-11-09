# SPDX-License-Identifier: GPL-3.0-or-later
"""Pointers from a feature map into the project, and whether they resolve.

A pointer is a project-relative path, a glob (``*``, ``?``, ``[...]`` and
``{a,b}`` alternatives), a directory, or a file path with an anchor after
``#``: a heading of a Markdown file, or a word any other file contains.
Checks read files as text and never parse a language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_GLOB = re.compile(r"[*?\[{]")
_BRACES = re.compile(r"\{([^{}]*)\}")
_MARKDOWN = frozenset({".md", ".markdown"})
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$")


@dataclass(frozen=True)
class Pointer:
    """One place a map points to: ``path`` and an optional ``anchor``."""

    path: str
    anchor: str | None = None

    @classmethod
    def parse(cls, text: str) -> Pointer:
        path, separator, anchor = text.partition("#")
        return cls(path.strip(), anchor.strip() if separator else None)

    @property
    def is_glob(self) -> bool:
        return bool(_GLOB.search(self.path))

    def __str__(self) -> str:
        return self.path if self.anchor is None else f"{self.path}#{self.anchor}"

    def shape_problem(self) -> str | None:
        """What makes this pointer malformed, whatever the project holds."""
        if not self.path:
            return "names no path"
        if self.anchor == "":
            return "has an empty anchor after #"
        pure = PurePosixPath(self.path)
        if pure.is_absolute() or ".." in pure.parts:
            return "must be a path inside the project"
        if self.anchor is not None and (self.is_glob or self.path.endswith("/")):
            return "an anchor needs a single file, not a glob or a directory"
        return None

    def files(self, root: Path) -> tuple[Path, ...]:
        """The existing paths this pointer names under ``root``."""
        if not self.is_glob:
            target = root / self.path
            return (target,) if target.exists() else ()
        found = {
            match for pattern in _expanded(self.path) for match in root.glob(pattern)
        }
        return tuple(sorted(found))


def pointer_problems(pointer: Pointer, root: Path) -> tuple[str, ...]:
    """Why ``pointer`` does not resolve under ``root``; empty when it does."""
    malformed = pointer.shape_problem()
    if malformed is not None:
        return (malformed,)
    found = pointer.files(root)
    if not found:
        return ("matches no file" if pointer.is_glob else "no such file or directory",)
    if pointer.anchor is None:
        return ()
    target = found[0]
    if not target.is_file():
        return ("an anchor needs a file, and this is a directory",)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return (f"cannot be read: {error.strerror or error}",)
    if target.suffix.lower() in _MARKDOWN:
        if pointer.anchor not in _headings(text):
            return (f"has no heading {pointer.anchor!r}",)
    elif not _whole_word(pointer.anchor).search(text):
        return (f"does not contain {pointer.anchor!r}",)
    return ()


def _expanded(pattern: str) -> tuple[str, ...]:
    """``pattern`` with each ``{a,b}`` group expanded, for :meth:`Path.glob`."""
    match = _BRACES.search(pattern)
    if match is None:
        return (pattern,)
    return tuple(
        expanded
        for option in match.group(1).split(",")
        for expanded in _expanded(
            pattern[: match.start()] + option + pattern[match.end() :]
        )
    )


def _headings(text: str) -> frozenset[str]:
    """The text of every ATX heading in a Markdown document."""
    return frozenset(
        found.group(1)
        for line in text.splitlines()
        if (found := _HEADING.match(line)) is not None
    )


def _whole_word(name: str) -> re.Pattern[str]:
    """``name`` not run into a longer identifier on either side."""
    return re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
