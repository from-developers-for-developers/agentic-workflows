# SPDX-License-Identifier: GPL-3.0-or-later
"""The notice that the ww checkout is behind the remote it tracks."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ww import updates
from ww.updates import (
    UpdateNotice,
    UpdateState,
    load_state,
    mark_announced,
    pending_notice,
    save_state,
)

CHANGELOG_BEFORE = """# Changelog

## Unreleased

- An existing entry that was already released.
"""

CHANGELOG_AFTER = """# Changelog

## Unreleased

- A step may declare `identity`, the field a new item must
  carry across the run.
- `interact --pause` records that the operator is done for now.
- An existing entry that was already released.
"""


def _git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _commit(repository: Path, message: str) -> None:
    _git("add", ".", cwd=repository)
    _git(
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-m",
        message,
        cwd=repository,
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A clone that tracks an origin one commit ahead of it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "CHANGELOG.md").write_text(CHANGELOG_BEFORE, encoding="utf-8")
    _commit(origin, "First")

    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)

    (origin / "CHANGELOG.md").write_text(CHANGELOG_AFTER, encoding="utf-8")
    _commit(origin, "Add item identity and interact --pause")
    return clone


def _state_file(tmp_path: Path) -> Path:
    return tmp_path / "state" / "updates.json"


def _age(state_file: Path) -> None:
    """Move the last check into the past so the next one is due."""
    state = load_state(state_file)
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    save_state(UpdateState(stale, state.announced_commit, state.notice), state_file)


def test_a_checkout_behind_its_remote_produces_a_notice(
    checkout: Path, tmp_path: Path
) -> None:
    notice = pending_notice(checkout, state_file=_state_file(tmp_path))

    assert notice is not None
    assert notice.commits_behind == 1
    assert notice.remote_ref == "origin/main"
    assert notice.checkout == checkout


def test_the_notice_summarizes_the_changelog_bullets_that_were_added(
    checkout: Path, tmp_path: Path
) -> None:
    notice = pending_notice(checkout, state_file=_state_file(tmp_path))

    assert notice is not None
    # The added bullets only, rejoined across their wrapped lines.
    assert notice.entries == (
        "A step may declare `identity`, the field a new item must carry "
        "across the run.",
        "`interact --pause` records that the operator is done for now.",
    )
    assert "already released" not in " ".join(notice.entries)


def test_an_up_to_date_checkout_says_nothing(checkout: Path, tmp_path: Path) -> None:
    _git("pull", "-q", cwd=checkout)

    assert pending_notice(checkout, state_file=_state_file(tmp_path)) is None


def test_an_announced_notice_is_not_shown_again(checkout: Path, tmp_path: Path) -> None:
    state_file = _state_file(tmp_path)
    notice = pending_notice(checkout, state_file=state_file)
    assert notice is not None
    mark_announced(notice, state_file=state_file)
    _age(state_file)

    # The remote is consulted again and finds the same commit: nothing to say.
    assert pending_notice(checkout, state_file=state_file) is None
    assert load_state(state_file).notice == notice


def test_asking_explicitly_reprints_an_already_announced_notice(
    checkout: Path, tmp_path: Path
) -> None:
    state_file = _state_file(tmp_path)
    notice = pending_notice(checkout, state_file=state_file)
    assert notice is not None
    mark_announced(notice, state_file=state_file)

    # What `ww updates --check` does: look now, and show what is there.
    assert pending_notice(checkout, state_file=state_file, force=True) == notice


def test_a_newer_remote_commit_is_announced_again(
    checkout: Path, tmp_path: Path
) -> None:
    state_file = _state_file(tmp_path)
    first = pending_notice(checkout, state_file=state_file)
    assert first is not None
    mark_announced(first, state_file=state_file)

    origin = tmp_path / "origin"
    (origin / "CHANGELOG.md").write_text(
        CHANGELOG_AFTER.replace("## Unreleased\n", "## Unreleased\n\n- One more.\n"),
        encoding="utf-8",
    )
    _commit(origin, "One more change")

    second = pending_notice(checkout, state_file=state_file, force=True)
    assert second is not None
    assert second.commit != first.commit
    assert second.commits_behind == 2


def test_no_git_work_happens_between_two_scheduled_checks(
    checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = _state_file(tmp_path)
    notice = pending_notice(checkout, state_file=state_file)
    assert notice is not None
    mark_announced(notice, state_file=state_file)

    def fail(*arguments: object, **keywords: object) -> str:
        raise AssertionError("the interval must keep Git out of ordinary commands")

    monkeypatch.setattr(updates, "_git", fail)

    assert pending_notice(checkout, state_file=state_file) is None


def test_a_notice_not_yet_announced_survives_until_it_is_shown(
    checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = _state_file(tmp_path)
    notice = pending_notice(checkout, state_file=state_file)
    assert notice is not None

    monkeypatch.setattr(
        updates, "_git", lambda *a, **k: pytest.fail("no check is due yet")
    )

    # The command that would have shown it was interrupted; the next one does.
    assert pending_notice(checkout, state_file=state_file) == notice


def test_the_interval_governs_when_the_remote_is_consulted_again(
    checkout: Path, tmp_path: Path
) -> None:
    state_file = _state_file(tmp_path)
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    save_state(UpdateState(stale, "", None), state_file)

    assert pending_notice(checkout, state_file=state_file) is not None


def test_a_directory_that_is_not_a_checkout_is_left_alone(tmp_path: Path) -> None:
    assert pending_notice(None, state_file=_state_file(tmp_path)) is None
    assert pending_notice(tmp_path, state_file=_state_file(tmp_path)) is None


def test_unreadable_state_is_treated_as_no_state(tmp_path: Path) -> None:
    corrupt = tmp_path / "updates.json"
    corrupt.write_text("{not json", encoding="utf-8")

    assert load_state(corrupt) == UpdateState()


def test_a_failing_git_never_raises(tmp_path: Path) -> None:
    assert updates._git(tmp_path, "rev-parse", "--verify", "nope") == ""
    assert updates._git(tmp_path / "missing", "status") == ""


def test_the_rendered_notice_tells_the_agent_to_relay_it() -> None:
    notice = UpdateNotice(
        Path("/tools/agentic-workflows"),
        "origin/main",
        "abc123",
        4,
        ("A change.", "Another change."),
        truncated=2,
    )

    text = notice.render()

    assert "4 commits behind `origin/main`" in text
    assert "- A change." in text
    assert "...and 2 more entries in CHANGELOG.md." in text
    assert "Tell the person you are working for" in text
    assert "git -C /tools/agentic-workflows pull" in text


def test_the_check_is_turned_off_by_configuration_and_by_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WW_UPDATE_CHECK", raising=False)
    assert updates.check_enabled(True) is True
    assert updates.check_enabled(False) is False

    monkeypatch.setenv("WW_UPDATE_CHECK", "0")
    assert updates.check_enabled(True) is False
    monkeypatch.setenv("WW_UPDATE_CHECK", "1")
    assert updates.check_enabled(False) is True
