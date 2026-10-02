# SPDX-License-Identifier: GPL-3.0-or-later
"""File writes that ww makes to configuration, together or not at all.

``ww rules add`` and ``ww setup apply`` plan their changes as
:class:`FileWrite` values and apply them in a :class:`Transaction`, which puts
every file back as it was when anything fails. ``ww rules add`` loads the
configuration after writing; ``ww setup apply`` validates its plan in memory
first and writes only once it is confirmed. The import file each command owns
is added to a root file's ``imports`` with :func:`import_write`, which changes
that one list and refuses when it cannot do so without touching anything else.

A write goes through a symbolic link to the file it names and keeps that
file's permissions, so a configuration file kept elsewhere stays linked.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ww.errors import StateError


@dataclass(frozen=True)
class FileWrite:
    """One file's new content, or ``None`` to delete it."""

    path: Path
    content: str | None


class Transaction:
    """Files written together and restored together when anything fails."""

    def __init__(self) -> None:
        self._saved: list[tuple[Path, bytes | None]] = []
        self._created: list[Path] = []
        self._done = False

    def __enter__(self) -> Transaction:
        return self

    def __exit__(self, kind: object, error: object, trace: object) -> None:
        if error is not None:
            self.roll_back()

    def apply(
        self, writes: Iterable[FileWrite], directories: Iterable[Path] = ()
    ) -> None:
        """Write every change, or put back what was written and fail.

        An ``OSError`` becomes a :class:`StateError` once the files are back.
        """
        try:
            for directory in directories:
                self._make_directory(directory)
            for change in writes:
                # A symbolic link stays one: its target is what changes.
                path = change.path.resolve()
                self._saved.append((path, path.read_bytes() if path.exists() else None))
                if change.content is None:
                    path.unlink()
                else:
                    self._make_directory(path.parent)
                    atomic_write(path, change.content)
        except OSError as error:
            self.roll_back()
            raise StateError(
                f"cannot write {error.filename or 'a file'}: {error.strerror or error}"
                "; nothing was written"
            ) from error

    def _make_directory(self, directory: Path) -> None:
        missing = [
            folder for folder in (directory, *directory.parents) if not folder.exists()
        ]
        for folder in reversed(missing):
            folder.mkdir()
            self._created.append(folder)

    def roll_back(self) -> None:
        if self._done:
            return
        self._done = True
        for path, content in reversed(self._saved):
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        for folder in reversed(self._created):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()


def atomic_write(path: Path, content: str) -> None:
    """Replace the file ``path`` names whole, keeping its permissions.

    A symbolic link is written through to its target. The temporary file is
    removed when the write fails.
    """
    target = path.resolve()
    temporary = target.with_name(f".{target.name}.ww-tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        if target.exists():
            temporary.chmod(target.stat().st_mode & 0o7777)
        temporary.replace(target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


_WIDTH = 80
_ITEM = re.compile(r"  -( |$)")
_LONG_TEXT = 72
_SHORT_LIST = 60


class _Dumper(yaml.SafeDumper):
    """Block style YAML a person would write, without anchors or ``null``."""

    def ignore_aliases(self, data: Any) -> bool:
        return True

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def _represent_none(dumper: _Dumper, _: None) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


def _represent_str(dumper: _Dumper, text: str) -> yaml.ScalarNode:
    style = "|" if "\n" in text else ">" if len(text) > _LONG_TEXT else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", text, style=style)


def _is_short_scalar(item: Any) -> bool:
    if isinstance(item, str):
        return "\n" not in item and len(item) <= _SHORT_LIST
    return isinstance(item, (bool, int, float))


def _represent_list(dumper: _Dumper, items: list[Any]) -> yaml.SequenceNode:
    inline = all(map(_is_short_scalar, items)) and len(str(items)) <= _SHORT_LIST
    return dumper.represent_sequence("tag:yaml.org,2002:seq", items, flow_style=inline)


_Dumper.add_representer(type(None), _represent_none)
_Dumper.add_representer(str, _represent_str)
_Dumper.add_representer(list, _represent_list)


def _dump(value: Any) -> str:
    return yaml.dump(  # type: ignore[no-any-return]
        value,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=_WIDTH,
    )


def _section(key: Any, value: Any) -> str:
    """One top-level key, with a blank line between the items of its list.

    Every other line of a block list's item, block text included, is indented
    deeper than the ``  - `` that starts the item.
    """
    lines = _dump({key: value}).splitlines(True)
    if not isinstance(value, list):
        return "".join(lines)
    return "".join(
        f"\n{line}" if index > 1 and _ITEM.match(line) else line
        for index, line in enumerate(lines)
    )


def dump_yaml(value: dict[str, Any]) -> str:
    """``value`` as readable YAML that loads back to the same data.

    Mappings are written in block style, short scalar lists inline, long text
    folded and multi-line text as a literal block. Top-level sections and the
    items of a top-level list are separated by a blank line.
    """
    return "\n".join(_section(key, item) for key, item in value.items())


def import_write(
    root: Path, text: str, raw: dict[str, Any], entry: str, label: str
) -> FileWrite:
    """``root`` with ``entry`` added to its imports, and nothing else changed."""
    updated = with_import(text, raw, entry)
    imports = list(raw.get("imports") or [])
    expected = {"imports": [*imports, entry]}
    expected.update((key, value) for key, value in raw.items() if key != "imports")
    if yaml.safe_load(updated) != expected:
        raise StateError(
            f"ww cannot add {entry} to the imports of {label} without changing "
            "anything else; add it by hand and run the command again"
        )
    return FileWrite(root, updated)


def with_import(text: str, raw: dict[str, Any], entry: str) -> str:
    """``text`` with ``entry`` appended to its top-level ``imports`` list.

    Without ``imports``, the list goes before the first top-level key, since
    imports come before every key but ``extends``. An existing list gains one
    line in block style, or one element in a one-line flow list.
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    if "imports" not in raw:
        index = next(
            (
                position
                for position, line in enumerate(lines)
                if line.strip() and not line.startswith(("#", "---", "%", " ", "\t"))
            ),
            len(lines),
        )
        return "".join([*lines[:index], f"imports:\n  - {entry}\n", *lines[index:]])
    start = next(
        (
            position
            for position, line in enumerate(lines)
            # The top-level imports key, e.g. "imports:" or "imports :".
            if re.match(r"imports\s*:", line)
        ),
        None,
    )
    if start is None:
        return text
    value = lines[start].split(":", 1)[1].split(" #", 1)[0].strip()
    if value.startswith("[") and value.endswith("]"):
        line = lines[start]
        close = line.rindex("]")
        separator = ", " if value[1:-1].strip() else ""
        lines[start] = f"{line[:close]}{separator}{entry}{line[close:]}"
        return "".join(lines)
    if value:
        return text
    last = None
    prefix = "  - "
    for position in range(start + 1, len(lines)):
        line = lines[position]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and (line[0] in " \t-"):
            last = position
            prefix = line[: len(line) - len(line.lstrip())] + "- "
            continue
        break
    if last is None:
        return text
    return "".join([*lines[: last + 1], f"{prefix}{entry}\n", *lines[last + 1 :]])
