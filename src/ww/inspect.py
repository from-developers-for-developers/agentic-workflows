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
import math
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, NamedTuple

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
# Hash, parents, author e-mail, author time and subject, as LOG_FORMAT asks.
LOG_FIELDS = 5
MAX_LISTED = 200_000
LOG_FORMAT = "--format=%x1e%H%x1f%P%x1f%ae%x1f%at%x1f%s"

VERIFY_NAMES = ("test", "lint", "typecheck", "check", "format", "build")
# Every manifest is listed; package.json, composer.json, Makefile and
# pyproject.toml also name the verify commands.
MANIFEST_NAMES = frozenset(
    {"package.json", "pyproject.toml", "Makefile", "composer.json", "go.mod"}
    | {"Cargo.toml", "Gemfile", "pom.xml", "build.gradle", "build.gradle.kts"}
)
PYTHON_TOOLS = (
    ("pytest", "test", ("python", "-m", "pytest")),
    ("ruff", "lint", ("ruff", "check", ".")),
    ("mypy", "typecheck", ("mypy",)),
)
NODE_LOCKS = (
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("bun.lockb", "bun"),
    ("bun.lock", "bun"),
)
MONOREPO_DIRECTORIES = ("apps", "packages", "services")
SKIPPED_DIRECTORIES = frozenset(
    {"node_modules", "vendor", "dist", "build", "target", "venv", "__pycache__"}
)
AGENT_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".codex",
    ".claude",
)
AGENT_FILES += (".cursor/rules*", ".cursorrules")
PR_FILES = [
    f"{directory}{name}"
    for directory in (".github/", "", "docs/")
    for name in ("PULL_REQUEST_TEMPLATE*", "pull_request_template*", "CODEOWNERS")
]
CI_FILES: tuple[str, ...] = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".gitlab-ci.yml",
)
CI_FILES += ("Jenkinsfile", ".circleci/config.yml")

# A subject that reports a fix: it starts, after an optional tracker key, with
# fix, fixes, fixed, hotfix or revert (so "fix:" and "fix(cli):" too), or it
# names a regression, e.g. "Fixed the totals", "PROJ-3: fix the crash" or
# "Guard the cache regression"; "Add the fix loop" does not match.
FIX_SUBJECT = re.compile(
    r"^(?:\[?[A-Z][A-Z0-9]+-\d+\]?:?\s+)?(?:fix|fixes|fixed|hotfix|revert)\b"
    r"|\bregression\b",
    re.I,
)
FIX_RULE = (
    "subject starts with fix, fixes, fixed, hotfix or revert, or names a regression"
)
# A tracker key such as an issue ID, upper case in a subject, e.g. "PROJ-12".
TICKET_KEY = re.compile(r"\b([A-Z][A-Z0-9]+)-\d+\b")
# The same key in a branch name, in any case, e.g. "feature/proj-12" gives "proj".
BRANCH_TICKET_KEY = re.compile(r"\b([A-Za-z][A-Za-z0-9]+)-\d+\b")
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


class Fact(NamedTuple):
    """One observation and where it came from; a ``value`` of ``None`` is not found."""

    value: Any
    evidence: str


Section = dict[str, Fact]


class Count(NamedTuple):
    name: str
    times: int


class Manifest(NamedTuple):
    path: str
    scripts: tuple[str, ...] = ()
    workspaces: tuple[str, ...] = ()


class VerifyCommand(NamedTuple):
    """A command a manifest names for checking the code, as exact argv."""

    name: str
    argv: tuple[str, ...]
    directory: str
    source: str


class Commit(NamedTuple):
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


@dataclass(frozen=True)
class Profile:
    """Everything ``ww inspect`` found, by section; Git sections need Git."""

    root: str
    commits_window: int
    git_unavailable: str | None
    sections: dict[str, Section]

    def value(self, section: str, key: str) -> Any:
        return self.sections[section][key].value

    def to_dict(self) -> dict[str, Any]:
        """The profile as JSON-ready data; each fact is its value and evidence."""
        plain: dict[str, Any] = _plain(vars(self))
        return plain


def _plain(value: Any) -> Any:
    if hasattr(value, "_asdict"):
        value = value._asdict()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _counts(counts: tuple[Count, ...]) -> str:
    return ", ".join(f"{count.name} {count.times}" for count in counts)


