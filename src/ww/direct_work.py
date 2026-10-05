# SPDX-License-Identifier: GPL-3.0-or-later
"""Direct work: changes the operator asked for outside any workflow.

An agent makes such a change as it would in a plain conversation and
registers it afterwards with ``ww record``. ww keeps each registration in the
task's ``direct-work.json``, with the commits it found for it.

The commits are found here, never reported by the agent. The ww/git
extension records the task's branch (``branches.jsonl``) and every commit it
made (``commits.jsonl``); the commits on the branch since its base, minus
those ww already knows, are the work ww had not seen. A task without a
recorded branch falls back to the current branch's commits that mention the
task ID. ``instruction --role manager`` runs the same search, so a
registration that failed or was forgotten is made up for on the next run.

Reconciliation counts only commits made outside the task's runs: agents
commit inside a run too (a fix worker's commit, a manager's land merge), so a
commit whose committer date falls in the active window of any run (from its
creation to its completion or abandonment, or open-ended while it is
unfinished) is workflow work, not direct work.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ww.extensions.store import ExtensionStore
from ww.validation import expect_mapping, expect_nonempty_string

if TYPE_CHECKING:
    from ww.project_config import ProjectConfig

GIT_EXTENSION = "ww/git"
BRANCHES_FILE = "branches.jsonl"
COMMITS_FILE = "commits.jsonl"
DirectWorkSource = Literal["agent", "reconciled"]
SOURCES: tuple[DirectWorkSource, ...] = ("agent", "reconciled")
# How far back the base branch itself is searched for commits naming a task.
_RECENT_LIMIT = 200
# The fields ``git log`` prints per commit, separated by the unit separator.
_LOG_FORMAT = "%H%x1f%s%x1f%cI"
_LOG_FIELDS = 3
SHORT_SHA = 7


@dataclass(frozen=True)
class DirectCommit:
    """One commit found for a piece of direct work."""

    sha: str
    subject: str
    committed_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "sha": self.sha,
            "subject": self.subject,
            "committed_at": self.committed_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> DirectCommit:
        record = expect_mapping(data, "direct-work commit")
        return cls(
            sha=expect_nonempty_string(record.get("sha"), "direct-work commit sha"),
            subject=_string(record, "subject", "direct-work commit subject"),
            committed_at=_string(
                record, "committed_at", "direct-work commit committed_at"
            ),
        )


@dataclass(frozen=True)
class DirectWork:
    """One registration of direct work, in ``direct-work.json``."""

    recorded_at: str
    summary: str
    source: DirectWorkSource
    commits: tuple[DirectCommit, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "recorded_at": self.recorded_at,
            "summary": self.summary,
            "source": self.source,
            "commits": [commit.to_dict() for commit in self.commits],
        }

    @classmethod
    def from_dict(cls, data: object) -> DirectWork:
        record = expect_mapping(data, "direct-work entry")
        source = record.get("source")
        if source not in SOURCES:
            raise ValueError("direct-work source must be 'agent' or 'reconciled'")
        commits = record.get("commits", [])
        if not isinstance(commits, list):
            raise ValueError("direct-work commits must be a list")
        return cls(
            recorded_at=expect_nonempty_string(
                record.get("recorded_at"), "direct-work recorded_at"
            ),
            summary=expect_nonempty_string(
                record.get("summary"), "direct-work summary"
            ),
            source=source,
            commits=tuple(DirectCommit.from_dict(entry) for entry in commits),
        )


def _string(record: dict[str, Any], key: str, context: str) -> str:
    value = record.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{context} must be a string")
    return value


def reconciled_summary(commits: Iterable[DirectCommit]) -> str:
    """The summary of a reconciled entry: the commit subjects, joined."""
    return "; ".join(commit.subject for commit in commits)


def unseen_commits(
    root: Path,
    config: ProjectConfig,
    task_id: str,
    registered: Iterable[DirectWork],
    run_windows: Iterable[RunWindow] = (),
) -> tuple[DirectCommit, ...]:
    """The commits made for ``task_id`` that ww has not seen, oldest first.

    ``commits.jsonl`` holds what ww's own handlers committed and
    ``registered`` (the task's ``direct-work.json``) what was already
    registered; neither is returned.
    Anything git cannot answer (no repository, a branch since deleted) is
    reported as no commits.
    """
    store = ExtensionStore(root, GIT_EXTENSION)
    branch = _recorded_branch(store, task_id)
    default_base = _default_base(config)
    if branch is not None and isinstance(branch.get("branch"), str):
        base = branch.get("base")
        start = base if isinstance(base, str) and base else default_base
        found = (
            _log(_repository(root, branch), [f"{start}..{branch['branch']}"])
            if start
            else ()
        )
    else:
        found = _mentioning(root, default_base, task_id)
    seen = _ww_commits(store) | {
        commit.sha for entry in registered for commit in entry.commits
    }
    windows = tuple(run_windows)
    return tuple(
        commit
        for commit in found
        if commit.sha not in seen and not _inside(commit.committed_at, windows)
    )


# A run's active window: its start and, once it is over, its end.
RunWindow = tuple[datetime, datetime | None]


def _inside(committed_at: str, windows: tuple[RunWindow, ...]) -> bool:
    try:
        moment = datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return any(
        start <= moment and (end is None or moment <= end) for start, end in windows
    )


def _mentioning(root: Path, base: str | None, task_id: str) -> tuple[DirectCommit, ...]:
    """Commits on the current branch since ``base`` that name ``task_id``."""
    if base is None:
        return ()
    mention = re.compile(rf"(?<![\w/-]){re.escape(task_id)}(?![\w/-])")
    commits = _log(root, [f"{base}..HEAD"])
    head = _git(root, "rev-parse", "HEAD")
    if not commits and head is not None and head == _git(root, "rev-parse", base):
        # HEAD is the base itself, so the range is empty: look at its recent
        # history instead.
        commits = _log(root, ["HEAD"], _RECENT_LIMIT)
    return tuple(
        commit
        for commit in commits
        if mention.search(commit.subject) or mention.search(_body(root, commit.sha))
    )


def _body(root: Path, sha: str) -> str:
    return _git(root, "show", "-s", "--format=%B", sha) or ""


def _log(
    cwd: Path, revisions: list[str], limit: int | None = None
) -> tuple[DirectCommit, ...]:
    if not revisions:
        return ()
    bound = [] if limit is None else [f"--max-count={limit}"]
    output = _git(
        cwd, "log", "--reverse", *bound, f"--format={_LOG_FORMAT}", *revisions
    )
    commits: list[DirectCommit] = []
    for line in (output or "").splitlines():
        fields = line.split("\x1f")
        if len(fields) == _LOG_FIELDS and fields[0]:
            commits.append(DirectCommit(fields[0], fields[1], fields[2]))
    return tuple(commits)


def _git(cwd: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def _repository(root: Path, branch: dict[str, Any]) -> Path:
    """Where to ask git: the task's recorded worktree while it exists."""
    worktree = branch.get("worktree")
    if isinstance(worktree, str) and Path(worktree).is_dir():
        return Path(worktree)
    return root


def _default_base(config: ProjectConfig) -> str | None:
    """The ww/git ``base_branches.default``, when it names a branch."""
    bases = config.settings_for(GIT_EXTENSION).get("base_branches")
    default = bases.get("default") if isinstance(bases, dict) else None
    return default if isinstance(default, str) and default else None


def _recorded_branch(store: ExtensionStore, task_id: str) -> dict[str, Any] | None:
    for line in reversed(store.read_lines(BRANCHES_FILE)):
        record = _json_line(line)
        if record is not None and record.get("task_id") == task_id:
            return record
    return None


def _ww_commits(store: ExtensionStore) -> set[str]:
    shas: set[str] = set()
    for line in store.read_lines(COMMITS_FILE):
        record = _json_line(line)
        if record is not None and isinstance(record.get("sha"), str):
            shas.add(record["sha"])
    return shas


def _json_line(line: str) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None
