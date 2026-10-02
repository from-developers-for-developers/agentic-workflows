# SPDX-License-Identifier: GPL-3.0-or-later
"""Notice that the checkout this ww runs from is behind its remote.

ww is installed from a Git clone in editable mode, so "is there an update" is
a local question. The checkout is found from this package's own location, its
tracking branch is fetched, and the result is compared with the commit the
checkout is on. Nothing is reported anywhere and no service of the project's
is contacted: the only network call is a ``git fetch`` against the remote the
user cloned from, and it is skipped entirely when the check is turned off.

The notice is announced, never enforced. It is written before the command's
own output and recorded as seen, so it appears once rather than on every
invocation; ``ww updates`` reprints the last one, and ``ww updates --now``
looks again ahead of the interval.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ww.executable import ww_command

# One check a day. The state file records the last attempt, so ordinary
# invocations between two checks read a small JSON file and run no Git at all.
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
# A fetch against an unreachable remote must not hold a command open.
FETCH_TIMEOUT_SECONDS = 5.0
GIT_TIMEOUT_SECONDS = 5.0
# Enough of the changelog to judge whether to pull, not the whole diff.
MAX_ENTRIES = 6
MAX_ENTRY_LENGTH = 180
CHANGELOG = "CHANGELOG.md"
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class UpdateNotice:
    """What one update announcement says."""

    checkout: Path
    remote_ref: str
    commit: str
    commits_behind: int
    entries: tuple[str, ...] = ()
    truncated: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkout": str(self.checkout),
            "remote_ref": self.remote_ref,
            "commit": self.commit,
            "commits_behind": self.commits_behind,
            "entries": list(self.entries),
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> UpdateNotice | None:
        try:
            return cls(
                Path(str(value["checkout"])),
                str(value["remote_ref"]),
                str(value["commit"]),
                int(value["commits_behind"]),
                tuple(str(entry) for entry in value.get("entries", ())),
                int(value.get("truncated", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def render(self) -> str:
        """Render the announcement an agent relays to the person running it."""
        commits = "commit" if self.commits_behind == 1 else "commits"
        lines = [
            "## A newer ww is available",
            "",
            f"This checkout is {self.commits_behind} {commits} behind "
            f"`{self.remote_ref}`.",
        ]
        if self.entries:
            lines += ["", "What changed:", ""]
            lines += [f"- {entry}" for entry in self.entries]
            if self.truncated:
                more = "entry" if self.truncated == 1 else "entries"
                lines.append(f"- ...and {self.truncated} more {more} in {CHANGELOG}.")
        lines += [
            "",
            "**Tell the person you are working for about this before you "
            "continue**, and let them decide whether to update. To update:",
            "",
            "```console",
            f"{ww_command()} upgrade",
            "```",
            "",
            "This notice is shown once. `ww updates` prints it again.",
            "",
            "---",
            "",
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class UpdateState:
    """What the last check found, kept per user rather than per project."""

    last_checked: datetime | None = None
    announced_commit: str = ""
    notice: UpdateNotice | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_checked": (
                self.last_checked.isoformat().replace("+00:00", "Z")
                if self.last_checked
                else None
            ),
            "announced_commit": self.announced_commit,
            "notice": self.notice.to_dict() if self.notice else None,
        }


def state_path() -> Path:
    """The per-user state file, following XDG when it is configured.

    The update concerns the ww installation, which every project on the
    machine shares, so the record of what was already announced does not
    belong under any one project's ``.ww``.
    """
    configured = os.environ.get("WW_STATE_HOME")
    if configured:
        return Path(configured) / "updates.json"
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "ww" / "updates.json"


def load_state(path: Path | None = None) -> UpdateState:
    """Read the saved state, treating anything unreadable as no state."""
    location = path or state_path()
    try:
        raw = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return UpdateState()
    if not isinstance(raw, dict):
        return UpdateState()
    return UpdateState(
        _parse_timestamp(raw.get("last_checked")),
        str(raw.get("announced_commit") or ""),
        (
            UpdateNotice.from_dict(raw["notice"])
            if isinstance(raw.get("notice"), dict)
            else None
        ),
    )


def save_state(state: UpdateState, path: Path | None = None) -> None:
    """Persist the state, silently accepting a read-only home directory."""
    location = path or state_path()
    try:
        location.parent.mkdir(parents=True, exist_ok=True)
        temporary = location.with_name(location.name + ".tmp")
        temporary.write_text(
            json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(location)
    except OSError:
        return


def installation_checkout() -> Path | None:
    """The Git checkout this ww was installed from, if it is one.

    An editable install resolves back to the clone; a wheel installed into
    site-packages does not, and then there is nothing to compare against.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / ".git").exists() and (
            candidate / "src/ww/updates.py"
        ).resolve() == here:
            return candidate
    return None


def check_enabled(update_check: bool = True) -> bool:
    """Whether to look at all, from the environment and project setting."""
    configured = os.environ.get("WW_UPDATE_CHECK")
    if configured is not None:
        return configured.strip().lower() not in FALSE_VALUES
    return update_check


