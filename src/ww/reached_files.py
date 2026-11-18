# SPDX-License-Identifier: GPL-3.0-or-later
"""The files ww's configuration names, and the files its commands reach.

A command reaches a project file from where ww reads its configuration as
``{{ww.root}}/<path>`` in ``argv``, ``args`` or ``env``, or as
``$WW_ROOT/<path>`` in a ``shell`` source; beside it, a rule file's
``check.files``, a step rule's ``files`` and a rule-automation store check's
``config`` list the files it needs by plain path, such as a script its script
calls. Three readers share this:

- a configuration snapshot copies every committed file the configuration
  names, a rule file's check included (:mod:`ww.config_snapshots`);
- a check whose command reaches a file missing there is unavailable, not
  failed (:mod:`ww.rule_checks`);
- every command warns as it reads the configuration about each file a command
  reaches that is missing there. The scan behind the warning runs once: for a
  snapshot when it is written, kept inside it; for the checkout, kept in
  ``.ww`` until a file it looked at changes, which a few ``stat`` calls tell.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import posixpath
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from ww.config_files import RULE_AUTOMATION_FILE, SETTINGS_FILE, WORKFLOWS_FILE

# The files a scan starts from; everything else is found through them.
ENTRY_FILES = (WORKFLOWS_FILE, SETTINGS_FILE, RULE_AUTOMATION_FILE)
# A path reached from the root in a command, e.g. ``tools/lint.py`` in
# ``python3 "$WW_ROOT/tools/lint.py"`` or ``{{ww.root}}/tools/lint.py``.
ROOT_REFERENCE = re.compile(
    r"(?:\{\{\s*ww\.root\s*\}\}|\$\{?WW_ROOT\}?)/([^\s\"'`;|&()<>]+)"
)
# A reference built at run time, ``$WW_ROOT/$NAME`` or ``{{ww.root}}/*.sh``,
# names no single file to look for.
_DYNAMIC = re.compile(r"[*?\[\]{}$~]")
_STRUCTURED = (".yaml", ".yml", ".json")
_RULE_FILE = ".md"
# A mapping holding one of these is a command: a check, a hook, a handler.
_COMMAND_KEYS = ("argv", "shell")
_COMMAND_TEXT_KEYS = ("argv", "shell", "args", "env")
# Beside a command: the files it needs, by plain path from the root.
_LISTED_KEYS = ("files", "config")
# What a snapshot keeps of its scan, inside it.
SNAPSHOT_RECORD = ".ww-reached-files.json"
# What the checkout's scan is kept in, under its ``.ww``.
CHECKOUT_RECORD = Path(".ww") / "reached-files.json"
_RECORD_VERSION = 1

if TYPE_CHECKING:
    from ww.actions import Commands


@dataclass(frozen=True, order=True)
class Reach:
    """A file a command of the configuration reaches, and which command.

    ``owner`` names the command for the operator, such as ``rule file
    rules/docs/lint.md`` or ``ww-rule-automation.json at checks.lint``.
    """

    owner: str
    file: str

    def to_dict(self) -> dict[str, str]:
        return {"owner": self.owner, "file": self.file}

    @classmethod
    def from_dict(cls, data: Any) -> Reach | None:
        if not isinstance(data, dict):
            return None
        owner, file = data.get("owner"), data.get("file")
        if isinstance(owner, str) and isinstance(file, str):
            return cls(owner, file)
        return None


class ConfigFiles(Protocol):
    """The files a scan reads: a commit's tree, or the checkout."""

    def is_file(self, path: str) -> bool: ...

    def is_dir(self, path: str) -> bool: ...

    def listing(self, directory: str) -> Iterable[str]:
        """The files directly inside ``directory``, by relative path."""
        ...

    def read(self, path: str) -> bytes: ...


@dataclass(frozen=True)
class Scan:
    """What one scan of the configuration found.

    ``files`` are the configuration files it read, in order; ``missing`` the
    files a command reaches that are neither a file nor a directory there.
    """

    files: tuple[str, ...]
    missing: tuple[Reach, ...]


