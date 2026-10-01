# SPDX-License-Identifier: GPL-3.0-or-later
"""How the commands ww prints invoke ww.

A project names its ww binary in ``ww.json`` (``executable``),
so two installs, such as one for developing ww and one pinned to ``dev``, can
live side by side under different global names. Every command ww prints
starts with that binary, or with the project launcher, ``./ww``, when the
project names none. The CLI sets it once per invocation.

This module imports nothing from ww, so any module may use it.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

PROJECT_LAUNCHER_COMMAND = "./ww"
# The binary a project uses when it names none, and the one the launcher
# falls back to.
DEFAULT_EXECUTABLE = "ww-agentic-workflows"
_EXECUTABLE: ContextVar[str] = ContextVar(
    "ww_executable", default=PROJECT_LAUNCHER_COMMAND
)


@contextmanager
def printed_executable(executable: str | None) -> Iterator[None]:
    """Print commands with ``executable``; ``None`` keeps the project launcher."""
    token = _EXECUTABLE.set(
        shlex.quote(executable) if executable else PROJECT_LAUNCHER_COMMAND
    )
    try:
        yield
    finally:
        _EXECUTABLE.reset(token)


def ww_command() -> str:
    """How a printed command invokes ww, as the command's first word."""
    return _EXECUTABLE.get()
