# SPDX-License-Identifier: GPL-3.0-or-later
"""The change set between two git tree marks, and rule glob matching."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ww.changes import (
    all_files,
    changed_files,
    changed_lines,
    select_files,
    take_mark,
)


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


def test_the_rule_automation_store_is_left_out_of_marks_and_file_lists(
    repository: Path,
) -> None:
    mark_a = take_mark(repository)
    _write(repository, "ww-rule-automation.json", "{}\n")

    assert take_mark(repository) == mark_a
    _write(repository, "code.py")
    mark_b = take_mark(repository)
    assert mark_a is not None and mark_b is not None
    assert changed_files(repository, mark_a, mark_b) == ("code.py",)
    assert "ww-rule-automation.json" not in all_files(repository)


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


def _tree(root: Path) -> tuple[str, ...]:
    files = {
        "a.php": "<?php class Mail {}\n",
        "b.php": "<?php class Post {}\n",
        "notes.md": "Mail is sent by the class.\n",
        "mixed.txt": "mail in lower case\n",
        "data.bin": "",
    }
    for name, text in files.items():
        _write(root, name, text)
    (root / "data.bin").write_bytes(b"\xff\xfe Mail \x00")
    return tuple(sorted(files))


def test_contains_in_file_alone_selects_the_files_with_the_text(
    tmp_path: Path,
) -> None:
    files = _tree(tmp_path)

    assert select_files(files, (), ("Mail",), directory=tmp_path) == (
        "a.php",
        "notes.md",
    )


def test_contains_is_case_sensitive_plain_text_not_a_pattern(tmp_path: Path) -> None:
    files = _tree(tmp_path)

    assert select_files(files, (), ("mail",), directory=tmp_path) == ("mixed.txt",)
    assert select_files(files, (), ("M.il",), directory=tmp_path) == ()
    assert select_files(files, (), ("Mail.*",), directory=tmp_path) == ()


def test_any_of_several_strings_selects(tmp_path: Path) -> None:
    files = _tree(tmp_path)

    assert select_files(files, (), ("Post", "lower case"), directory=tmp_path) == (
        "b.php",
        "mixed.txt",
    )


def test_globs_and_contains_both_have_to_match(tmp_path: Path) -> None:
    files = _tree(tmp_path)

    assert select_files(files, ("*.php",), ("Mail",), directory=tmp_path) == ("a.php",)
    assert select_files(files, ("*.md",), ("Post",), directory=tmp_path) == ()
    assert select_files(files, ("*.php",), (), directory=tmp_path) == ("a.php", "b.php")


def test_a_file_that_is_not_text_or_is_gone_is_not_selected(tmp_path: Path) -> None:
    files = (*_tree(tmp_path), "deleted.php")

    assert "data.bin" not in select_files(files, (), ("Mail",), directory=tmp_path)
    assert "deleted.php" not in select_files(files, (), ("",), directory=tmp_path)


@pytest.fixture
def marked(repository: Path) -> tuple[Path, tuple[str, str]]:
    """A step that edited, created, and renamed files; its marks."""
    _write(repository, "edited.txt", "keep Mail\nold line\nkeep Post\n")
    _write(repository, "with space.txt", "x\n")
    _git("add", "-A", cwd=repository)
    _git("commit", "-qm", "before the step", cwd=repository)
    mark_a = take_mark(repository)
    _write(repository, "edited.txt", "keep Mail\nnew line\nkeep Post\n")
    _write(repository, "created.txt", "Fresh Mail\nsecond\n")
    (repository / "with space.txt").rename(repository / "with other space.txt")
    (repository / "data.bin").write_bytes(b"Mail \x00\xff")
    mark_b = take_mark(repository)
    assert mark_a and mark_b
    return repository, (mark_a, mark_b)


def test_changed_lines_are_the_added_and_removed_lines_only(
    marked: tuple[Path, tuple[str, str]],
) -> None:
    root, marks = marked

    assert changed_lines(root, *marks, "edited.txt") == "old line\nnew line"
    assert changed_lines(root, *marks, "created.txt") == "Fresh Mail\nsecond"
    assert changed_lines(root, *marks, "with other space.txt") == "x"
    assert changed_lines(root, *marks, "data.bin") == ""
    assert changed_lines(root, *marks, "kept.txt") == ""


def test_contains_in_diff_reads_the_changed_lines_with_marks(
    marked: tuple[Path, tuple[str, str]],
) -> None:
    root, marks = marked
    files = changed_files(root, *marks)

    assert files == ("created.txt", "data.bin", "edited.txt", "with other space.txt")
    # ``Mail`` is only in the unchanged lines of ``edited.txt``.
    assert select_files(files, (), (), ("Mail",), directory=root, marks=marks) == (
        "created.txt",
    )
    assert select_files(files, (), (), ("old line",), directory=root, marks=marks) == (
        "edited.txt",
    )
    assert select_files(files, (), (), ("new line",), directory=root, marks=marks) == (
        "edited.txt",
    )
    # Without marks the whole file counts, as it does without git.
    assert select_files(files, (), (), ("Mail",), directory=root) == (
        "created.txt",
        "edited.txt",
    )


def test_both_kinds_of_strings_have_to_hold(
    marked: tuple[Path, tuple[str, str]],
) -> None:
    root, marks = marked
    files = changed_files(root, *marks)

    assert select_files(
        files, (), ("Post",), ("new line",), directory=root, marks=marks
    ) == ("edited.txt",)
    assert (
        select_files(files, (), ("Post",), ("Fresh",), directory=root, marks=marks)
        == ()
    )
