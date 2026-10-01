# SPDX-License-Identifier: GPL-3.0-or-later
"""A read-only profile of the checkout, built from its files and ``git``.

``ww inspect`` reads the working tree and the local Git history and reports
facts the setup workflows build a proposal from. Every fact names where it
came from, or says it was not found. Nothing is written and nothing leaves the
machine: Git runs as an argument list, without a shell, with a timeout and a
bounded ``-n``, and never fetches. A failing or slow Git call leaves only that
fact not found.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Generic, TypeVar

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:  # pyproject.toml is read from Python 3.11, which ships tomllib.
    tomllib = None

DEFAULT_COMMITS = 300
GIT_TIMEOUT_SECONDS = 15
DAY_SECONDS = 86_400
WEEK_SECONDS = 7 * DAY_SECONDS
ACTIVE_DAYS = 90
CADENCE_WEEKS = 12
DAILY_COMMITS_PER_WEEK = 5
SMALL_TEAM_MAX = 5
TOP_FIX_PATHS = 5
RECENT_FIXES = 5
TOP_HOT_PATHS = 10
TOP_TICKET_PREFIXES = 3
TAG_GAPS_FROM = 10
LIFETIME_MERGES = 10
LIFETIME_COMMITS = 200
MAJORITY = 0.5
# Below this share of merge commits the history reads as rebased or squashed.
MERGE_STYLE_SHARE = 0.05
MANIFEST_DEPTH = 2
MONOREPO_MIN_DIRECTORIES = 2
# Hash, parents, author e-mail, author time and subject, as _log_arguments asks.
LOG_FIELDS = 5
MAX_LISTED_FILES = 200_000
MAX_LISTED_BRANCHES = 2_000

VERIFY_NAMES = ("test", "lint", "typecheck", "check", "format", "build")
MANIFEST_NAMES = (
    "package.json",
    "pyproject.toml",
    "Makefile",
    "composer.json",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)
MONOREPO_DIRECTORIES = ("apps", "packages", "services")
SKIPPED_DIRECTORIES = frozenset(
    {"node_modules", "vendor", "dist", "build", "target", "venv", "__pycache__"}
)
AGENT_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".codex", ".claude")

# A subject that reports a fix, matched as a whole word in any case,
# e.g. "fix: keep the cursor" or 'Revert "Add the cache"'.
FIX_SUBJECT = re.compile(r"\b(fix|fixes|fixed|revert|hotfix|regression)\b", re.I)
# A tracker key such as an issue ID, e.g. "PROJ-12".
TICKET_KEY = re.compile(r"\b([A-Z][A-Z0-9]+)-\d+\b")
# A subject that starts with a tracker key, bracketed or not,
# e.g. "PROJ-12: Add the cache" or "[PROJ-12] Add the cache".
TICKET_SUBJECT = re.compile(r"^\[?[A-Z][A-Z0-9]+-\d+\]?[:\s]")
# A conventional-commits subject: type, optional scope, optional "!", colon,
# e.g. "fix(cli): keep the cursor".
CONVENTIONAL_SUBJECT = re.compile(r"^[a-z]+(\([\w./-]+\))?!?: ")
# The branch a merge commit's subject names, e.g. "Merge branch 'hotfix/x'"
# gives "hotfix/x" and "Merge pull request #7 from owner/feature/y" gives
# "owner/feature/y".
MERGED_BRANCH = re.compile(
    r"^Merge (?:remote-tracking )?branch '([^']+)'|^Merge pull request #\d+ from (\S+)"
)
# A Makefile rule's target at the start of a line, not a variable assignment,
# e.g. "test: deps" gives "test".
MAKE_TARGET = re.compile(r"^([A-Za-z0-9][\w.-]*)\s*:(?![:=])", re.M)
# A relative link out of this checkout into a sibling directory,
# e.g. "../billing-api." gives "billing-api" and "../web.app" gives "web.app".
SIBLING_LINK = re.compile(r"(?<![\w.])\.\./([A-Za-z0-9][\w-]*(?:\.[\w-]+)*)")
# A Git remote URL, SSH or HTTPS, ending in ".git",
# e.g. "git@example.com:team/billing-api.git" gives "billing-api".
GIT_URL = re.compile(
    r"(?:git@[\w.-]+:|https?://[\w.-]+/)[\w.-]+/([\w-]+(?:\.[\w-]+)*)\.git\b"
)

# A Compose file at the root, e.g. "docker-compose.dev.yml" or "compose.yaml".
COMPOSE_FILE = re.compile(r"(docker-)?compose[\w.-]*\.ya?ml$")

T = TypeVar("T")


@dataclass(frozen=True)
class Fact(Generic[T]):
    """One observation and where it came from; ``None`` means not found."""

    value: T | None
    evidence: str


@dataclass(frozen=True)
class Count:
    name: str
    count: int


@dataclass(frozen=True)
class Manifest:
    path: str
    kind: str
    scripts: tuple[str, ...] = ()
    workspaces: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifyCommand:
    """A command a manifest names for checking the code, as exact argv."""

    name: str
    argv: tuple[str, ...]
    directory: str
    source: str


@dataclass(frozen=True)
class Repository:
    default_branch: Fact[str]
    integration_branch: Fact[str]
    branch_patterns: Fact[tuple[Count, ...]]
    remotes: Fact[tuple[str, ...]]
    shallow: Fact[bool]
    merge_share: Fact[float]
    merge_style: Fact[str]
    pr_signals: Fact[tuple[str, ...]]
    tags: Fact[int]
    tag_interval_days: Fact[float]


@dataclass(frozen=True)
class Activity:
    commits: Fact[int]
    contributors: Fact[int]
    active_contributors: Fact[int]
    team_shape: Fact[str]
    weekly_commits: Fact[tuple[int, ...]]
    cadence: Fact[str]
    files_per_commit: Fact[float]
    lines_per_commit: Fact[float]
    branch_lifetime_days: Fact[float]


@dataclass(frozen=True)
class Fixes:
    share: Fact[float]
    paths: Fact[tuple[Count, ...]]
    recent: Fact[tuple[str, ...]]


@dataclass(frozen=True)
class HotPaths:
    paths: Fact[tuple[Count, ...]]


@dataclass(frozen=True)
class Layout:
    manifests: Fact[tuple[Manifest, ...]]
    ci: Fact[tuple[str, ...]]
    verify: Fact[tuple[VerifyCommand, ...]]
    monorepo_signals: Fact[tuple[str, ...]]
    projects: Fact[tuple[str, ...]]


@dataclass(frozen=True)
class Conventions:
    ticket_prefixes: Fact[tuple[Count, ...]]
    task_format: Fact[str]
    ticket_share: Fact[float]
    conventional_share: Fact[float]
    subject_convention: Fact[str]
    commit_format: Fact[str]
    agent_files: Fact[tuple[str, ...]]


@dataclass(frozen=True)
class Profile:
    """Everything ``ww inspect`` found; the Git sections are ``None`` without Git."""

    root: str
    commits_window: int
    git_unavailable: str | None
    repository: Repository | None
    activity: Activity | None
    fixes: Fixes | None
    hot_paths: HotPaths | None
    layout: Layout
    conventions: Conventions

    def to_dict(self) -> dict[str, Any]:
        """The profile as JSON-ready data: tuples become lists."""
        plain: dict[str, Any] = _plain(asdict(self))
        return plain


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    author: str
    timestamp: int
    subject: str
    # (path, added + deleted lines); binary files count zero lines.
    changes: tuple[tuple[str, int], ...]

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _git(root: Path, *arguments: str) -> str | None:
    """Run one read-only Git command; ``None`` when it fails or times out."""
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _lines(output: str | None, limit: int = MAX_LISTED_BRANCHES) -> list[str]:
    if output is None:
        return []
    return [line for line in output.splitlines() if line.strip()][:limit]


def parse_log(output: str) -> list[Commit]:
    """Read ``git log`` output in :func:`_log_arguments`' format."""
    commits = []
    for record in output.split("\x1e"):
        header, _, body = record.strip("\n").partition("\n")
        fields = header.split("\x1f")
        if len(fields) != LOG_FIELDS:
            continue
        sha, parents, author, timestamp, subject = fields
        changes = []
        for line in body.splitlines():
            added, _, rest = line.partition("\t")
            deleted, _, path = rest.partition("\t")
            if not path:
                continue
            lines = (
                int(added) + int(deleted)
                if added.isdigit() and deleted.isdigit()
                else 0
            )
            changes.append((path, lines))
        commits.append(
            Commit(
                sha=sha,
                parents=tuple(parents.split()),
                author=author.lower(),
                timestamp=int(timestamp) if timestamp.isdigit() else 0,
                subject=subject,
                changes=tuple(changes),
            )
        )
    return commits