def _manifests(manifests: tuple[Manifest, ...]) -> str:
    shown = []
    for manifest in manifests:
        details = [f"{len(manifest.scripts)} scripts"] if manifest.scripts else []
        details += ["workspaces"] if manifest.workspaces else []
        shown.append(manifest.path + (f" [{', '.join(details)}]" if details else ""))
    return ", ".join(shown)


def _verify(commands: tuple[VerifyCommand, ...]) -> str:
    return "; ".join(
        f"{command.name}: `{' '.join(command.argv)}`"
        + ("" if command.directory == "." else f" in {command.directory}")
        + f" from {command.source}"
        for command in commands
    )


_percent = "{:.0%}".format
_number = "{:g}".format
_code = "`{}`".format
_comma = ", ".join
_semicolon = "; ".join

# Every fact of each section, in page order: its label and how its value reads.
# The section's title is its key with spaces, e.g. "Hot paths".
LABELS: dict[str, dict[str, tuple[str, Callable[[Any], str]]]] = {
    "repository": {
        "default_branch": ("Default branch", str),
        "integration_branch": ("Integration branch", str),
        "branch_patterns": ("Branch patterns", _counts),
        "remotes": ("Remotes", _comma),
        "shallow": ("Shallow clone", lambda shallow: "yes" if shallow else "no"),
        "merge_share": ("Merge commits", _percent),
        "merge_style": ("Merge style", str),
        "pr_signals": ("PR signals", _semicolon),
        "tags": ("Tags", str),
        "tag_interval_days": ("Days between tags", _number),
    },
    "activity": {
        "commits": ("Commits read", str),
        "contributors": ("Contributors", str),
        "active_contributors": ("Active in 90 days", str),
        "team_shape": ("Team shape", str),
        "weekly_commits": (
            "Commits per week",
            lambda weekly: " ".join(map(str, weekly)) + " (oldest first)",
        ),
        "cadence": ("Cadence", str),
        "files_per_commit": ("Files per commit (median)", _number),
        "lines_per_commit": ("Lines per commit (median)", _number),
        "branch_lifetime_days": ("Branch lifetime in days (median)", _number),
    },
    "fixes": {
        "share": ("Fix share", _percent),
        "paths": ("Paths fixes touch", _counts),
        "recent": (
            "Recent fixes",
            lambda subjects: _semicolon(f'"{s}"' for s in subjects),
        ),
    },
    "hot_paths": {"most_changed": ("Most changed", _counts)},
    "layout": {
        "manifests": ("Manifests", _manifests),
        "ci": ("CI", _comma),
        "verify": ("Verify commands", _verify),
        "monorepo_signals": ("Monorepo signals", _semicolon),
        "projects": ("Candidate projects", _comma),
    },
    "conventions": {
        "ticket_prefixes": ("Ticket prefixes", _counts),
        "task_format": ("task_format candidate", _code),
        "ticket_share": ("Subjects led by a ticket key", _percent),
        "conventional_share": ("Conventional-commit subjects", _percent),
        "subject_convention": ("Subject convention", str),
        "commit_format": ("commit_format candidate", _code),
        "agent_files": ("Agent instruction files", _comma),
    },
}


def _section(name: str, unknown: str = "", **found: tuple[Any, str]) -> Section:
    """A section from ``key=(value, evidence)``; a key left out is not found."""
    return {key: Fact(*found.get(key, (None, unknown))) for key in LABELS[name]}


def _git(root: Path, *arguments: str) -> str | None:
    """Run one read-only Git command; ``None`` when it fails or times out."""
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
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


def _lines(output: str | None) -> list[str]:
    return [line for line in (output or "").splitlines() if line.strip()][:MAX_LISTED]


def _ref_exists(root: Path, ref: str) -> bool:
    return _git(root, "rev-parse", "--verify", "--quiet", ref) is not None


