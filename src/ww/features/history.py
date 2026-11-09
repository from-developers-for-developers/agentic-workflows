# SPDX-License-Identifier: GPL-3.0-or-later
"""What git says about a map: how far its code moved on since the map did,
and which files a task or a checkout changed.

A map is stale by the commits that changed the files it points to after the
last commit that changed the map itself. Each such commit names its task
when ww made it, by its ``WW-Operation`` trailer, or when its subject follows
the ww/git ``commit_format``; any other commit counts as made outside ww.
Nothing is stamped into the maps.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ww.changes import branch_base, take_mark, touched_files

from .maps import FeatureMap

# The trailer ww writes on its own commits: the task ID comes first.
_OPERATION = re.compile(r"^WW-Operation:\s*([^:\s]+)", re.MULTILINE)
_RECORD = "\x1e"
_FIELD = "\x1f"


@dataclass(frozen=True)
class Staleness:
    """How far a map's code moved on since the map last changed.

    ``committed`` is false while the map has no commit of its own, which
    leaves nothing to measure from. ``tasks`` are the tasks whose commits
    changed its files since, in the order they were made; ``outside`` counts
    the commits that name no task.
    """

    committed: bool = True
    commits: int = 0
    tasks: tuple[str, ...] = ()
    outside: int = 0

    @property
    def stale(self) -> bool:
        return self.commits > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "committed": self.committed,
            "commits": self.commits,
            "tasks": list(self.tasks),
            "outside": self.outside,
        }

    def describe(self) -> str:
        """One line for a reader of the map."""
        if not self.committed:
            return "not committed yet, so its staleness is unknown"
        if not self.stale:
            return "up to date with its files"
        named = [*self.tasks, *([f"{self.outside} outside ww"] if self.outside else [])]
        return (
            f"{self.commits} commit(s) changed its files since the map last "
            f"changed ({', '.join(named)})"
        )


def staleness(
    root: Path, feature: FeatureMap, commit_format: str | None = None
) -> Staleness | None:
    """How stale ``feature`` is in the repository at ``root``; ``None``
    without git."""
    map_file = feature.file.relative_to(root).as_posix()
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        return None
    last = _git(root, "log", "-1", "--format=%H", "--", map_file)
    if not last:
        return Staleness(committed=False)
    pathspecs = [
        f":(glob){pattern}" if re.search(r"[*?\[]", pattern) else pattern
        for _, pointer in feature.pointers()
        for pattern in pointer.patterns()
    ]
    log = _git(
        root,
        "log",
        f"--format=%H{_FIELD}%s{_FIELD}%B{_RECORD}",
        f"{last.strip()}..HEAD",
        "--",
        *pathspecs,
    )
    subject_task = _subject_pattern(commit_format)
    tasks: list[str] = []
    outside = 0
    commits = 0
    for record in (log or "").split(_RECORD):
        if not record.strip():
            continue
        _, subject, body = record.strip("\n").split(_FIELD, 2)
        commits += 1
        found = _OPERATION.search(body)
        task = found.group(1) if found else None
        if task is None and subject_task is not None:
            match = subject_task.match(subject)
            task = match.group("task") if match else None
        if task is None:
            outside += 1
        elif task not in tasks:
            tasks.append(task)
    # git log lists the newest first; a reader follows them as they were made.
    return Staleness(True, commits, tuple(reversed(tasks)), outside)


def changed_paths(directory: Path, base: str | None = None) -> tuple[str, ...] | None:
    """Every path changed in ``directory``, deleted ones included: since it
    forked from branch ``base``, or, without one, since ``HEAD``. ``None``
    without git."""
    tree = branch_base(directory, base)
    mark = take_mark(directory)
    if tree is None or mark is None:
        return None
    return touched_files(directory, tree, mark)


def _subject_pattern(commit_format: str | None) -> re.Pattern[str] | None:
    """A ww/git ``commit_format`` as a pattern reading the task from a subject."""
    if not commit_format or "{{ww.task.id}}" not in commit_format:
        return None
    pattern = ""
    for piece in re.split(r"(\{\{[^}]*\}\})", commit_format.splitlines()[0]):
        if piece == "{{ww.task.id}}":
            pattern += r"(?P<task>\S+?)"
        elif piece.startswith("{{"):
            pattern += ".*?"
        else:
            pattern += re.escape(piece)
    return re.compile(pattern + "$")


def _git(directory: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=directory,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None