def _log_arguments(commits: int) -> tuple[str, ...]:
    return (
        "log",
        f"-n{commits}",
        "--no-renames",
        "--numstat",
        "--format=%x1e%H%x1f%P%x1f%ae%x1f%at%x1f%s",
    )


def team_shape(active: int) -> str:
    """Solo for at most one contributor active in 90 days, small to five, else team."""
    if active <= 1:
        return "solo"
    return "small" if active <= SMALL_TEAM_MAX else "team"


def cadence(weekly: Sequence[int]) -> str:
    """Daily at a median of five commits a week or more, weekly at one, else sparse."""
    median = statistics.median(weekly) if weekly else 0
    if median >= DAILY_COMMITS_PER_WEEK:
        return "daily"
    return "weekly" if median >= 1 else "sparse"


def weekly_commits(
    timestamps: Iterable[int], now: float, weeks: int = CADENCE_WEEKS
) -> tuple[int, ...]:
    """Commits in each of the last ``weeks`` weeks, oldest week first."""
    counts = [0] * weeks
    for timestamp in timestamps:
        age = int((now - timestamp) // WEEK_SECONDS)
        if 0 <= age < weeks:
            counts[weeks - 1 - age] += 1
    return tuple(counts)


def is_fix(subject: str) -> bool:
    return FIX_SUBJECT.search(subject) is not None


def top_counts(names: Iterable[str], limit: int) -> tuple[Count, ...]:
    """The ``limit`` most frequent names, ties in first-seen order."""
    return tuple(
        Count(name, count) for name, count in Counter(names).most_common(limit)
    )


def branch_lane(name: str) -> str | None:
    """A branch's first path segment with its slash, e.g. "feature/"."""
    head, slash, _ = name.partition("/")
    return f"{head}/" if slash and head else None


def ticket_prefixes(texts: Iterable[str]) -> tuple[Count, ...]:
    """Tracker key prefixes, counted once per subject or branch name."""
    seen: list[str] = []
    for text in texts:
        seen.extend(dict.fromkeys(TICKET_KEY.findall(text)))
    return top_counts(seen, TOP_TICKET_PREFIXES)


def commit_format_for(ticket_share: float) -> str:
    """The task ID leads the subject when most subjects start with a key."""
    if ticket_share >= MAJORITY:
        return "{{ww.task.id}}: {{commit_message}}"
    return "{{commit_message}}"


def subject_convention(ticket_share: float, conventional_share: float) -> str:
    """Whichever convention most subjects follow, else free."""
    if ticket_share >= MAJORITY:
        return "ticket prefix"
    if conventional_share >= MAJORITY:
        return "conventional commits"
    return "free"


def makefile_targets(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(MAKE_TARGET.findall(text)))


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _median(values: Sequence[float]) -> float | None:
    return round(float(statistics.median(values)), 1) if values else None


def inspect_checkout(root: Path, commits: int = DEFAULT_COMMITS) -> Profile:
    """Profile the checkout at ``root`` from at most ``commits`` commits."""
    root = root.resolve()
    files, files_evidence = _tracked_files(root)
    layout = _layout(root, files, files_evidence)
    agent_files = _agent_files(root)
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        return Profile(
            root=str(root),
            commits_window=commits,
            git_unavailable="not a Git repository (git rev-parse)",
            repository=None,
            activity=None,
            fixes=None,
            hot_paths=None,
            layout=layout,
            conventions=_conventions([], [], "no Git history", agent_files),
        )
    history: list[Commit] = []
    if _git(root, "rev-parse", "--verify", "--quiet", "HEAD") is not None:
        history = parse_log(_git(root, *_log_arguments(commits)) or "")
    window = f"git log -n {commits}"
    branches, branches_evidence = _branch_names(root, history)
    return Profile(
        root=str(root),
        commits_window=commits,
        git_unavailable=None,
        repository=_repository(root, history, window, branches, branches_evidence),
        activity=_activity(root, history, window),
        fixes=_fixes(history, window),
        hot_paths=HotPaths(
            paths=Fact(
                top_counts(
                    (path for commit in history for path, _ in commit.changes),
                    TOP_HOT_PATHS,
                )
                if history
                else None,
                f"{window} --numstat",
            )
        ),
        layout=layout,
        conventions=_conventions(
            history, branches, f"{window}; {branches_evidence}", agent_files
        ),
    )


def _branch_names(root: Path, history: list[Commit]) -> tuple[list[str], str]:
    """Branch names without their remote, plus those merge subjects name."""
    remote = _refs(root, "refs/remotes")
    names = [name.partition("/")[2] for name in remote]
    evidence = "git branch -r"
    if not names:
        names = _refs(root, "refs/heads")
        evidence = "git branch"
    merged = []
    for commit in history:
        match = MERGED_BRANCH.match(commit.subject)
        if match and match.group(1):
            merged.append(match.group(1))
        elif match:
            # A pull request names "owner/branch"; the owner is not a lane.
            merged.append(match.group(2).partition("/")[2])
    if merged:
        evidence += ", merge subjects"
    return [name for name in names + merged if name], evidence


def _refs(root: Path, namespace: str) -> list[str]:
    """Short names of the refs under ``namespace``, without symbolic refs."""
    output = _git(
        root, "for-each-ref", "--format=%(refname:short)%09%(symref)", namespace
    )
    names = []
    for line in _lines(output):
        name, _, symref = line.partition("\t")
        if not symref:
            names.append(name)
    return names


def _repository(
    root: Path,
    history: list[Commit],
    window: str,
    branches: list[str],
    branches_evidence: str,
) -> Repository:
    default = _default_branch(root)
    merges = sum(commit.is_merge for commit in history)
    merge_share = _share(merges, len(history))
    style = None
    if merge_share is not None:
        style = (
            "merge commits"
            if merge_share >= MERGE_STYLE_SHARE
            else "linear (rebase or squash)"
        )
    lanes = top_counts(
        (lane for lane in map(branch_lane, branches) if lane), len(branches) or 1
    )
    remotes = _lines(_git(root, "remote"))
    shallow = _git(root, "rev-parse", "--is-shallow-repository")
    tags = _lines(_git(root, "tag", "--list"), MAX_LISTED_FILES)
    return Repository(
        default_branch=default,
        integration_branch=_integration_branch(root, default.value),
        branch_patterns=Fact(lanes, branches_evidence),
        remotes=Fact(tuple(remotes), "git remote"),
        shallow=Fact(
            None if shallow is None else shallow.strip() == "true",
            "git rev-parse --is-shallow-repository",
        ),
        merge_share=Fact(merge_share, f"{merges} of {len(history)} commits, {window}"),
        merge_style=Fact(
            style, f"merge share against {MERGE_STYLE_SHARE:.0%}, {window}"
        ),
        pr_signals=Fact(_pr_signals(root, history), f"files and subjects, {window}"),
        tags=Fact(len(tags), "git tag --list"),
        tag_interval_days=Fact(
            _tag_interval(root), f"last {TAG_GAPS_FROM} tags, git for-each-ref"
        ),
    )


def _default_branch(root: Path) -> Fact[str]:
    head = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head:
        return Fact(head.strip().partition("/")[2], "git symbolic-ref origin/HEAD")
    for name in ("main", "master"):
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            if _git(root, "rev-parse", "--verify", "--quiet", ref) is not None:
                return Fact(name, f"{ref} exists")
    current = _git(root, "symbolic-ref", "--short", "HEAD")
    if current:
        return Fact(current.strip(), "current branch, git symbolic-ref HEAD")
    return Fact(None, "origin/HEAD, main, master")


def _integration_branch(root: Path, default: str | None) -> Fact[str]:
    """``dev`` or ``develop`` when merges land on it, else the default branch."""
    for name in ("dev", "develop"):
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            merged = _git(
                root, "log", "-n", "1", "--first-parent", "--merges", "--format=%H", ref
            )
            if merged and merged.strip():
                return Fact(name, f"merges on {ref}, git log --first-parent --merges")
    return Fact(default, "no dev or develop receiving merges; the default branch")


def _pr_signals(root: Path, history: list[Commit]) -> tuple[str, ...]:
    signals = [
        path.relative_to(root).as_posix()
        for pattern in (
            ".github/PULL_REQUEST_TEMPLATE*",
            ".github/pull_request_template*",
            "PULL_REQUEST_TEMPLATE*",
            "docs/PULL_REQUEST_TEMPLATE*",
            "CODEOWNERS",
            ".github/CODEOWNERS",
            "docs/CODEOWNERS",
        )
        for path in sorted(root.glob(pattern))
    ]
    pulls = sum(commit.subject.startswith("Merge pull request") for commit in history)
    if pulls:
        signals.append(f'{pulls} "Merge pull request" subjects')
    return tuple(dict.fromkeys(signals))


def _tag_interval(root: Path) -> float | None:
    """Median days between the last ten tags by creation date."""
    output = _git(
        root,
        "for-each-ref",
        "--sort=-creatordate",
        f"--count={TAG_GAPS_FROM}",
        "--format=%(creatordate:unix)",
        "refs/tags",
    )
    stamps = [int(line) for line in _lines(output) if line.strip().isdigit()]
    gaps = [(newer - older) / DAY_SECONDS for newer, older in pairwise(stamps)]
    return _median(gaps)


def _activity(root: Path, history: list[Commit], window: str) -> Activity:
    now = time.time()
    found = bool(history)
    authors = {commit.author for commit in history}
    active = {
        commit.author
        for commit in history
        if now - commit.timestamp <= ACTIVE_DAYS * DAY_SECONDS
    }
    weekly = weekly_commits((commit.timestamp for commit in history), now)
    changes = [commit for commit in history if not commit.is_merge]
    return Activity(
        commits=Fact(len(history), window),
        contributors=Fact(len(authors) if found else None, f"author e-mails, {window}"),
        active_contributors=Fact(
            len(active) if found else None, f"authors in {ACTIVE_DAYS} days, {window}"
        ),
        team_shape=Fact(
            team_shape(len(active)) if found else None,
            f"{len(active)} active in {ACTIVE_DAYS} days: solo 1, small 2-5, team 6+",
        ),
        weekly_commits=Fact(
            weekly if found else None, f"last {CADENCE_WEEKS} weeks, {window}"
        ),
        cadence=Fact(
            cadence(weekly) if found else None,
            f"median {statistics.median(weekly):g}/week: daily 5+, weekly 1+",
        ),
        files_per_commit=Fact(
            _median([len(commit.changes) for commit in changes]),
            f"non-merge commits, {window} --numstat",
        ),
        lines_per_commit=Fact(
            _median([sum(lines for _, lines in commit.changes) for commit in changes]),
            f"non-merge commits, {window} --numstat",
        ),
        branch_lifetime_days=Fact(
            _branch_lifetime(root, history),
            f"first commit to merge, last {LIFETIME_MERGES} merges, git log P1..P2",
        ),
    )


def _branch_lifetime(root: Path, history: list[Commit]) -> float | None:
    """Median days from a merged branch's first own commit to its merge."""
    lifetimes = []
    for merge in [commit for commit in history if commit.is_merge][:LIFETIME_MERGES]:
        output = _git(
            root,
            "log",
            f"-n{LIFETIME_COMMITS}",
            "--format=%at",
            f"{merge.parents[0]}..{merge.parents[1]}",
        )
        stamps = [
            int(line) for line in _lines(output, LIFETIME_COMMITS) if line.isdigit()
        ]
        if stamps:
            lifetimes.append(max(0, merge.timestamp - min(stamps)) / DAY_SECONDS)
    return _median(lifetimes)


def _fixes(history: list[Commit], window: str) -> Fixes:
    # Merge subjects name branches ("Merge branch 'hotfix/x'"), not fixes, so
    # the share is over the commits that change something themselves.
    changes = [commit for commit in history if not commit.is_merge]
    fixes = [commit for commit in changes if is_fix(commit.subject)]
    rule = "fix|fixes|fixed|revert|hotfix|regression in the subject"
    return Fixes(
        share=Fact(
            _share(len(fixes), len(changes)),
            f"{len(fixes)} of {len(changes)} non-merge commits, {rule}, {window}",
        ),
        paths=Fact(
            top_counts(
                (path for commit in fixes for path, _ in commit.changes), TOP_FIX_PATHS
            )
            if changes
            else None,
            f"paths the fix commits touch, {window} --numstat",
        ),
        recent=Fact(
            tuple(commit.subject for commit in fixes[:RECENT_FIXES])
            if changes
            else None,
            window,
        ),
    )


def _conventions(
    history: list[Commit],
    branches: list[str],
    evidence: str,
    agent_files: Fact[tuple[str, ...]],
) -> Conventions:
    subjects = [commit.subject for commit in history if not commit.is_merge]
    prefixes = ticket_prefixes([*subjects, *branches])
    found = bool(history or branches)
    tickets = sum(TICKET_SUBJECT.match(subject) is not None for subject in subjects)
    conventional = sum(
        CONVENTIONAL_SUBJECT.match(subject) is not None for subject in subjects
    )
    ticket_share = _share(tickets, len(subjects))
    conventional_share = _share(conventional, len(subjects))
    shares = f"{tickets} ticket-led and {conventional} conventional of {len(subjects)}"
    return Conventions(
        ticket_prefixes=Fact(prefixes if found else None, evidence),
        task_format=Fact(
            f"{prefixes[0].name}-{{{{digit}}}}" if prefixes else None,
            "the most frequent tracker key prefix",
        ),
        ticket_share=Fact(ticket_share, f"{shares} non-merge subjects"),
        conventional_share=Fact(conventional_share, f"{shares} non-merge subjects"),
        subject_convention=Fact(
            subject_convention(ticket_share, conventional_share or 0.0)
            if ticket_share is not None
            else None,
            "the convention half the subjects follow, else free",
        ),
        commit_format=Fact(
            commit_format_for(ticket_share) if ticket_share is not None else None,
            "task ID first when half the subjects start with a key",
        ),
        agent_files=agent_files,
    )


def _agent_files(root: Path) -> Fact[tuple[str, ...]]:
    present = [name for name in AGENT_FILES if (root / name).exists()]
    present.extend(
        path.relative_to(root).as_posix()
        for path in sorted((root / ".cursor").glob("rules*"))
    )
    if (root / ".cursorrules").exists():
        present.append(".cursorrules")
    return Fact(tuple(present), "files at the project root")


def _tracked_files(root: Path) -> tuple[list[str], str]:
    """Paths at most two directories deep: Git's tracked files, else a walk."""
    output = _git(root, "ls-files")
    if output is not None and output.strip():
        paths = [
            line
            for line in _lines(output, MAX_LISTED_FILES)
            if line.count("/") <= MANIFEST_DEPTH
        ]
        return paths, "git ls-files"
    paths = []
    for directory, subdirectories, names in os.walk(root):
        relative = Path(directory).relative_to(root)
        depth = len(relative.parts)
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if depth < MANIFEST_DEPTH
            and not name.startswith(".")
            and name not in SKIPPED_DIRECTORIES
        )
        paths.extend((relative / name).as_posix() for name in sorted(names))
    return paths, "files on disk"


