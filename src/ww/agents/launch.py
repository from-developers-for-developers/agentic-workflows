# SPDX-License-Identifier: GPL-3.0-or-later
"""The seam for ww starting an agent itself.

Reserved: ww does not launch agents yet, and no agent has a launcher.  The
intended use is a step that requests another agent than the session's own
(``agent: codex`` in a Claude Code session): ww would start that agent
headless, or call it through an API, with the same worker bootstrap a
manager hands a worker today, and the launched agent would report its own
results through ``complete``.  An agent that cannot run ww's commands, such
as a model behind an API, would need ww to record its result for it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LaunchRequest:
    """What a launched agent is given: the worker bootstrap and its settings."""

    task_id: str
    # The worker's bootstrap, such as ``./ww instruction <task> --role worker``.
    prompt: str
    working_directory: Path
    model: str | None = None
    reasoning: str | None = None


class Launcher(ABC):
    """How ww would start one agent headless or through an API."""

    @abstractmethod
    def launch(self, request: LaunchRequest) -> None:
        """Start the agent on ``request``; it reports its results through ww."""