def _read(path: Path) -> str:
    """The file's text; empty when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _present(root: Path, patterns: Iterable[str]) -> tuple[str, ...]:
    """The paths under ``root`` the glob patterns name, each once, in order."""
    paths = (path for pattern in patterns for path in sorted(root.glob(pattern)))
    return tuple(dict.fromkeys(path.relative_to(root).as_posix() for path in paths))


def parse_log(output: str) -> list[Commit]:
    """Read ``git log --numstat`` output in :data:`LOG_FORMAT`."""
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
            if path:
                numeric = added.isdigit() and deleted.isdigit()
                changes.append((path, int(added) + int(deleted) if numeric else 0))
        stamp = int(timestamp) if timestamp.isdigit() else 0
        heads = tuple(parents.split())
        commits.append(
            Commit(sha, heads, author.lower(), stamp, subject, tuple(changes))
        )
    return commits


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


def history_weeks(timestamps: Iterable[int], now: float) -> int:
    """The weeks from the oldest commit read to now: at least one, at most 12.

    A young history is measured over the weeks it has, so a week of daily
    commits reads as daily rather than as one busy week among eleven empty ones.
    """
    oldest = min(timestamps, default=now)
    return min(CADENCE_WEEKS, max(1, math.ceil((now - oldest) / WEEK_SECONDS)))


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
    return tuple(Count(*pair) for pair in Counter(names).most_common(limit))


def ticket_prefixes(
    subjects: Iterable[str], branches: Iterable[str] = ()
) -> tuple[Count, ...]:
    """Upper-case tracker key prefixes, counted once per subject or branch name.

    Subjects count only upper-case keys; branch names, often lower case, count
    keys in any case.
    """
    seen: list[str] = []
    for subject in subjects:
        seen.extend(dict.fromkeys(TICKET_KEY.findall(subject)))
    for branch in branches:
        seen.extend(dict.fromkeys(k.upper() for k in BRANCH_TICKET_KEY.findall(branch)))
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


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _median(values: Sequence[float]) -> float | None:
    return round(float(statistics.median(values)), 1) if values else None


def inspect_checkout(root: Path, commits: int = DEFAULT_COMMITS) -> Profile:
    """Profile the checkout at ``root`` from at most ``commits`` commits."""
    root = root.resolve()
    files, files_evidence = _tracked_files(root)
    layout = _layout(root, files, files_evidence)
    if _git(root, "rev-parse", "--is-inside-work-tree") is None:
        conventions = _conventions(root, [], [], "no Git history")
        unavailable = "not a Git repository (git rev-parse)"
        sections = {"layout": layout, "conventions": conventions}
        return Profile(str(root), commits, unavailable, sections)
    history: list[Commit] = []
    if _ref_exists(root, "HEAD"):
        log = _git(root, "log", f"-n{commits}", "--no-renames", "--numstat", LOG_FORMAT)
        history = parse_log(log or "")
    window = f"git log -n {commits}"
    branches, branches_evidence = _branch_names(root, history)
    hot = top_counts((path for c in history for path, _ in c.changes), TOP_HOT_PATHS)
    sections = {
        "repository": _repository(root, history, window, branches, branches_evidence),
        "activity": _activity(root, history, window),
        "fixes": _fixes(history, window),
        "hot_paths": _section(
            "hot_paths", most_changed=(hot if history else None, f"{window} --numstat")
        ),
        "layout": layout,
        "conventions": _conventions(
            root, history, branches, f"{window}; {branches_evidence}"
        ),
    }
    return Profile(str(root), commits, None, sections)


def _branch_names(root: Path, history: list[Commit]) -> tuple[list[str], str]:
    """Branch names without their remote, plus those merge subjects name."""
    names = [name.partition("/")[2] for name in _refs(root, "refs/remotes")]
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
    lines = (line.partition("\t") for line in _lines(output))
    return [name for name, _, symref in lines if not symref]


def _repository(
    root: Path, history: list[Commit], window: str, branches: list[str], evidence: str
) -> Section:
    default = _default_branch(root)
    merges = sum(commit.is_merge for commit in history)
    merge_share = _share(merges, len(history))
    style = None
    if merge_share is not None:
        linear = merge_share < MERGE_STYLE_SHARE
        style = "linear (rebase or squash)" if linear else "merge commits"
    # A lane is a branch's first path segment with its slash, e.g. "feature/".
    lanes = [f"{name.partition('/')[0]}/" for name in branches if "/" in name[1:]]
    shallow = _git(root, "rev-parse", "--is-shallow-repository")
    pulls = sum(commit.subject.startswith("Merge pull request") for commit in history)
    pr_signals = _present(root, PR_FILES)
    if pulls:
        pr_signals += (f'{pulls} "Merge pull request" subjects',)
    tags = "--format=%(creatordate:unix)"
    stamps = _lines(
        _git(root, "for-each-ref", "--sort=-creatordate", tags, "refs/tags")
    )[:TAG_GAPS_FROM]
    gaps = [(int(new) - int(old)) / DAY_SECONDS for new, old in pairwise(stamps)]
    return _section(
        "repository",
        default_branch=default,
        integration_branch=_integration_branch(root, default[0]),
        branch_patterns=(top_counts(lanes, len(lanes) or 1), evidence),
        remotes=(tuple(_lines(_git(root, "remote"))), "git remote"),
        shallow=(
            None if shallow is None else shallow.strip() == "true",
            "git rev-parse --is-shallow-repository",
        ),
        merge_share=(merge_share, f"{merges} of {len(history)} commits, {window}"),
        merge_style=(style, f"merge share against {MERGE_STYLE_SHARE:.0%}, {window}"),
        pr_signals=(pr_signals, f"files and subjects, {window}"),
        tags=(len(_lines(_git(root, "tag", "--list"))), "git tag --list"),
        tag_interval_days=(
            _median(gaps),
            f"last {TAG_GAPS_FROM} tags, git for-each-ref",
        ),
    )


def _default_branch(root: Path) -> tuple[str | None, str]:
    head = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head.strip().partition("/")[2], "git symbolic-ref origin/HEAD"
    for name in ("main", "master"):
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            if _ref_exists(root, ref):
                return name, f"{ref} exists"
    current = _git(root, "symbolic-ref", "--short", "HEAD")
    if current:
        return current.strip(), "current branch, git symbolic-ref HEAD"
    return None, "origin/HEAD, main, master"


def _integration_branch(root: Path, default: str | None) -> tuple[str | None, str]:
    """``dev`` or ``develop`` when merges land on it, else the default branch."""
    for name in ("dev", "develop"):
        for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"):
            merged = _git(
                root, "log", "-n", "1", "--first-parent", "--merges", "--format=%H", ref
            )
            if merged and merged.strip():
                return name, f"merges on {ref}, git log --first-parent --merges"
    return default, "no dev or develop receiving merges; the default branch"


def _activity(root: Path, history: list[Commit], window: str) -> Section:
    if not history:
        return _section("activity", window, commits=(0, window))
    now = time.time()
    stamps = [commit.timestamp for commit in history]
    recent = now - ACTIVE_DAYS * DAY_SECONDS
    active = len({commit.author for commit in history if commit.timestamp >= recent})
    weeks = history_weeks(stamps, now)
    weekly = weekly_commits(stamps, now, weeks)
    span = f"the {weeks} week{'s' * (weeks != 1)} the history spans, at most 12"
    changes = [commit for commit in history if not commit.is_merge]
    numstat = f"non-merge commits, {window} --numstat"
    return _section(
        "activity",
        commits=(len(history), window),
        contributors=(
            len({commit.author for commit in history}),
            f"author e-mails, {window}",
        ),
        active_contributors=(active, f"authors in {ACTIVE_DAYS} days, {window}"),
        team_shape=(
            team_shape(active),
            f"{active} active in {ACTIVE_DAYS} days: solo 1, small 2-5, team 6+",
        ),
        weekly_commits=(weekly, f"{span}, {window}"),
        cadence=(
            cadence(weekly),
            f"median {statistics.median(weekly):g}/week over {span}: "
            "daily 5+, weekly 1+",
        ),
        files_per_commit=(_median([len(c.changes) for c in changes]), numstat),
        lines_per_commit=(
            _median([sum(lines for _, lines in c.changes) for c in changes]),
            numstat,
        ),
        branch_lifetime_days=(
            _branch_lifetime(root, history),
            f"first commit to merge, last {LIFETIME_MERGES} merges, git log P1..P2",
        ),
    )


def _branch_lifetime(root: Path, history: list[Commit]) -> float | None:
    """Median days from a merged branch's first own commit to its merge."""
    lifetimes = []
    for merge in [commit for commit in history if commit.is_merge][:LIFETIME_MERGES]:
        branch = f"{merge.parents[0]}..{merge.parents[1]}"
        output = _git(root, "log", f"-n{LIFETIME_COMMITS}", "--format=%at", branch)
        stamps = [int(line) for line in _lines(output) if line.isdigit()]
        if stamps:
            lifetimes.append(max(0, merge.timestamp - min(stamps)) / DAY_SECONDS)
    return _median(lifetimes)


