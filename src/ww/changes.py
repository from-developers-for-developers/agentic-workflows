# SPDX-License-Identifier: GPL-3.0-or-later
"""The files a step changed, measured with git between two tree marks.

A mark is the tree git would commit if everything in the working directory,
tracked or not, were staged: ``git add -A`` then ``git write-tree`` against a
temporary copy of the index, so the real index, the stash, and the working
tree are never touched. The change set of a step is the difference between
the mark taken when the step began and the one taken when it completes, so
work that was already uncommitted before the step cancels out and a commit
made during the step still counts.

Without git there is no change set: a rule's globs then select every file
in the directory, and the report says so.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from itertools import dropwhile
from pathlib import Path
from typing import Protocol

from ww.config_files import RULE_AUTOMATION_FILE

# Directories never part of the files a rule may check, and ww's own
# rule-automation store at the root, which verification itself writes.
_EXCLUDED = frozenset({".git", ".ww", RULE_AUTOMATION_FILE})


def take_mark(workdir: Path) -> str | None:
    """The tree id of everything in ``workdir``, or ``None`` without git."""
    if not _inside_work_tree(workdir):
        return None
    index = _git(workdir, "rev-parse", "--git-path", "index")
    if index is None:
        return None
    real_index = Path(index.strip())
    if not real_index.is_absolute():
        real_index = workdir / real_index
    with tempfile.TemporaryDirectory(prefix="ww-mark-") as scratch:
        temporary = Path(scratch) / "index"
        if real_index.is_file():
            shutil.copyfile(real_index, temporary)
        environment = {"GIT_INDEX_FILE": str(temporary)}
        # ww's own rule-automation store is written while a step's
        # verification runs; leaving it out keeps the mark of an unchanged
        # tree stable, so a held completion's checks need not run twice.
        added = _git(
            workdir,
            "add",
            "-A",
            "--",
            ":/",
            f":(exclude){RULE_AUTOMATION_FILE}",
            environment=environment,
        )
        if added is None:
            return None
        tree = _git(workdir, "write-tree", environment=environment)
    return tree.strip() if tree else None


def can_diff(workdir: Path, mark_a: str | None, mark_b: str | None) -> bool:
    """Whether ``workdir`` is still a repository that holds both marks."""
    return all(
        mark is not None
        and _git(workdir, "cat-file", "-e", f"{mark}^{{tree}}") is not None
        for mark in (mark_a, mark_b)
    )


def changed_files(workdir: Path, mark_a: str, mark_b: str) -> tuple[str, ...]:
    """Files added, copied, modified, or renamed between two marks, sorted.

    Paths are relative to ``workdir``. Deleted files are left out: there is
    nothing in them to check. So are ww's own state under ``.ww`` and entries
    that are not regular files, such as a nested repository.
    """
    output = _git(
        workdir,
        "diff-tree",
        "-r",
        "--name-only",
        "--no-renames",
        "--diff-filter=ACMR",
        "--relative",
        mark_a,
        mark_b,
    )
    if output is None:
        return ()
    return tuple(
        sorted(
            line
            for line in output.splitlines()
            if line
            and line.split("/", 1)[0] not in _EXCLUDED
            and (workdir / line).is_file()
        )
    )


def all_files(workdir: Path) -> tuple[str, ...]:
    """Every file under ``workdir``, relative and sorted, for a run without git.

    ``.git``, ``.ww`` and the rule-automation store are skipped. Git
    worktrees need git, so a directory without git has none to skip.
    """
    found: list[str] = []
    for directory, names, files in os.walk(workdir):
        relative = Path(directory).relative_to(workdir).as_posix()
        names[:] = sorted(name for name in names if name not in _EXCLUDED)
        found.extend(
            _join(relative, name)
            for name in files
            if relative not in {"", "."} or name not in _EXCLUDED
        )
    return tuple(sorted(found))


def project_files(workdir: Path) -> tuple[str, ...]:
    """The files a glob in ``workdir`` could match, relative and sorted.

    With git: tracked files and untracked ones git does not ignore, so a
    dependency directory listed in ``.gitignore`` does not count. Without
    git: :func:`all_files`.
    """
    if not _inside_work_tree(workdir):
        return all_files(workdir)
    output = _git(
        workdir, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    if output is None:
        return all_files(workdir)
    return tuple(
        sorted(
            {
                path
                for path in output.split("\0")
                if path and path.split("/", 1)[0] not in _EXCLUDED
            }
        )
    )


def changed_lines(workdir: Path, mark_a: str, mark_b: str, path: str) -> str | None:
    """The lines of ``path`` added or removed between two marks, joined.

    No context lines and no headers: a new file contributes every line, a
    binary file none. ``None`` when git cannot diff the file, or its diff is
    not UTF-8 text.
    """
    output = _git(
        workdir,
        "diff",
        "--no-renames",
        "--no-ext-diff",
        "--no-color",
        "--unified=0",
        mark_a,
        mark_b,
        "--",
        f":(literal){path}",
    )
    if output is None:
        return None
    hunks = dropwhile(lambda line: not line.startswith("@@"), output.splitlines())
    return "\n".join(line[1:] for line in hunks if line[:1] in {"+", "-"})


class Scope(Protocol):
    """What narrows a check or rule to some of the changed files."""

    @property
    def paths(self) -> tuple[str, ...]: ...
    @property
    def contains_in_file(self) -> tuple[str, ...]: ...
    @property
    def contains_in_diff(self) -> tuple[str, ...]: ...


def is_scoped(scope: Scope) -> bool:
    """Whether a check or rule narrows the change set at all."""
    return bool(scope.paths or scope.contains_in_file or scope.contains_in_diff)


def select_files(
    files: Iterable[str],
    globs: tuple[str, ...],
    contains_in_file: tuple[str, ...] = (),
    contains_in_diff: tuple[str, ...] = (),
    *,
    directory: Path | None = None,
    marks: tuple[str, str] | None = None,
) -> tuple[str, ...]:
    """The files matching any glob and holding a string of each kind given.

    A filter that is not given selects every file. ``*`` and ``?`` stay
    within one path segment, ``**`` spans any number of segments, and a glob
    without a ``/`` matches a file's name anywhere, so ``*.py`` and
    ``**/*.py`` mean the same. The strings are plain, case-sensitive
    substrings (no regular expressions): ``contains_in_file`` of the file's
    text, read under ``directory``; ``contains_in_diff`` of the lines the
    file gained or lost between the two tree ``marks``, or of its whole text
    when the files were not measured with git. A file that cannot be read as
    text is not selected.
    """
    selected: Iterable[str] = files
    if globs:
        patterns = tuple(_compile(glob) for glob in globs)
        selected = (
            path
            for path in selected
            if any(pattern.fullmatch(path) for pattern in patterns)
        )
    if contains_in_file:
        selected = (
            path
            for path in selected
            if _holds_any(_file_text(directory, path), contains_in_file)
        )
    if contains_in_diff:
        selected = (
            path
            for path in selected
            if _holds_any(_diff_text(directory, path, marks), contains_in_diff)
        )
    return tuple(selected)


def _holds_any(text: str | None, needles: tuple[str, ...]) -> bool:
    return text is not None and any(needle in text for needle in needles)


def _file_text(directory: Path | None, path: str) -> str | None:
    try:
        return ((directory or Path()) / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _diff_text(
    directory: Path | None, path: str, marks: tuple[str, str] | None
) -> str | None:
    if marks is None:
        return _file_text(directory, path)
    return changed_lines(directory or Path(), *marks, path)


def _compile(glob: str) -> re.Pattern[str]:
    glob = glob.strip().removeprefix("./")
    if "/" not in glob.rstrip("/"):
        glob = f"**/{glob}"
    if glob.endswith("/"):
        glob += "**"
    parts: list[str] = []
    segments = glob.split("/")
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            parts.append(".*" if last else "(?:[^/]+/)*")
            continue
        parts.append(_segment(segment) + ("" if last else "/"))
    # The glob over a whole path: "src/**/*.py" matches "src/cli.py" and
    # "src/ww/cli.py", but not "docs/src/cli.py".
    return re.compile("".join(parts))


def _segment(segment: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            result.append("[^/]*")
        elif char == "?":
            result.append("[^/]")
        elif char == "[":
            end = segment.find("]", index + 1)
            if end == -1:
                result.append(re.escape(char))
            else:
                body = segment[index + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                result.append(f"[{body}]")
                index = end
        else:
            result.append(re.escape(char))
        index += 1
    return "".join(result)


def _join(directory: str, name: str) -> str:
    return name if directory in {"", "."} else f"{directory}/{name}"


def _inside_work_tree(workdir: Path) -> bool:
    output = _git(workdir, "rev-parse", "--is-inside-work-tree")
    return output is not None and output.strip() == "true"


def _git(
    workdir: Path, *arguments: str, environment: dict[str, str] | None = None
) -> str | None:
    """Run git in ``workdir``; ``None`` when git is missing, the call fails,
    or its output is not UTF-8 text."""
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=workdir,
            capture_output=True,
            encoding="utf-8",
            check=False,
            env={**os.environ, **(environment or {})},
        )
    except (OSError, UnicodeDecodeError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout
