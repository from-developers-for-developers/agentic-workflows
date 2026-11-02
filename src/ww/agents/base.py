# SPDX-License-Identifier: GPL-3.0-or-later
"""The agent abstraction: one coding agent ww works with.

An :class:`Agent` is an adapter: everything ww says or writes differently
for one agent lives on its subclass, and every other module asks the agent
instead of keying a table by its name.  Each part is optional and has a
neutral default, since not every agent has every part: an agent behind an
API has no project directory, no hooks, and no shell.

An agent plays two roles.  The *host* is the agent of the session running a
task (``start --agent``): it reads ww's pages, offers choices, waits for the
operator, runs ww's hooks, and has ww's skills installed.  A *requested*
agent is the one a step asks for with ``agent:``; today that request is
advice for the manager, and its :attr:`Agent.launcher` is the seam for ww
starting it itself.
"""

from __future__ import annotations

from abc import ABC
from typing import ClassVar

from .hooks import HookProtocol
from .interaction import FOREGROUND, PLAIN_TEXT, ChoiceMechanism, WaitMechanism
from .launch import Launcher
from .layout import Layout
from .permissions import Permissions


class Agent(ABC):
    """One agent integration; subclasses override only what differs.

    ``key`` names the agent within whatever provides it, and ``name`` is how
    ww addresses it, in ``--agent`` and in a step's ``agent:``: the key for a
    built-in agent, ``ext/<vendor>/<extension>/agents:<key>`` for an
    extension's.  The registry creates each agent with its name.
    """

    key: ClassVar[str]
    # How people call the agent, e.g. "Claude Code".
    title: str
    layout: Layout | None = None
    hooks: HookProtocol | None = None
    permissions: Permissions | None = None
    choices: ChoiceMechanism = PLAIN_TEXT
    waiting: WaitMechanism = FOREGROUND
    # What the operator types before a skill's name to run it.
    skill_prefix: str = "/"
    # Reserved: ww does not launch agents yet; see :mod:`ww.agents.launch`.
    launcher: Launcher | None = None

    def __init__(self, name: str | None = None) -> None:
        self.name = name or self.key

    def skill_command(self, skill: str, request: str = "") -> str:
        """What the operator types to run ``skill``, with an optional request."""
        return f"{self.skill_prefix}{skill}" + (f" {request}" if request else "")

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"