def _fixes(history: list[Commit], window: str) -> Section:
    # Merge subjects name branches ("Merge branch 'hotfix/x'"), not fixes, so
    # the share is over the commits that change something themselves.
    changes = [commit for commit in history if not commit.is_merge]
    fixes = [commit for commit in changes if is_fix(commit.subject)]
    rule = f"{len(fixes)} of {len(changes)} non-merge commits, {FIX_RULE}, {window}"
    if not changes:
        return _section("fixes", window, share=(None, rule))
    paths = top_counts((path for c in fixes for path, _ in c.changes), TOP_FIX_PATHS)
    return _section(
        "fixes",
        share=(_share(len(fixes), len(changes)), rule),
        paths=(paths, f"paths the fix commits touch, {window} --numstat"),
        recent=(tuple(commit.subject for commit in fixes[:RECENT_FIXES]), window),
    )


def _conventions(
    root: Path, history: list[Commit], branches: list[str], evidence: str
) -> Section:
    subjects = [commit.subject for commit in history if not commit.is_merge]
    prefixes = ticket_prefixes(subjects, branches)
    tickets = sum(TICKET_SUBJECT.match(subject) is not None for subject in subjects)
    conventional = sum(bool(CONVENTIONAL_SUBJECT.match(s)) for s in subjects)
    ticket_share = _share(tickets, len(subjects))
    conventional_share = _share(conventional, len(subjects))
    shares = f"{tickets} ticket-led and {conventional} conventional of {len(subjects)}"
    shares += " non-merge subjects"
    convention = commit_format = None
    if ticket_share is not None:
        convention = subject_convention(ticket_share, conventional_share or 0.0)
        commit_format = commit_format_for(ticket_share)
    task_format = f"{prefixes[0].name}-{{{{digit}}}}" if prefixes else None
    return _section(
        "conventions",
        ticket_prefixes=(prefixes if history or branches else None, evidence),
        task_format=(task_format, "the most frequent tracker key prefix"),
        ticket_share=(ticket_share, shares),
        conventional_share=(conventional_share, shares),
        subject_convention=(
            convention,
            "the convention half the subjects follow, else free",
        ),
        commit_format=(
            commit_format,
            "task ID first when half the subjects start with a key",
        ),
        agent_files=(_present(root, AGENT_FILES), "files at the project root"),
    )