def _layout(root: Path, files: list[str], files_evidence: str) -> Layout:
    manifests: list[Manifest] = []
    verify: list[VerifyCommand] = []
    for path in files:
        name = path.rpartition("/")[2]
        if name not in MANIFEST_NAMES or path.split("/")[0] in SKIPPED_DIRECTORIES:
            continue
        manifest, commands = _read_manifest(root, path)
        manifests.append(manifest)
        verify.extend(commands)
    ci = _ci_files(root)
    signals, projects = _monorepo(root, files, manifests)
    return Layout(
        manifests=Fact(tuple(manifests), f"root and two levels down, {files_evidence}"),
        ci=Fact(ci, ".github/workflows, .gitlab-ci.yml, Jenkinsfile, .circleci"),
        verify=Fact(
            tuple(verify),
            f"scripts and targets named {'/'.join(VERIFY_NAMES)} in the manifests",
        ),
        monorepo_signals=Fact(
            signals, f"manifests, compose files, README, {files_evidence}"
        ),
        projects=Fact(
            projects, "directories and sibling repositories the signals name"
        ),
    )


def _read_manifest(root: Path, path: str) -> tuple[Manifest, list[VerifyCommand]]:
    """One manifest and the verify commands it names; unreadable means none."""
    file = root / path
    directory = path.rpartition("/")[0] or "."
    name = path.rpartition("/")[2]
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return Manifest(path, name), []

    def commands(
        names: Iterable[str], argv: Callable[[str], tuple[str, ...]]
    ) -> list[VerifyCommand]:
        return [
            VerifyCommand(task, argv(each), directory, path)
            for each in names
            if (task := _verify_name(each))
        ]

    if name == "package.json":
        data = _json_object(text)
        scripts = (
            tuple(data.get("scripts") or {})
            if isinstance(data.get("scripts"), dict)
            else ()
        )
        workspaces = data.get("workspaces")
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("packages")
        globs = (
            tuple(str(each) for each in workspaces)
            if isinstance(workspaces, list)
            else ()
        )
        runner = _node_runner(root, directory)
        return Manifest(path, name, scripts, globs), commands(
            scripts, lambda script: (runner, "run", script)
        )
    if name == "composer.json":
        data = _json_object(text)
        raw = data.get("scripts")
        scripts = tuple(raw) if isinstance(raw, dict) else ()
        return Manifest(path, name, scripts), commands(
            scripts, lambda script: ("composer", "run", script)
        )
    if name == "Makefile":
        targets = makefile_targets(text)
        return Manifest(path, name, targets), commands(
            (target for target in targets if target in VERIFY_NAMES),
            lambda target: ("make", target),
        )
    if name == "pyproject.toml":
        return Manifest(path, name), _python_commands(text, directory, path)
    toolchain = {
        "go.mod": (
            ("test", ("go", "test", "./...")),
            ("lint", ("go", "vet", "./...")),
            ("build", ("go", "build", "./...")),
        ),
        "Cargo.toml": (
            ("test", ("cargo", "test")),
            ("lint", ("cargo", "clippy")),
            ("format", ("cargo", "fmt", "--check")),
            ("build", ("cargo", "build")),
        ),
        "pom.xml": (("test", ("mvn", "test")),),
    }
    gradle = "./gradlew" if (root / directory / "gradlew").exists() else "gradle"
    toolchain["build.gradle"] = (("test", (gradle, "test")),)
    toolchain["build.gradle.kts"] = toolchain["build.gradle"]
    return Manifest(path, name), [
        VerifyCommand(task, argv, directory, f"{path} (toolchain default)")
        for task, argv in toolchain.get(name, ())
    ]


