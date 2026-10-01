# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww inspect``: a read-only profile of a checkout and its Git history."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ww.cli import main
from ww.inspect import (
    WEEK_SECONDS,
    Count,
    VerifyCommand,
    cadence,
    commit_format_for,
    history_weeks,
    inspect_checkout,
    is_fix,
    team_shape,
    ticket_prefixes,
    weekly_commits,
)

AUTHORS = {
    "first": ("Test", "t@e.st"),
    "second": ("Other", "o@e.st"),
}


def _git(root: Path, *arguments: str, author: str = "first") -> None:
    name, email = AUTHORS[author]
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, env=environment
    )


def _commit(
    root: Path, path: str, text: str, subject: str, author: str = "first"
) -> None:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    _git(root, "add", path, author=author)
    _git(root, "commit", "-q", "-m", subject, author=author)


def _scripted(root: Path) -> Path:
    """Two authors, feature and hotfix branches, fixes on one file, a tag."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "t@e.st")
    _commit(
        root,
        "package.json",
        json.dumps({"scripts": {"test": "jest", "lint": "eslint .", "start": "x"}}),
        "Add the package",
    )
    _commit(root, ".github/workflows/ci.yml", "on: push\n", "PROJ-1: Add CI")
    _commit(root, "src/app.js", "1\n", "PROJ-12: Add the app", author="second")
    _git(root, "tag", "v1")
    _git(root, "switch", "-q", "-c", "feature/proj-13")
    _commit(root, "src/view.js", "1\n", "PROJ-13: Add the view", author="second")
    _git(root, "switch", "-q", "main")
    _git(
        root,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "Merge branch 'feature/proj-13'",
        "feature/proj-13",
    )
    _git(root, "switch", "-q", "-c", "hotfix/crash")
    _commit(root, "src/app.js", "2\n", "fix: stop the crash")
    _git(root, "switch", "-q", "main")
    _git(
        root,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "Merge branch 'hotfix/crash'",
        "hotfix/crash",
    )
    _commit(root, "src/app.js", "3\n", "Fixed the totals", author="second")
    _commit(root, "README.md", "See ../billing-api.\n", "PROJ-14: Describe it")
    return root


def _listing(root: Path) -> dict[str, float]:
    return {
        path.relative_to(root).as_posix(): path.stat().st_mtime
        for path in root.rglob("*")
    }


def _inspect(root: Path, capsys: pytest.CaptureFixture[str], *flags: str) -> str:
    assert main(["--root", str(root), "inspect", *flags]) == 0
    return capsys.readouterr().out


def test_inspect_profiles_a_scripted_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _scripted(tmp_path)
    before = _listing(root)

    page = _inspect(root, capsys)
    profile = inspect_checkout(root)

    assert _listing(root) == before
    repository = profile.sections["repository"]
    assert repository["default_branch"].value == "main"
    assert repository["integration_branch"].value == "main"
    assert set(repository["branch_patterns"].value) == {
        Count("feature/", 2),
        Count("hotfix/", 2),
    }
    assert repository["remotes"].value == ()
    assert repository["tags"].value == 1
    assert repository["merge_share"].value == round(2 / 9, 3)
    activity = profile.sections["activity"]
    assert activity["commits"].value == 9
    assert activity["contributors"].value == 2
    assert activity["team_shape"].value == "small"
    # The whole history is minutes old: one week, not eleven empty ones.
    assert activity["weekly_commits"].value == (9,)
    assert activity["cadence"].value == "daily"
    fixes = profile.sections["fixes"]
    assert fixes["share"].value == round(2 / 7, 3)
    assert fixes["paths"].value == (Count("src/app.js", 2),)
    assert fixes["recent"].value == ("Fixed the totals", "fix: stop the crash")
    assert profile.value("hot_paths", "most_changed")[0] == Count("src/app.js", 3)
    layout = profile.sections["layout"]
    assert layout["ci"].value == (".github/workflows/ci.yml",)
    assert layout["verify"].value == (
        VerifyCommand("test", ("npm", "run", "test"), ".", "package.json"),
        VerifyCommand("lint", ("npm", "run", "lint"), ".", "package.json"),
    )
    assert layout["projects"].value == ("../billing-api",)
    conventions = profile.sections["conventions"]
    # Four subjects and the lower-case "feature/proj-13" branch, local and merged.
    assert conventions["ticket_prefixes"].value == (Count("PROJ", 6),)
    assert conventions["task_format"].value == "PROJ-{{digit}}"
    assert conventions["ticket_share"].value == round(4 / 7, 3)
    assert conventions["commit_format"].value == "{{ww.task.id}}: {{commit_message}}"

    assert "## Repository" in page
    assert "- Default branch: main (refs/heads/main exists)" in page
    assert "- Branch patterns: " in page and "(git branch, merge subjects)" in page
    assert "- Fix share: 29% (2 of 7 non-merge commits," in page
    assert "test: `npm run test` from package.json" in page
    assert "- Cadence: daily (median 9/week over the 1 week the history spans" in page
    assert '- Recent fixes: "Fixed the totals"; "fix: stop the crash"' in page
    assert "- task_format candidate: `PROJ-{{digit}}`" in page


def test_inspect_json_round_trips_the_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _scripted(tmp_path)

    printed = json.loads(_inspect(root, capsys, "--json", "--commits", "5"))

    assert printed == inspect_checkout(root, 5).to_dict()
    assert printed["sections"]["activity"]["commits"] == {
        "value": 5,
        "evidence": "git log -n 5",
    }


def test_inspect_an_empty_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    before = _listing(tmp_path)

    page = _inspect(tmp_path, capsys)
    profile = inspect_checkout(tmp_path)

    assert _listing(tmp_path) == before
    assert profile.git_unavailable is None
    assert profile.value("activity", "commits") == 0
    assert profile.value("activity", "team_shape") is None
    assert profile.value("conventions", "task_format") is None
    assert "- Team shape: not found (" in page
    assert "- Default branch: main (current branch" in page


def test_inspect_a_directory_without_git(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "Makefile").write_text("test: deps\n\tpytest\nlint:\n\truff\n")
    (tmp_path / "CLAUDE.md").write_text("Notes.\n")
    for name in ("web", "api"):
        (tmp_path / "apps" / name).mkdir(parents=True)
        (tmp_path / "apps" / name / "package.json").write_text("{}")
    before = _listing(tmp_path)

    page = _inspect(tmp_path, capsys)
    profile = inspect_checkout(tmp_path)

    assert _listing(tmp_path) == before
    assert list(profile.sections) == ["layout", "conventions"]
    assert "Git facts are unavailable: not a Git repository" in page
    assert "## Repository" not in page
    assert [command.argv for command in profile.value("layout", "verify")] == [
        ("make", "test"),
        ("make", "lint"),
    ]
    assert profile.value("layout", "projects") == ("apps/api", "apps/web")
    assert "apps/ holds manifests" in profile.value("layout", "monorepo_signals")
    assert profile.value("conventions", "agent_files") == ("CLAUDE.md",)
    assert "- Ticket prefixes: not found (no Git history)" in page


def test_derived_rules() -> None:
    assert [team_shape(n) for n in (0, 1, 2, 5, 6)] == [
        "solo",
        "solo",
        "small",
        "small",
        "team",
    ]
    assert cadence([5] * 7 + [0] * 5) == "daily"
    assert cadence([1] * 7 + [0] * 5) == "weekly"
    assert cadence([0] * 7 + [3] * 5) == "sparse"
    assert ticket_prefixes(["PROJ-1 and PROJ-2", "feature/OPS-3", "OPS-4"]) == (
        Count("OPS", 2),
        Count("PROJ", 1),
    )
    # Branch names count keys in any case; subjects only upper-case ones.
    assert ticket_prefixes(["utf-8 now"], ["hotfix/task-3", "feature/TASK-4"]) == (
        Count("TASK", 2),
    )
    assert commit_format_for(0.5) == "{{ww.task.id}}: {{commit_message}}"
    assert commit_format_for(0.4) == "{{commit_message}}"


def test_fix_subjects() -> None:
    fixes = [
        "fix: keep the cursor",
        "fix(cli): keep the cursor",
        "Fixed the totals",
        "Fixes the crash",
        "hotfix: the release",
        'Revert "Add the cache"',
        "PROJ-3: Fix the crash",
        "Guard against the cache regression",
    ]
    features = [
        "Add rules, checks and the fix loop",
        "Prefix the subjects",
        "Describe the hotfix lane",
        "feat: add fixtures",
    ]
    assert [is_fix(subject) for subject in fixes] == [True] * len(fixes)
    assert [is_fix(subject) for subject in features] == [False] * len(features)


def test_cadence_spans_the_weeks_the_history_has() -> None:
    now = 100 * WEEK_SECONDS
    young = [now - day * 86_400 for day in range(6)]
    assert history_weeks(young, now) == 1
    assert cadence(weekly_commits(young, now, history_weeks(young, now))) == "daily"
    old = [now - 30 * WEEK_SECONDS, now - 3 * WEEK_SECONDS]
    assert history_weeks(old, now) == 12
    assert history_weeks([], now) == 1