def interval_seconds() -> float:
    """Seconds between checks, overridable for slower or faster cadence."""
    configured = os.environ.get("WW_UPDATE_CHECK_INTERVAL")
    if configured:
        try:
            return max(0.0, float(configured))
        except ValueError:
            return DEFAULT_INTERVAL_SECONDS
    return DEFAULT_INTERVAL_SECONDS


def pending_notice(
    checkout: Path | None,
    *,
    state_file: Path | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> UpdateNotice | None:
    """Return an update to announce, or ``None`` when there is nothing to say.

    Between checks this reads one small file and runs no Git. A notice already
    announced stays quiet unless ``force`` asks for it again.
    """
    moment = now or datetime.now(timezone.utc)
    state = load_state(state_file)
    if not force and not _due(state, moment):
        return state.notice if _unannounced(state) else None
    if checkout is None:
        return None
    notice = _look(checkout)
    save_state(UpdateState(moment, state.announced_commit, notice), state_file)
    if notice is None:
        return None
    if not force and state.announced_commit == notice.commit:
        return None
    return notice


def mark_announced(notice: UpdateNotice, *, state_file: Path | None = None) -> None:
    """Record that this notice was printed, so it is not repeated."""
    state = load_state(state_file)
    save_state(UpdateState(state.last_checked, notice.commit, notice), state_file)


def last_notice(*, state_file: Path | None = None) -> UpdateNotice | None:
    """The most recent notice, announced or not, for ``ww updates``."""
    return load_state(state_file).notice


def _due(state: UpdateState, now: datetime) -> bool:
    if state.last_checked is None:
        return True
    return now - state.last_checked >= timedelta(seconds=interval_seconds())


def _unannounced(state: UpdateState) -> bool:
    return state.notice is not None and state.announced_commit != state.notice.commit


def _look(checkout: Path) -> UpdateNotice | None:
    """Fetch and compare, returning what an announcement would say."""
    remote_ref = _tracking_ref(checkout)
    if remote_ref is None:
        return None
    remote_name = remote_ref.partition("/")[0]
    _git(checkout, "fetch", "--quiet", remote_name, timeout=FETCH_TIMEOUT_SECONDS)
    local = _git(checkout, "rev-parse", "HEAD")
    remote = _git(checkout, "rev-parse", remote_ref)
    if not local or not remote or local == remote:
        return None
    behind = _git(checkout, "rev-list", "--count", f"{local}..{remote}")
    if not behind or not behind.isdigit() or behind == "0":
        return None
    entries, truncated = _changes(checkout, local, remote)
    return UpdateNotice(checkout, remote_ref, remote, int(behind), entries, truncated)


def _tracking_ref(checkout: Path) -> str | None:
    """The upstream the checkout tracks, falling back to the default remote.

    Someone following ``dev`` should be told about ``dev``, not about ``main``.
    """
    upstream = _git(
        checkout, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    if upstream and "/" in upstream:
        return upstream
    for candidate in ("origin/main", "origin/master"):
        if _git(checkout, "rev-parse", "--verify", "--quiet", candidate):
            return candidate
    return None


def _changes(checkout: Path, local: str, remote: str) -> tuple[tuple[str, ...], int]:
    """Summarize the update from the changelog, or from commit subjects."""
    entries = _changelog_entries(checkout, local, remote)
    if not entries:
        entries = _commit_subjects(checkout, local, remote)
    if len(entries) > MAX_ENTRIES:
        return tuple(entries[:MAX_ENTRIES]), len(entries) - MAX_ENTRIES
    return tuple(entries), 0


def _changelog_entries(checkout: Path, local: str, remote: str) -> list[str]:
    """Bullets added to the changelog between the two commits.

    This is the summary the project already writes by hand, so there is no
    second place to keep an announcement up to date.
    """
    diff = _git(
        checkout,
        "diff",
        "--unified=0",
        f"{local}..{remote}",
        "--",
        CHANGELOG,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    entries: list[str] = []
    current: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        if body.startswith("- "):
            if current:
                entries.append(" ".join(current))
            current = [body[2:].strip()]
        elif current and body.startswith("  "):
            current.append(body.strip())
        elif current:
            entries.append(" ".join(current))
            current = []
    if current:
        entries.append(" ".join(current))
    # Each run of whitespace, e.g. "  \n  ", becomes one space.
    return [_shorten(re.sub(r"\s+", " ", entry).strip()) for entry in entries if entry]


def _commit_subjects(checkout: Path, local: str, remote: str) -> list[str]:
    log = _git(
        checkout,
        "log",
        "--no-merges",
        "--format=%s",
        f"{local}..{remote}",
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return [_shorten(line.strip()) for line in log.splitlines() if line.strip()]


def _shorten(text: str) -> str:
    if len(text) <= MAX_ENTRY_LENGTH:
        return text
    return text[: MAX_ENTRY_LENGTH - 1].rstrip() + "…"


def _git(checkout: Path, *arguments: str, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    """Run one read-only Git command, returning "" for anything that fails.

    An update notice is a convenience. No failure here -- no Git, no network,
    a remote that wants credentials -- may disturb the command being run.
    """
    environment = {
        **os.environ,
        # Never block a ww command on a credential prompt.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GIT_SSH_COMMAND": os.environ.get(
            "GIT_SSH_COMMAND", "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new"
        ),
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
