# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading an agent's own session logs for token usage.

An agent that keeps a log of each session, with the tokens of every model
response, has a :class:`UsageReader`.  The reader only translates: it finds a
session's log files by the session's ID, and turns new lines into events in
order — a model response's token counts, a ww command the agent ran, a
subagent it started.  Which task, step and role the tokens belong to is
decided by :mod:`ww.usage`, never here.

Readers keep numbers and identifiers only.  A command line is read to find
the ww commands in it and is never returned whole; no message text leaves a
reader.
"""

from __future__ import annotations

import json
import re
import shlex
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TokenCounts:
    """Tokens of one or more model responses, split the way prices are.

    ``input`` is fresh input only: cache reads and writes are counted apart,
    whatever the agent's log folds into its own input figure.  ``reasoning``
    is part of ``output``, shown separately, and never added to the total.
    """

    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    reasoning: int = 0

    @property
    def total(self) -> int:
        return self.input + self.cache_write + self.cache_read + self.output

    def __add__(self, other: TokenCounts) -> TokenCounts:
        return TokenCounts(
            self.input + other.input,
            self.cache_write + other.cache_write,
            self.cache_read + other.cache_read,
            self.output + other.output,
            self.reasoning + other.reasoning,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "cache_write": self.cache_write,
            "cache_read": self.cache_read,
            "output": self.output,
            "reasoning": self.reasoning,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TokenCounts:
        return cls(**{name: count(data.get(name)) for name in TOKEN_FIELDS})


TOKEN_FIELDS = ("input", "cache_write", "cache_read", "output", "reasoning")


@dataclass(frozen=True)
class LogFile:
    """One log file of a session: the session's own, or a subagent's.

    ``key`` identifies the file by IDs alone, ``<session>`` or
    ``<session>/<subagent>``, so the same file read on a host and in a
    container is one file; ``path`` is only where it is found here.  A reader
    holds no agent identity: whoever reads the file qualifies the key with
    the name ww addresses the agent by.  A subagent's file names its spawner
    either directly (``parent``, the spawner file's key) or through the tool
    call that started it (``spawn_tool``, found among the session's
    :class:`Spawn` events).
    """

    key: str
    path: Path
    depth: int = 0
    parent: str | None = None
    spawn_tool: str | None = None
    spawned_at: str | None = None


@dataclass(frozen=True)
class Usage:
    """The tokens of one model response."""

    at: str
    model: str | None
    counts: TokenCounts


@dataclass(frozen=True)
class Call:
    """A ww command the agent ran: its verb, the task it named and its role."""

    at: str
    verb: str
    task: str | None
    role: str | None


@dataclass(frozen=True)
class Spawn:
    """A tool call that started a subagent, by its tool call ID."""

    at: str
    tool: str


Event = Usage | Call | Spawn


@dataclass(frozen=True)
class TurnUsage:
    """The tokens of one agent turn, as a hook reports them.

    ``key`` identifies the turn, so a turn reported twice counts once.
    """

    session: str
    key: str
    model: str | None
    counts: TokenCounts


@dataclass
class Reading:
    """What one read of a file found, and where the next read starts."""

    events: list[Event] = field(default_factory=list)
    offset: int = 0
    # Reader-specific state carried to the next read, JSON-serialisable.
    carry: dict[str, Any] = field(default_factory=dict)
    # The working directory the log says the session ran in, if it says.
    directory: str | None = None


class UsageReader(ABC):
    """How ww finds and reads one agent's session logs."""

    @abstractmethod
    def homes(self, environ: Mapping[str, str]) -> tuple[Path, ...]:
        """The agent's own log directories on this machine."""

    @abstractmethod
    def files(self, session: str, roots: tuple[Path, ...]) -> tuple[LogFile, ...]:
        """The session's log files under ``roots``, its own first; none if absent."""

    @abstractmethod
    def read(self, file: LogFile, offset: int, carry: dict[str, Any]) -> Reading:
        """The events after ``offset``, up to the last complete line."""

    @abstractmethod
    def sessions_in(self, roots: tuple[Path, ...], directory: Path) -> tuple[str, ...]:
        """The IDs of top-level sessions that ran in ``directory`` or below it."""


# ww's commands that move a session onto a task, and so mark where its
# tokens start to count for that task.  Read-only commands such as
# ``status`` do not: a session may look at another task while it works.
TASK_VERBS = frozenset(
    {
        "start",
        "instruction",
        "next",
        "complete",
        "fail",
        "record",
        "interact",
        "handoff",
        "amend",
        "check",
        "dispute",
        "add-item",
        "update-item",
        "remove-item",
        "resolve-item",
        "report-item",
        "add-child",
        "update-child",
        "start-child",
    }
)
# ``./ww``, ``"$CLAUDE_PROJECT_DIR"/ww``, ``ww`` or an installed
# ``ww-<name>`` launcher, then optional global options, then the verb.
_WW_COMMAND = re.compile(
    r"(?:^|[\s;&|(`'\"])(?:[^\s;&|()`]*/)?ww(?:-[\w.-]+)?"
    r"(?P<options>(?:\s+--root(?:=|\s+)\S+)*)"
    r"\s+(?P<verb>[a-z][a-z-]*)(?P<rest>[^\n;&|]*)"
)
_ROLE = re.compile(r"--role(?:=|\s+)['\"]?(manager|worker)\b")


def ww_calls(text: str, at: str) -> list[Call]:
    """The ww commands that move a session onto a task, found in ``text``."""
    calls = []
    for match in _WW_COMMAND.finditer(text):
        verb = match["verb"]
        if verb not in TASK_VERBS:
            continue
        rest = match["rest"]
        try:
            words = shlex.split(rest)
        except ValueError:
            words = rest.split()
        first = words[0].strip("\"'\\,;)}") if words else ""
        task = first if first and not first.startswith("-") else None
        role = _ROLE.search(rest)
        calls.append(Call(at, verb, task, role[1] if role else None))
    return calls


def count(value: object) -> int:
    """A token figure from a log: a non-negative integer, else nothing."""
    return value if isinstance(value, int) and value > 0 else 0


def lines_from(path: Path, offset: int) -> Iterator[tuple[int, dict[str, Any]]]:
    """Each complete JSON line after ``offset`` with the offset after it.

    A line still being written, with no newline yet, is left for the next
    read; a line that is not a JSON object is skipped.
    """
    with path.open("rb") as handle:
        handle.seek(offset)
        position = offset
        for raw in handle:
            if not raw.endswith(b"\n"):
                return
            position += len(raw)
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if isinstance(data, dict):
                yield position, data


def first_line(path: Path) -> dict[str, Any] | None:
    """The file's first line as a JSON object, if it is one."""
    try:
        with path.open("rb") as handle:
            data = json.loads(handle.readline())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def within(directory: str | None, root: Path) -> bool:
    """Whether a logged working directory is ``root`` or below it."""
    if not directory:
        return False
    path = Path(directory)
    return path == root or root in path.parents
