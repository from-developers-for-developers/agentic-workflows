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
from pathlib import Path

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


def select_files(files: Iterable[str], globs: tuple[str, ...]) -> tuple[str, ...]:
    """The files matching any glob; every file when there are no globs.

    ``*`` and ``?`` stay within one path segment, ``**`` spans any number of
    segments, and a glob without a ``/`` matches a file's name anywhere, so
    ``*.py`` and ``**/*.py`` mean the same.
    """
    if not globs:
        return tuple(files)
    patterns = tuple(_compile(glob) for glob in globs)
    return tuple(
        path for path in files if any(pattern.fullmatch(path) for pattern in patterns)
    )


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
    """Run git in ``workdir``; ``None`` when git is missing or the call fails."""
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **(environment or {})},
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout
