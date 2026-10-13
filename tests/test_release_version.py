# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

script = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/release_version.py")
)
next_version = script["next_version"]
project_version = script["project_version"]
stamp = script["stamp"]
PYPROJECT = """[project]
name = "x"
version = "1.0.0b1"

[tool.other]
version = "9"
"""


@pytest.mark.parametrize(
    ("series", "released", "expected"),
    [
        # A series not on PyPI yet is released as written.
        ("1.0.0b1", ["1.0.0.dev15"], "1.0.0b1"),
        ("1.0.0rc1", ["1.0.0b1", "1.0.0b7"], "1.0.0rc1"),
        ("1.0.0", ["1.0.0rc2"], "1.0.0"),
        ("1.1.0", ["1.0.0", "1.0.4"], "1.1.0"),
        ("1.0.0b1", [], "1.0.0b1"),
        # Otherwise the highest of its series moves on, whatever else exists.
        ("1.0.0b1", ["1.0.0.dev15", "1.0.0b1"], "1.0.0b2"),
        ("1.0.0b1", ["1.0.0b1", "1.0.0b2", "1.0.0b10", "1.0.0rc1"], "1.0.0b11"),
        ("1.0.0rc1", ["1.0.0b9", "1.0.0rc1", "1.0.0rc2"], "1.0.0rc3"),
        ("1.0.0", ["1.0.0", "1.0.1rc1", "1.0.2", "1.1.0"], "1.0.3"),
        ("1.1.0", ["1.0.9", "1.1.0"], "1.1.1"),
    ],
)
def test_the_next_version_continues_the_series(
    series: str, released: list[str], expected: str
) -> None:
    assert next_version(series, released) == expected


@pytest.mark.parametrize("series", ["1.0", "1.0.0.dev1", "v1.0.0", "1.0.0beta1"])
def test_a_series_must_be_a_release_version(series: str) -> None:
    with pytest.raises(ValueError, match="must be X.Y.Z"):
        next_version(series, [])


def test_the_version_is_read_and_stamped_in_the_project_table(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT)
    assert project_version(pyproject) == "1.0.0b1"
    stamp(pyproject, "1.0.0b2")
    assert project_version(pyproject) == "1.0.0b2"
    assert 'version = "9"' in pyproject.read_text()


def test_a_project_without_a_static_version_is_refused(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\ndynamic = ["version"]\n')
    with pytest.raises(ValueError, match="static version"):
        project_version(pyproject)
