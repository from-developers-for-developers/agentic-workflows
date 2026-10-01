#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Add an SPDX license identifier to Python and shell source files.

With no paths, this script recursively processes Python and shell files below
the current directory. Pass paths (files and/or directories) to limit the
scope. Existing SPDX identifiers are preserved.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

DEFAULT_LICENSE = "GPL-3.0-or-later"
SUPPORTED_SUFFIXES = {".py", ".sh"}
EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
# An SPDX tag comment on any line, e.g. "# SPDX-License-Identifier: MIT".
SPDX_PATTERN = re.compile(r"^\s*#\s*SPDX-[A-Za-z0-9-]+:", re.MULTILINE)
# A PEP 263 encoding comment, e.g. "# -*- coding: utf-8 -*-".
PYTHON_ENCODING_PATTERN = re.compile(r"^\s*#.*coding[:=]", re.IGNORECASE)


def supported_files(paths: Iterable[Path]) -> Iterable[Path]:
    """Yield supported files recursively, without visiting generated trees."""
    seen: set[Path] = set()
    for path in paths:
        candidates: Iterable[Path]
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = (
                child
                for child in path.rglob("*")
                if not any(part in EXCLUDED_DIRECTORIES for part in child.parts)
            )
        else:
            print(f"warning: skipping missing path: {path}", file=sys.stderr)
            continue

        for candidate in candidates:
            if candidate.is_file() and candidate.suffix in SUPPORTED_SUFFIXES:
                resolved = candidate.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    yield candidate


def header_insertion_offset(contents: str) -> int:
    """Keep a shebang and Python encoding declaration in their required spots."""
    lines = contents.splitlines(keepends=True)
    offset = 0
    if lines and lines[0].startswith("#!"):
        offset = 1
    if offset < len(lines) and PYTHON_ENCODING_PATTERN.match(lines[offset]):
        offset += 1
    return sum(len(line) for line in lines[:offset])


def ensure_header(path: Path, license_identifier: str) -> bool:
    """Add the SPDX identifier to *path*, returning whether it changed."""
    try:
        contents = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"warning: skipping non-UTF-8 file: {path}", file=sys.stderr)
        return False

    updated = add_header(contents, license_identifier)
    if updated is None:
        return False

    with path.open("w", encoding="utf-8", newline="") as output:
        output.write(updated)
    print(path)
    return True


def add_header(contents: str, license_identifier: str) -> str | None:
    """Return contents with a header added, or ``None`` when it already has one."""
    if SPDX_PATTERN.search(contents):
        return None

    newline = "\r\n" if "\r\n" in contents else "\n"
    offset = header_insertion_offset(contents)
    header = f"# SPDX-License-Identifier: {license_identifier}{newline}"
    return contents[:offset] + header + contents[offset:]


def staged_paths() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--diff-filter=ACMR",
            "--",
            "*.py",
            "*.sh",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [Path(name) for name in result.stdout.decode("utf-8").split("\0") if name]


def ensure_staged_header(path: Path, license_identifier: str) -> bool:
    """Add a header to the version of *path* in Git's index.

    A pre-commit hook must inspect the staged snapshot, not the working copy:
    the two can differ when a developer has unstaged edits. Updating the index
    directly preserves those unstaged edits and guarantees the committed file
    receives the header.
    """
    name = os.fspath(path)
    working_tree_matches_index = (
        subprocess.run(["git", "diff", "--quiet", "--", name], check=False).returncode
        == 0
    )
    contents_result = subprocess.run(
        ["git", "show", f":{name}"],
        check=True,
        stdout=subprocess.PIPE,
    )
    try:
        updated = add_header(contents_result.stdout.decode("utf-8"), license_identifier)
    except UnicodeDecodeError:
        print(f"warning: skipping non-UTF-8 staged file: {path}", file=sys.stderr)
        return False
    if updated is None:
        return False

    stage_result = subprocess.run(
        ["git", "ls-files", "--stage", "--", name],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    mode = stage_result.stdout.split(maxsplit=1)[0]
    object_result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        check=True,
        input=updated.encode("utf-8"),
        stdout=subprocess.PIPE,
    )
    blob = object_result.stdout.decode("ascii").strip()
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"{mode},{blob},{name}"],
        check=True,
    )
    if working_tree_matches_index:
        with path.open("w", encoding="utf-8", newline="") as output:
            output.write(updated)
    print(path)
    return True


def install_hook() -> None:
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], check=True)
    print("Installed repository hooks from .githooks.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="*", type=Path, help="Files or directories to process"
    )
    parser.add_argument(
        "--license",
        default=DEFAULT_LICENSE,
        help=f"SPDX license identifier (default: {DEFAULT_LICENSE})",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Process staged added/modified Python and shell files",
    )
    parser.add_argument(
        "--install-hook",
        action="store_true",
        help="Configure Git to use the committed .githooks directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.install_hook:
        if args.paths or args.staged:
            raise SystemExit("--install-hook cannot be combined with paths or --staged")
        install_hook()
        return 0

    if args.staged:
        for path in staged_paths():
            ensure_staged_header(path, args.license)
        return 0

    paths = args.paths or [Path.cwd()]
    for path in supported_files(paths):
        ensure_header(path, args.license)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
