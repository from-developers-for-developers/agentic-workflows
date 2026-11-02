# SPDX-License-Identifier: GPL-3.0-or-later
"""Where an agent keeps its project files, and what ww finds there.

Skills live in ``<directory>/skills/<name>/SKILL.md``, slash commands in
``<directory>/commands/*.md``, and profiles in ``<directory>/agents/``.  The
shared ``.agents`` directory holds skills every agent may use, and
``AGENTS.md`` is the instruction file shared by every agent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Skills and commands shared by every agent with a project directory.
SHARED_DIRECTORY = ".agents"
# The instruction file every agent reads, whichever agents are set up.
SHARED_INSTRUCTION_FILE = "AGENTS.md"


@dataclass(frozen=True)
class Layout:
    """An agent's project directory and the files that show it is set up."""

    directory: str
    # The instruction file the agent reads besides ``AGENTS.md``, if any.
    instruction_file: str | None = None
    # Paths or globs, relative to the root, that show the agent is configured
    # in a project; the directory when none are given.
    markers: tuple[str, ...] = ()

    @property
    def evidence(self) -> tuple[str, ...]:
        return self.markers or (self.directory,)

    def instruction_files(self, root: Path) -> tuple[str, ...]:
        """The instruction files this agent reads in ``root``, if it is set up.

        The agent counts as set up when its directory or its own instruction
        file exists.
        """
        own = self.instruction_file
        if own is None or own == SHARED_INSTRUCTION_FILE:
            return ()
        if (root / self.directory).is_dir() or (root / own).exists():
            return (own,)
        return ()


def instruction_files(root: Path, layouts: Iterable[Layout | None]) -> tuple[str, ...]:
    """``AGENTS.md`` and the own instruction file of every agent set up in ``root``."""
    names = [SHARED_INSTRUCTION_FILE]
    for layout in layouts:
        if layout is not None:
            names.extend(layout.instruction_files(root))
    return tuple(dict.fromkeys(names))


@dataclass(frozen=True)
class AvailableActions:
    """Project-local action names available to one agent integration."""

    skills: frozenset[str]
    slash_commands: frozenset[str]


NO_ACTIONS = AvailableActions(frozenset(), frozenset())


class AgentDiscovery:
    """Discover skills, commands, and profiles in one agent's directories."""

    def __init__(self, root: Path, layout: Layout | None) -> None:
        self.root = root
        self.layout = layout

    def available(self) -> AvailableActions:
        """Return normalized skill and slash-command names; none without a layout."""
        if self.layout is None:
            return NO_ACTIONS
        skills: set[str] = set()
        commands: set[str] = set()
        for root in (self.root / SHARED_DIRECTORY, self.root / self.layout.directory):
            if not root.is_dir():
                continue
            for path in root.rglob("SKILL.md"):
                if "skills" in path.parts:
                    skills.add(path.parent.name)
            for path in root.rglob("*.md"):
                if "commands" in path.parts:
                    commands.add(path.stem)
        return AvailableActions(frozenset(skills), frozenset(commands))

    def profile(self, name: str) -> Path | None:
        """Return a project-local profile from the agent's own directory."""
        if self.layout is None:
            return None
        profiles = self.root / self.layout.directory / "agents"
        if not profiles.is_dir():
            return None
        return next(
            (
                path
                for path in profiles.rglob("*")
                if path.is_file() and path.stem == name
            ),
            None,
        )