def _tracked_files(root: Path) -> tuple[list[str], str]:
    """Paths at most two directories deep: Git's tracked files, else a walk."""
    output = _git(root, "ls-files")
    if output is not None and output.strip():
        paths = [line for line in _lines(output) if line.count("/") <= MANIFEST_DEPTH]
        return paths, "git ls-files"
    paths = []
    for directory, subdirectories, names in os.walk(root):
        relative = Path(directory).relative_to(root)
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if len(relative.parts) < MANIFEST_DEPTH
            and not name.startswith(".")
            and name not in SKIPPED_DIRECTORIES
        )
        paths.extend((relative / name).as_posix() for name in sorted(names))
    return paths, "files on disk"


def _layout(root: Path, files: list[str], files_evidence: str) -> Section:
    manifests: list[Manifest] = []
    verify: list[VerifyCommand] = []
    for path in files:
        name = path.rpartition("/")[2]
        if name in MANIFEST_NAMES and path.split("/")[0] not in SKIPPED_DIRECTORIES:
            manifest, commands = _read_manifest(root, path)
            manifests.append(manifest)
            verify.extend(commands)
    signals, projects = _monorepo(root, files, manifests)
    return _section(
        "layout",
        manifests=(tuple(manifests), f"root and two levels down, {files_evidence}"),
        ci=(
            tuple(sorted(_present(root, CI_FILES))),
            ".github/workflows, .gitlab-ci.yml, Jenkinsfile, .circleci",
        ),
        verify=(
            tuple(verify),
            f"scripts and targets named {'/'.join(VERIFY_NAMES)} in the manifests",
        ),
        monorepo_signals=(
            signals,
            f"manifests, compose files, README, {files_evidence}",
        ),
        projects=(projects, "directories and sibling repositories the signals name"),
    )