def _verify_name(script: str) -> str | None:
    """A script's verify name: "test" for "test" or "test:unit"."""
    head = script.partition(":")[0]
    return head if head in VERIFY_NAMES else None


def _json_object(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _node_runner(root: Path, directory: str) -> str:
    """The package manager a lock file names, in the package or at the root."""
    for place in dict.fromkeys((root / directory, root)):
        for lock, runner in (
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("bun.lockb", "bun"),
            ("bun.lock", "bun"),
        ):
            if (place / lock).exists():
                return runner
    return "npm"


def _python_commands(text: str, directory: str, path: str) -> list[VerifyCommand]:
    """Commands from pyproject's tool tables and script runners."""
    if tomllib is None:
        return []
    try:
        tools = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError:
        return []
    found = [
        VerifyCommand(task, argv, directory, f"{path} [tool.{tool}]")
        for tool, task, argv in (
            ("pytest", "test", ("python", "-m", "pytest")),
            ("ruff", "lint", ("ruff", "check", ".")),
            ("mypy", "typecheck", ("mypy",)),
            ("black", "format", ("black", "--check", ".")),
        )
        if tool in tools
    ]
    scripts = {
        "hatch": tools.get("hatch", {})
        .get("envs", {})
        .get("default", {})
        .get("scripts"),
        "pdm": tools.get("pdm", {}).get("scripts"),
    }
    for runner, table in scripts.items():
        if isinstance(table, dict):
            found.extend(
                VerifyCommand(
                    name, (runner, "run", script), directory, f"{path} [tool.{runner}]"
                )
                for script in table
                if (name := _verify_name(script))
            )
    return found


def _ci_files(root: Path) -> tuple[str, ...]:
    paths = [
        path
        for pattern in (".github/workflows/*.yml", ".github/workflows/*.yaml")
        for path in root.glob(pattern)
    ]
    paths.extend(
        root / name
        for name in (".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml")
        if (root / name).exists()
    )
    return tuple(sorted(path.relative_to(root).as_posix() for path in paths))


def _monorepo(
    root: Path, files: list[str], manifests: list[Manifest]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Signals of several projects in or beside this checkout, and their names.

    Candidates are the directories holding manifests, when there are at least
    two of them, a workspace list, or an apps/, packages/ or services/
    directory; plus the build directories compose files name and the sibling
    repositories the README links to, which are named and never read.
    """
    signals: list[str] = []
    candidates: list[str] = []
    directories = sorted(
        {m.path.rpartition("/")[0] for m in manifests if "/" in m.path}
    )
    grouped = sorted(
        {
            d.split("/")[0]
            for d in directories
            if d.split("/")[0] in MONOREPO_DIRECTORIES
        }
    )
    workspaces = [m for m in manifests if m.workspaces]
    if len(directories) >= MONOREPO_MIN_DIRECTORIES:
        signals.append(f"{len(directories)} directories with manifests")
    for manifest in workspaces:
        signals.append(
            f"workspaces in {manifest.path}: {', '.join(manifest.workspaces)}"
        )
    signals.extend(f"{name}/ holds manifests" for name in grouped)
    if signals:
        candidates.extend(directories)
    for path in files:
        if "/" in path or not COMPOSE_FILE.match(path):
            continue
        services, builds = _compose_services(root / path)
        if services:
            signals.append(f"{path} services: {', '.join(services)}")
            candidates.extend(builds)
    siblings = _readme_siblings(root, files)
    if siblings:
        signals.append(f"README links to sibling repositories: {', '.join(siblings)}")
        candidates.extend(f"../{name}" for name in siblings)
    return tuple(signals), tuple(dict.fromkeys(candidates))


def _compose_services(path: Path) -> tuple[list[str], list[str]]:
    """Service names, and the local build directories that exist."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return [], []
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return [], []
    builds = []
    for service in services.values():
        build = service.get("build") if isinstance(service, dict) else None
        if isinstance(build, dict):
            build = build.get("context")
        if isinstance(build, str):
            directory = (path.parent / build).resolve()
            if directory.is_dir() and directory.is_relative_to(path.parent.resolve()):
                relative = directory.relative_to(path.parent.resolve()).as_posix()
                if relative != ".":
                    builds.append(relative)
    return [str(name) for name in services], builds


def _readme_siblings(root: Path, files: list[str]) -> tuple[str, ...]:
    names: list[str] = []
    for path in files:
        if "/" in path or not path.lower().startswith("readme"):
            continue
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        names.extend(SIBLING_LINK.findall(text))
        names.extend(GIT_URL.findall(text))
    # A README usually shows how to clone this very repository; that is not a
    # sibling.
    own = {root.name}
    for line in _lines(_git(root, "remote", "-v")):
        url = line.split()[1] if len(line.split()) > 1 else ""
        own.add(url.rstrip("/").rpartition("/")[2].removesuffix(".git"))
    return tuple(name for name in dict.fromkeys(names) if name not in own)


def render_markdown(profile: Profile) -> str:
    """The profile as short sections, one fact per line, evidence in parentheses."""
    lines = [
        f"# Profile of {Path(profile.root).name}",
        "",
        "Read-only facts about this checkout; each names where it came from.",
    ]
    if profile.git_unavailable is not None:
        lines += ["", f"Git facts are unavailable: {profile.git_unavailable}."]
    if profile.repository is not None:
        repository = profile.repository
        lines += _section(
            "Repository",
            ("Default branch", repository.default_branch, str),
            ("Integration branch", repository.integration_branch, str),
            ("Branch patterns", repository.branch_patterns, _counts),
            ("Remotes", repository.remotes, ", ".join),
            ("Shallow clone", repository.shallow, _yes_no),
            ("Merge commits", repository.merge_share, _percent),
            ("Merge style", repository.merge_style, str),
            ("PR signals", repository.pr_signals, "; ".join),
            ("Tags", repository.tags, str),
            ("Days between tags", repository.tag_interval_days, _number),
        )
    if profile.activity is not None:
        activity = profile.activity
        lines += _section(
            "Activity",
            ("Commits read", activity.commits, str),
            ("Contributors", activity.contributors, str),
            ("Active in 90 days", activity.active_contributors, str),
            ("Team shape", activity.team_shape, str),
            ("Commits per week", activity.weekly_commits, _weeks),
            ("Cadence", activity.cadence, str),
            ("Files per commit (median)", activity.files_per_commit, _number),
            ("Lines per commit (median)", activity.lines_per_commit, _number),
            (
                "Branch lifetime in days (median)",
                activity.branch_lifetime_days,
                _number,
            ),
        )
    if profile.fixes is not None:
        fixes = profile.fixes
        lines += _section(
            "Fixes",
            ("Fix share", fixes.share, _percent),
            ("Paths fixes touch", fixes.paths, _counts),
            ("Recent fixes", fixes.recent, _quoted),
        )
    if profile.hot_paths is not None:
        lines += _section(
            "Hot paths", ("Most changed", profile.hot_paths.paths, _counts)
        )
    layout = profile.layout
    lines += _section(
        "Layout",
        ("Manifests", layout.manifests, _manifests),
        ("CI", layout.ci, ", ".join),
        ("Verify commands", layout.verify, _verify),
        ("Monorepo signals", layout.monorepo_signals, "; ".join),
        ("Candidate projects", layout.projects, ", ".join),
    )
    conventions = profile.conventions
    lines += _section(
        "Conventions",
        ("Ticket prefixes", conventions.ticket_prefixes, _counts),
        ("task_format candidate", conventions.task_format, _code),
        ("Subjects led by a ticket key", conventions.ticket_share, _percent),
        ("Conventional-commit subjects", conventions.conventional_share, _percent),
        ("Subject convention", conventions.subject_convention, str),
        ("commit_format candidate", conventions.commit_format, _code),
        ("Agent instruction files", conventions.agent_files, ", ".join),
    )
    return "\n".join(lines) + "\n"


def _section(
    title: str, *facts: tuple[str, Fact[Any], Callable[[Any], str]]
) -> list[str]:
    lines = ["", f"## {title}", ""]
    for label, fact, show in facts:
        if fact.value is None:
            value = "not found"
        elif isinstance(fact.value, tuple) and not fact.value:
            value = "none"
        else:
            value = show(fact.value)
        lines.append(f"- {label}: {value} ({fact.evidence})")
    return lines


def _counts(counts: tuple[Count, ...]) -> str:
    return ", ".join(f"{count.name} {count.count}" for count in counts)


def _percent(share: float) -> str:
    return f"{share:.0%}"


def _number(value: float) -> str:
    return f"{value:g}"


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _weeks(weekly: tuple[int, ...]) -> str:
    return " ".join(str(count) for count in weekly) + " (oldest first)"


def _quoted(subjects: tuple[str, ...]) -> str:
    return "; ".join(f'"{subject}"' for subject in subjects)


def _code(text: str) -> str:
    return f"`{text}`"


def _manifests(manifests: tuple[Manifest, ...]) -> str:
    shown = []
    for manifest in manifests:
        details = []
        if manifest.scripts:
            details.append(f"{len(manifest.scripts)} scripts")
        if manifest.workspaces:
            details.append("workspaces")
        shown.append(manifest.path + (f" [{', '.join(details)}]" if details else ""))
    return ", ".join(shown)


def _verify(commands: tuple[VerifyCommand, ...]) -> str:
    return "; ".join(
        f"{command.name}: `{' '.join(command.argv)}`"
        + ("" if command.directory == "." else f" in {command.directory}")
        + f" from {command.source}"
        for command in commands
    )