def scan(
    source: ConfigFiles, on_file: Callable[[str, bytes], None] | None = None
) -> Scan:
    """Read the entry files and every file they name, recursively.

    ``on_file`` sees each file read with its content, as a snapshot copies
    it.
    """
    pending = [path for path in ENTRY_FILES if source.is_file(path)]
    read: list[str] = []
    reached: set[Reach] = set()
    while pending:
        path = pending.pop(0)
        if path in read:
            continue
        read.append(path)
        content = source.read(path)
        if on_file is not None:
            on_file(path, content)
        data = parse(path, content)
        if data is None:
            continue
        reached.update(reaches(path, data))
        pending.extend(
            found for found in _named(path, data, source) if found not in read
        )
    missing = tuple(
        sorted(
            reach
            for reach in reached
            if not source.is_file(reach.file) and not source.is_dir(reach.file)
        )
    )
    return Scan(tuple(read), missing)


def parse(path: str, content: bytes) -> Any | None:
    """A configuration file's data, a rule file's frontmatter, or ``None``.

    A file that does not parse is left to the configuration loader to report.
    """
    try:
        text = content.decode("utf-8")
        if path.endswith(".json"):
            return json.loads(text)
        if path.endswith(_STRUCTURED):
            return yaml.safe_load(text)
    except (UnicodeDecodeError, ValueError, yaml.YAMLError):
        return None
    return frontmatter(text) if path.endswith(_RULE_FILE) else None


def frontmatter(text: str) -> dict[str, Any] | None:
    """A rule file's YAML frontmatter, split as the rule loader splits it.

    ``None`` when it has none, or none that parses into a mapping.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            try:
                data = yaml.safe_load("".join(lines[1:index]))
            except yaml.YAMLError:
                return None
            return data if isinstance(data, dict) else None
    return None


def reaches(path: str, data: Any) -> Iterator[Reach]:
    """The files the commands in one configuration file reach."""
    for location, command in _commands(data, ""):
        owner = (
            f"rule file {path}"
            if path.endswith(_RULE_FILE)
            else f"{path} at {location or 'its top level'}"
        )
        texts = (
            text
            for key in _COMMAND_TEXT_KEYS
            if key in command
            for text in _strings(command[key])
        )
        named = [
            *(found for text in texts for found in root_references(text)),
            *(
                normalized(value)
                for key in _LISTED_KEYS
                if isinstance(command.get(key), list)
                for value in command[key]
                if isinstance(value, str)
            ),
        ]
        yield from (
            Reach(owner, file) for file in dict.fromkeys(named) if file is not None
        )


def root_references(text: str) -> tuple[str, ...]:
    """The root-relative files ``text`` reaches as ``$WW_ROOT/<path>`` or
    ``{{ww.root}}/<path>``, normalised; one built at run time is left out."""
    found = (
        normalized(match)
        for match in ROOT_REFERENCE.findall(text)
        if not _DYNAMIC.search(match)
    )
    return tuple(dict.fromkeys(path for path in found if path is not None))


def command_references(command: Commands) -> tuple[str, ...]:
    """The root-relative files a planned command reaches, once each."""
    return tuple(
        dict.fromkeys(
            path
            for definition in command.commands
            for text in (*definition.templates, definition.shell or "")
            for path in root_references(text)
        )
    )


def normalized(path: str) -> str | None:
    """``path`` inside the root, normalised, or ``None`` when it leaves it."""
    stripped = path.strip()
    if not stripped or stripped.startswith("/") or "\n" in stripped:
        return None
    result = posixpath.normpath(stripped)
    if result in {".", ""} or result.split("/")[0] == "..":
        return None
    return result


# ----- the checkout --------------------------------------------------------


class CheckoutFiles:
    """The configuration files of a checkout, noting every path looked at.

    What each looked-at path was, ``stat``-wise, tells later whether a kept
    scan still holds without reading a file again.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.looked_at: dict[str, None] = {}

    def is_file(self, path: str) -> bool:
        self.looked_at[path] = None
        return (self.directory / path).is_file()

    def is_dir(self, path: str) -> bool:
        self.looked_at[path] = None
        return (self.directory / path).is_dir()

    def listing(self, directory: str) -> Iterable[str]:
        self.looked_at[directory] = None
        try:
            entries = sorted((self.directory / directory).iterdir())
        except OSError:
            return ()
        return tuple(
            posixpath.join(directory, entry.name)
            for entry in entries
            if entry.is_file()
        )

    def read(self, path: str) -> bytes:
        self.looked_at[path] = None
        try:
            return (self.directory / path).read_bytes()
        except OSError:
            return b""