def _read_manifest(root: Path, path: str) -> tuple[Manifest, list[VerifyCommand]]:
    """One manifest and the verify commands it names; unreadable means none."""
    directory, _, name = path.rpartition("/")
    directory = directory or "."
    text = _read(root / path)
    if name in ("package.json", "composer.json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        data = data if isinstance(data, dict) else {}
        scripts = (
            tuple(data["scripts"]) if isinstance(data.get("scripts"), dict) else ()
        )
        listed = data.get("workspaces")
        if isinstance(listed, dict):
            listed = listed.get("packages")
        workspaces = tuple(map(str, listed)) if isinstance(listed, list) else ()
        runner = (
            "composer" if name == "composer.json" else _node_runner(root, directory)
        )
        commands = [
            VerifyCommand(verb, (runner, "run", script), directory, path)
            for script in scripts
            if (verb := script.partition(":")[0]) in VERIFY_NAMES
        ]
        return Manifest(path, scripts, workspaces), commands
    if name == "Makefile":
        targets = tuple(dict.fromkeys(MAKE_TARGET.findall(text)))
        return Manifest(path, targets), [
            VerifyCommand(target, ("make", target), directory, path)
            for target in targets
            if target in VERIFY_NAMES
        ]
    tools: Any = {}
    if name == "pyproject.toml" and tomllib is not None:
        try:
            tools = tomllib.loads(text).get("tool", {})
        except tomllib.TOMLDecodeError:
            tools = {}
    return Manifest(path), [
        VerifyCommand(verb, argv, directory, f"{path} [tool.{tool}]")
        for tool, verb, argv in PYTHON_TOOLS
        if isinstance(tools, dict) and tool in tools
    ]


def _node_runner(root: Path, directory: str) -> str:
    """The package manager a lock file names, in the package or at the root."""
    for place in dict.fromkeys((root / directory, root)):
        for lock, runner in NODE_LOCKS:
            if (place / lock).exists():
                return runner
    return "npm"


def _monorepo(
    root: Path, files: list[str], manifests: list[Manifest]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Signals of several projects in or beside this checkout, and their names.

    Candidates are the directories holding manifests, when there are at least
    two of them, a workspace list, or an apps/, packages/ or services/
    directory; plus the build directories compose files name and the sibling
    repositories the README links to, which are named and never read.
    """
    directories = sorted(
        {m.path.rpartition("/")[0] for m in manifests if "/" in m.path}
    )
    signals: list[str] = []
    if len(directories) >= MONOREPO_MIN_DIRECTORIES:
        signals.append(f"{len(directories)} directories with manifests")
    signals.extend(
        f"workspaces in {m.path}: {', '.join(m.workspaces)}"
        for m in manifests
        if m.workspaces
    )
    signals.extend(
        f"{name}/ holds manifests"
        for name in sorted({d.split("/")[0] for d in directories})
        if name in MONOREPO_DIRECTORIES
    )
    candidates = list(directories) if signals else []
    for path in files:
        if "/" not in path and COMPOSE_FILE.match(path):
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
    """Service names, and the build directories inside the checkout that exist."""
    try:
        data = yaml.safe_load(_read(path))
    except yaml.YAMLError:
        data = None
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return [], []
    base = path.parent.resolve()
    builds = []
    for service in services.values():
        build = service.get("build") if isinstance(service, dict) else None
        if isinstance(build, dict):
            build = build.get("context")
        directory = (base / build).resolve() if isinstance(build, str) else base
        if directory != base and directory.is_dir() and directory.is_relative_to(base):
            builds.append(directory.relative_to(base).as_posix())
    return [str(name) for name in services], builds


def _readme_siblings(root: Path, files: list[str]) -> tuple[str, ...]:
    names: list[str] = []
    for path in files:
        if "/" not in path and path.lower().startswith("readme"):
            text = _read(root / path)
            names += SIBLING_LINK.findall(text) + GIT_URL.findall(text)
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
    for name, section in profile.sections.items():
        lines += ["", f"## {name.replace('_', ' ').capitalize()}", ""]
        for key, (value, evidence) in section.items():
            label, show = LABELS[name][key]
            if value is None:
                text = "not found"
            elif isinstance(value, tuple) and not value:
                text = "none"
            else:
                text = show(value)
            lines.append(f"- {label}: {text} ({evidence})")
    return "\n".join(lines) + "\n"
