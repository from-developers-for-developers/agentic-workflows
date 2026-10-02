# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

set_dev_version = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/set_dev_version.py")
)["set_dev_version"]


def test_stamp_only_project_version_and_allow_retry(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    original = (
        '[project]\nname = "example"\nversion = "0.1.0"\n'
        '\n[tool.example]\nversion = "keep-me"\n'
    )
    project.write_text(original)
    assert set_dev_version(project, "1.0.0", "42") == "1.0.0.dev42"
    expected = original.replace('version = "0.1.0"', 'version = "1.0.0.dev42"')
    assert project.read_text() == expected
    set_dev_version(project, "1.0.0", "42")
    assert project.read_text() == expected
    set_dev_version(project, "1.0.0", "43")
    assert 'version = "1.0.0.dev43"' in project.read_text()


@pytest.mark.parametrize(
    ("base", "number"),
    [
        ("1.0.0b1", "1"),
        ("1.0", "1"),
        ("01.0.0", "1"),
        ("1.0.0", "0"),
        ("1.0.0", "-1"),
        ("1.0.0", "1\n2"),
    ],
)
def test_invalid_version_inputs_leave_file_unchanged(
    tmp_path: Path, base: str, number: str
) -> None:
    project = tmp_path / "pyproject.toml"
    original = '[project]\nversion = "0.1.0"\n'
    project.write_text(original)
    with pytest.raises(ValueError):
        set_dev_version(project, base, number)
    assert project.read_text() == original


@pytest.mark.parametrize(
    "original",
    [
        '[tool.example]\nversion = "0.1.0"\n',
        '[project]\ndynamic = ["version"]\n',
        '[project]\nversion = "0.1.0"\nversion = "0.2.0"\n',
    ],
)
def test_unsupported_metadata_leaves_file_unchanged(
    tmp_path: Path, original: str
) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(original)
    with pytest.raises(ValueError):
        set_dev_version(project, "1.0.0", "42")
    assert project.read_text() == original