def checkout_missing(directory: Path) -> tuple[Reach, ...]:
    """The files the checkout's commands reach that it lacks.

    The scan is kept in ``.ww`` with every path it looked at; while each
    still ``stat``\\ s the same, the kept result is returned unread.
    """
    record = directory / CHECKOUT_RECORD
    kept = _read_record(record)
    if kept is not None and kept.get("directory") == str(directory):
        paths = kept.get("paths")
        if isinstance(paths, list) and kept.get("digest") == _digest(
            directory, [str(path) for path in paths]
        ):
            return _missing(kept)
    source = CheckoutFiles(directory)
    result = scan(source)
    paths = list(source.looked_at)
    if record.parent.is_dir():
        _write_record(
            record,
            {
                "directory": str(directory),
                "paths": paths,
                "digest": _digest(directory, paths),
                "missing": [reach.to_dict() for reach in result.missing],
            },
        )
    return result.missing


def snapshot_record(missing: Iterable[Reach]) -> str:
    """What a snapshot keeps of its scan."""
    return json.dumps(
        {
            "version": _RECORD_VERSION,
            "missing": [reach.to_dict() for reach in missing],
        },
        indent=2,
    )


def snapshot_missing(directory: Path) -> tuple[Reach, ...]:
    """The files a snapshot's commands reach that its commit lacks."""
    kept = _read_record(directory / SNAPSHOT_RECORD)
    return _missing(kept) if kept is not None else ()


def _missing(kept: dict[str, Any]) -> tuple[Reach, ...]:
    entries = kept.get("missing")
    if not isinstance(entries, list):
        return ()
    return tuple(reach for reach in map(Reach.from_dict, entries) if reach is not None)


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != _RECORD_VERSION:
        return None
    return data


def _write_record(path: Path, data: dict[str, Any]) -> None:
    """Write a checkout record atomically; a failure only costs a rescan."""
    with contextlib.suppress(OSError):
        handle, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump({"version": _RECORD_VERSION, **data}, file)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _digest(directory: Path, paths: list[str]) -> str:
    """What every path looked at is now: kind, size and modification time."""
    digest = hashlib.sha256()
    for path in paths:
        try:
            status = (directory / path).stat()
        except (OSError, ValueError):
            state = "-"
        else:
            state = f"{status.st_mode >> 12}:{status.st_size}:{status.st_mtime_ns}"
        digest.update(f"{path}\0{state}\n".encode())
    return digest.hexdigest()


# ----- what a configuration file names --------------------------------------


def _named(path: str, data: Any, source: ConfigFiles) -> Iterator[str]:
    """The files a configuration file's values name.

    A YAML or JSON value names a file relative to the root or to the file's
    own directory, or, in a command, as ``{{ww.root}}/<path>`` or
    ``$WW_ROOT/<path>``; a directory stands for the ``*.md`` files directly
    inside it, as a rule directory does. A rule file names the files its
    check reaches. Values naming nothing there are ignored.
    """
    if path.endswith(_RULE_FILE):
        yield from (
            reach.file for reach in reaches(path, data) if source.is_file(reach.file)
        )
        return
    base = posixpath.dirname(path)
    for value in _strings(data):
        for candidate in (value, *ROOT_REFERENCE.findall(value)):
            for relative in dict.fromkeys((candidate, posixpath.join(base, candidate))):
                found = normalized(relative)
                if found is None:
                    continue
                if source.is_file(found):
                    yield found
                elif source.is_dir(found):
                    yield from (
                        file for file in source.listing(found) if file.endswith(".md")
                    )


def _commands(data: Any, location: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every mapping in ``data`` that is a command, with where it is."""
    if isinstance(data, dict):
        if any(key in data for key in _COMMAND_KEYS):
            yield location, data
            return
        for key, value in data.items():
            yield from _commands(value, f"{location}.{key}" if location else str(key))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from _commands(value, f"{location}[{index}]")


def _strings(data: Any) -> Iterator[str]:
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str):
                yield key
            yield from _strings(value)
    elif isinstance(data, list):
        for value in data:
            yield from _strings(value)
