# SPDX-License-Identifier: GPL-3.0-or-later

"""Stamp a CI checkout with a PEP 440 development version; never commit it."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def set_dev_version(project: Path, base_version: str, run_number: str) -> str:
    stable_version = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    if not re.fullmatch(stable_version, base_version):
        raise ValueError("base version must be a stable major.minor.patch version")
    if not re.fullmatch(r"[1-9][0-9]*", run_number):
        raise ValueError("run number must be a positive integer")
    version = f"{base_version}.dev{run_number}"
    content = project.read_text()
    match = re.search(r"(?ms)^\[project\]\s*\n(.*?)(?=^\[|\Z)", content)
    if match is None:
        raise ValueError("pyproject.toml has no [project] table")
    table, count = re.subn(
        r'^version[ \t]*=[ \t]*"[^"]*"[ \t]*$',
        f'version = "{version}"',
        match.group(1),
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError("[project] must contain exactly one static version")
    project.write_text(content[: match.start(1)] + table + content[match.end(1) :])
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-version", required=True)
    parser.add_argument("--run-number", required=True)
    arguments = parser.parse_args()
    print(
        set_dev_version(
            ROOT / "pyproject.toml", arguments.base_version, arguments.run_number
        )
    )


if __name__ == "__main__":
    main()
