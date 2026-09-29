# SPDX-License-Identifier: GPL-3.0-or-later
"""The change set between two git tree marks, and rule glob matching."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ww.changes import all_files, changed_files, select_files, take_mark


def _git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _write(root: Path, relative: str, text: str = "x\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git("init", "-q", "-b", "main", ".", cwd=tmp_path)
    _git("config", "user.email", "t@e.st", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _write(tmp_path, "kept.txt")
    _write(tmp_path, "doomed.txt")
    _write(tmp_path, "edited.txt")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "seed", cwd=tmp_path)
    return tmp_path


def test_the_change_set_holds_committed_staged_unstaged_and_untracked_work(
    repository: Path,
) -> None:
    _write(repository, "dirty-before.txt", "already there\n")
    mark_a = take_mark(repository)
    assert mark_a is not None

    _write(repository, "committed.txt")
    _git("add", "committed.txt", cwd=repository)
    _git("commit", "-qm", "during the step", cwd=repository)
    _write(repository, "staged.txt")
    _git("add", "staged.txt", cwd=repository)
    _write(repository, "edited.txt", "changed\n")
    _write(repository, "src/new/untracked.py")
    (repository / "doomed.txt").unlink()
    mark_b = take_mark(repository)
    assert mark_b is not None

    assert changed_files(repository, mark_a, mark_b) == (
        "committed.txt",
        "edited.txt",
        "src/new/untracked.py",
        "staged.txt",
    )


def test_taking_a_mark_leaves_the_index_and_stash_alone(repository: Path) -> None:
    _write(repository, "staged.txt")
    _git("add", "staged.txt", cwd=repository)
    _write(repository, "untracked.txt")
    before = _git("status", "--porcelain", cwd=repository)

    assert take_mark(repository) is not None

    assert _git("status", "--porcelain", cwd=repository) == before
    assert _git("stash", "list", cwd=repository) == ""


def test_the_same_tree_gives_the_same_mark_and_no_changes(repository: Path) -> None:
    first = take_mark(repository)
    second = take_mark(repository)

    assert first == second
    assert first is not None
    assert changed_files(repository, first, first) == ()


def test_ww_state_is_never_part_of_the_change_set(repository: Path) -> None:
    mark_a = take_mark(repository)
    _write(repository, ".ww/tasks/T-1/state.json", "{}")
    _write(repository, "work.txt")
    mark_b = take_mark(repository)
    assert mark_a is not None and mark_b is not None

    assert changed_files(repository, mark_a, mark_b) == ("work.txt",)


def test_a_subdirectory_reports_paths_relative_to_itself(repository: Path) -> None:
    project = repository / "backend"
    _write(repository, "backend/seed.txt")
    _git("add", "-A", cwd=repository)
    _git("commit", "-qm", "backend", cwd=repository)
    mark_a = take_mark(project)
    _write(repository, "backend/app.py")
    _write(repository, "frontend/app.js")
    mark_b = take_mark(project)
    assert mark_a is not None and mark_b is not None

    assert changed_files(project, mark_a, mark_b) == ("app.py",)


def test_without_git_there_is_no_mark_and_every_file_is_a_candidate(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "a.py")
    _write(tmp_path, "docs/b.md")
    _write(tmp_path, ".ww/tasks/state.json")
    _write(tmp_path, ".git/config")

    assert take_mark(tmp_path) is None
    assert all_files(tmp_path) == ("a.py", "docs/b.md")


FILES = ("README.md", "docs/guide.md", "src/a.py", "src/pkg/b.py", "src/pkg/c.txt")


@pytest.mark.parametrize(
    ("globs", "expected"),
    [
        ((), FILES),
        (("*.py",), ("src/a.py", "src/pkg/b.py")),
        (("**/*.py",), ("src/a.py", "src/pkg/b.py")),
        (("src/*.py",), ("src/a.py",)),
        (("src/**/*.py",), ("src/a.py", "src/pkg/b.py")),
        (("src/",), ("src/a.py", "src/pkg/b.py", "src/pkg/c.txt")),
        (("docs/**",), ("docs/guide.md",)),
        (("./README.md",), ("README.md",)),
        (("src/pkg/?.py",), ("src/pkg/b.py",)),
        (("src/pkg/[bc].*",), ("src/pkg/b.py", "src/pkg/c.txt")),
        (("src/pkg/[!b].*",), ("src/pkg/c.txt",)),
        (("*.md", "src/a.py"), ("README.md", "docs/guide.md", "src/a.py")),
        (("*.rs",), ()),
    ],
)
def test_globs_select_files(globs: tuple[str, ...], expected: tuple[str, ...]) -> None:
    assert select_files(FILES, globs) == expected
