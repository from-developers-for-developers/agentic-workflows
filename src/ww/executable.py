# SPDX-License-Identifier: GPL-3.0-or-later
"""How the commands ww prints invoke ww.

A project names its ww binary in ``ww.json`` (``executable``),
so two installs, such as one for developing ww and one pinned to ``dev``, can
live side by side under different global names. Every command ww prints
starts with that binary, or with the project launcher, ``./ww``, when the
project names none, or names the default binary while the launcher is there
to run it. The CLI sets it once per invocation.

This module imports nothing from ww, so any module may use it.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

PROJECT_LAUNCHER_COMMAND = "./ww"
# The binary a project uses when it names none, and the one the launcher
# falls back to.
DEFAULT_EXECUTABLE = "ww-agentic-workflows"
_EXECUTABLE: ContextVar[str] = ContextVar(
    "ww_executable", default=PROJECT_LAUNCHER_COMMAND
)


def project_command(configured: str | None, root: Path) -> str:
    """How the commands printed for the project at ``root`` invoke ww.

    The launcher, unless ``configured`` names a binary of the project's own:
    the default binary's name, which an older ``init`` wrote into every
    ``ww.json``, is no choice while the launcher is there to run it.
    """
    if not configured:
        return PROJECT_LAUNCHER_COMMAND
    launcher = root / "ww"
    if (
        configured == DEFAULT_EXECUTABLE
        and launcher.is_file()
        and os.access(launcher, os.X_OK)
    ):
        return PROJECT_LAUNCHER_COMMAND
    return configured


@contextmanager
def printed_executable(command: str) -> Iterator[None]:
    """Print commands with ``command``, a project's :func:`project_command`."""
    token = _EXECUTABLE.set(shlex.quote(command))
    try:
        yield
    finally:
        _EXECUTABLE.reset(token)


def ww_command() -> str:
    """How a printed command invokes ww, as the command's first word."""
    return _EXECUTABLE.get()
