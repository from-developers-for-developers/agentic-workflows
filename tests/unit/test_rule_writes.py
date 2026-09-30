# SPDX-License-Identifier: GPL-3.0-or-later
"""The text edits behind the rule writes, and the files a glob is counted on."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from ww.changes import project_files
from ww.errors import StateError
from ww.rule_writes import _set_key, _split, _with_import, rule_stem


@pytest.mark.parametrize(
    ("text", "stem"),
    [
        (
            "Put every `*Service.php` under `src/Service/`. Why.",
            "put-every-service-php-under",
        ),
        ("Keep it short!", "keep-it-short"),
        ("Use UTF-8", "use-utf-8"),
    ],
)
def test_a_stem_is_the_first_five_words_of_the_first_sentence(
    text: str, stem: str
) -> None:
    assert rule_stem(text) == stem


def test_a_sentence_without_words_needs_an_id() -> None:
    with pytest.raises(StateError, match="pass --id"):
        rule_stem("...")


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "# Workflows.\nmodes: []\n",
            "# Workflows.\nimports:\n  - ww-rules.yaml\nmodes: []\n",
        ),
        (
            "extends: false\nmodes: []\n",
            "imports:\n  - ww-rules.yaml\nextends: false\nmodes: []\n",
        ),
        ("---\nmodes: []", "---\nimports:\n  - ww-rules.yaml\nmodes: []\n"),
        (
            "imports:\n    - a.yaml\n    # b is gone\n\nmodes: []\n",
            (
                "imports:\n    - a.yaml\n    - ww-rules.yaml\n    # b is gone\n\n"
                "modes: []\n"
            ),
        ),
        ("imports: []\nmodes: []\n", "imports: [ww-rules.yaml]\nmodes: []\n"),
    ],
)
def test_an_import_is_added_without_touching_the_rest(before: str, after: str) -> None:
    raw = yaml.safe_load(before)

    assert _with_import(before, raw, "ww-rules.yaml") == after


def test_an_imports_list_ww_cannot_extend_is_returned_unchanged() -> None:
    text = "imports: !!seq [a.yaml]\nmodes: []\n"

    assert _with_import(text, yaml.safe_load(text), "ww-rules.yaml") == text


def test_a_frontmatter_key_is_replaced_and_every_other_line_kept(
    tmp_path: Path,
) -> None:
    frontmatter = (
        "# Scoped to PHP.\npaths:\n  - src/**/*.php\n  - lib/**/*.php\nmax_fixes: 2\n"
    )

    result = _set_key(frontmatter, "paths", ["app/**"], tmp_path / "rule.md")

    assert result == "# Scoped to PHP.\nmax_fixes: 2\npaths: [app/**]\n"
    assert _set_key("", "check", {"argv": ["true"]}, tmp_path / "rule.md") == (
        "check:\n  argv: ['true']\n"
    )


def test_a_frontmatter_ww_cannot_edit_cleanly_is_refused(tmp_path: Path) -> None:
    # A blank line inside the list: ww stops removing ``paths``' lines there,
    # and reading the result back shows the leftover item.
    frontmatter = "paths:\n- a\n\n- b\nmax_fixes: 2\n"

    with pytest.raises(StateError, match="change it by hand"):
        _set_key(frontmatter, "paths", ["c"], tmp_path / "rule.md")


def test_a_rule_file_splits_and_joins_back_byte_for_byte() -> None:
    content = "---\npaths: [a]\n---\nBody.\n"

    parts = _split(content)

    assert parts == ("---\n", "paths: [a]\n", "---\n", "Body.\n")
    assert "".join(part or "" for part in parts) == content
    assert _split("Body only.\n") == (None, None, None, "Body only.\n")


def test_project_files_are_what_git_tracks_or_would_track(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("vendor/\n", encoding="utf-8")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor/lib.php").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.php").write_text("", encoding="utf-8")
    (tmp_path / ".ww").mkdir()
    (tmp_path / ".ww/state.json").write_text("{}", encoding="utf-8")

    assert project_files(tmp_path) == (".gitignore", "src/app.php")


def test_project_files_without_git_are_every_file(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.php").write_text("", encoding="utf-8")

    assert project_files(tmp_path) == ("src/app.php",)
