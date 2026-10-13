# SPDX-License-Identifier: GPL-3.0-or-later

"""Choose the version a push to main releases, and stamp it into the build.

``pyproject.toml``'s version names the release series. While PyPI lacks that
exact version it is released as written; otherwise the highest PyPI version
of the same series is incremented: ``1.0.0b3`` -> ``1.0.0b4``, ``1.0.0rc1`` ->
``1.0.0rc2``, and a final ``1.2.3`` -> ``1.2.4``. A yanked version stays taken.
Changing series, to an RC, a final release, or the next minor, is a manual
edit of ``pyproject.toml``. The stamped version is never committed.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "ww-agentic-workflows"
HTTP_NOT_FOUND = 404
_VERSION = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:(?P<label>a|b|rc)(?P<number>0|[1-9][0-9]*))?"
)
_PROJECT_VERSION = re.compile(r'^version[ \t]*=[ \t]*"([^"]*)"[ \t]*$', re.MULTILINE)


@dataclass(frozen=True, order=True)
class Version:
    """A release version: ``X.Y.Z``, or a pre-release ``X.Y.Z{a,b,rc}N``."""

    major: int
    minor: int
    patch: int
    label: str | None = None
    number: int | None = None

    @classmethod
    def parse(cls, text: str) -> Version | None:
        match = _VERSION.fullmatch(text.strip())
        if match is None:
            return None
        number = match["number"]
        return cls(
            int(match["major"]),
            int(match["minor"]),
            int(match["patch"]),
            match["label"],
            int(number) if number is not None else None,
        )

    def same_series(self, other: Version) -> bool:
        """Pre-releases share ``X.Y.Z`` and the label; finals share ``X.Y``."""
        if self.label is not None:
            return (self.major, self.minor, self.patch, self.label) == (
                other.major,
                other.minor,
                other.patch,
                other.label,
            )
        return other.label is None and (self.major, self.minor) == (
            other.major,
            other.minor,
        )

    def following(self) -> Version:
        if self.label is not None:
            assert self.number is not None
            return Version(
                self.major, self.minor, self.patch, self.label, self.number + 1
            )
        return Version(self.major, self.minor, self.patch + 1)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.label is None else f"{base}{self.label}{self.number}"


def next_version(series: str, released: Iterable[str]) -> str:
    """The version to release for ``series`` given the versions on PyPI."""
    base = Version.parse(series)
    if base is None:
        raise ValueError(
            f"pyproject.toml version {series!r} must be X.Y.Z or X.Y.Z{{a,b,rc}}N"
        )
    published = {parsed for text in released if (parsed := Version.parse(text))}
    if base not in published:
        return str(base)
    return str(max(v for v in published if base.same_series(v)).following())


def project_version(pyproject: Path) -> str:
    match = _PROJECT_VERSION.search(_project_table(pyproject.read_text()))
    if match is None:
        raise ValueError("[project] must contain one static version")
    return match.group(1)


def stamp(pyproject: Path, version: str) -> None:
    """Write ``version`` into the build checkout's ``pyproject.toml``."""
    content = pyproject.read_text()
    table = _project_table(content)
    stamped, count = _PROJECT_VERSION.subn(f'version = "{version}"', table)
    if count != 1:
        raise ValueError("[project] must contain exactly one static version")
    pyproject.write_text(content.replace(table, stamped, 1))


def released_versions(package: str = PACKAGE) -> tuple[str, ...]:
    """Every version PyPI holds for ``package``, yanked ones included."""
    url = f"https://pypi.org/pypi/{package}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == HTTP_NOT_FOUND:
            return ()
        raise
    return tuple(data.get("releases", {}))


def _project_table(content: str) -> str:
    match = re.search(r"(?ms)^\[project\]\s*\n(.*?)(?=^\[|\Z)", content)
    if match is None:
        raise ValueError("pyproject.toml has no [project] table")
    return match.group(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    parser.add_argument(
        "--stamp", action="store_true", help="Write the chosen version into it."
    )
    arguments = parser.parse_args()
    version = next_version(project_version(arguments.pyproject), released_versions())
    if arguments.stamp:
        stamp(arguments.pyproject, version)
    print(version)


if __name__ == "__main__":
    main()
