# SPDX-License-Identifier: GPL-3.0-or-later
"""Task working directories: persisted relative to the project root.

A task's state may be read from another filesystem than the one that wrote
it, for example a container that mounts the checkout elsewhere.  ww therefore
stores a working directory relative to the project root and resolves it
against the current root whenever a path is printed or used.  A value saved
absolute by an earlier release still resolves, because joining an absolute
path onto the root yields that path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, get_args


def relative_workspace(root: Path, path: Path | str) -> str:
    """The persisted form of a working directory: relative to ``root``."""
    return os.path.relpath(Path(path).resolve(), root.resolve())


def resolve_workspace(root: Path, value: str | None) -> Path | None:
    """The absolute working directory for the filesystem ww runs in now."""
    if not value:
        return None
    return (root / value).resolve()


Workdir = Literal["task", "project", "root"]
WORKDIRS: tuple[Workdir, ...] = get_args(Workdir)


def item_workspace(
    root: Path,
    workdir: Workdir,
    working_directory: str | None,
    project_dir: str | None,
) -> Path | None:
    """The directory a plan item works in, chosen by its ``workdir``.

    ``task`` is the task workspace (``None`` while that is still the root);
    ``project`` is the configured project directory itself, never a worktree
    made from it, and the root for a task without a project; ``root`` is the
    project root that holds configuration and ``.ww``.
    """
    if workdir == "root":
        return root.resolve()
    if workdir == "project":
        return Path(project_dir).resolve() if project_dir else root.resolve()
    return resolve_workspace(root, working_directory)
