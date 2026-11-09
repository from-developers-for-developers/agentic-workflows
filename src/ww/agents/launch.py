# SPDX-License-Identifier: GPL-3.0-or-later
"""How ww starts an agent itself.

A step names how its agent runs with ``run``: ``session`` (the default) is
today's worker, which the manager starts or plays itself; ``headless`` is
ww starting the agent's own command-line program; ``api`` is reserved for
ww calling a model's API.  Each transport other than ``session`` is a
:class:`Launcher` the agent's adapter provides, so everything a particular
agent needs, such as its flags, stays in its adapter.

A launcher answers two questions separately: how the agent is *started*
(the transport), and how its *result is recorded*.  A headless agent is an
ordinary ww worker that runs ``./ww complete`` itself; an agent behind an
API cannot run ww's commands, so ww would record its result for it.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import ClassVar, Literal

Transport = Literal["session", "headless", "api"]
TRANSPORTS: tuple[Transport, ...] = ("session", "headless", "api")
# How much a headless agent may do without asking: it cannot ask anyone.
Permission = Literal["read-only", "edit", "full"]
PERMISSIONS: tuple[Permission, ...] = ("read-only", "edit", "full")


class ResultRecording(Enum):
    """Who records a launched agent's result."""

    # The agent runs ``./ww complete`` itself, as any worker does.
    WORKER = "worker"
    # The agent cannot run ww's commands; ww records what it returned.
    WW = "ww"


@dataclass(frozen=True)
class HeadlessSettings:
    """A project's settings for running one agent headless.

    Every value has a neutral default, so an agent runs headless with no
    settings at all.  ``binary`` is a path or a name found on ``PATH``;
    ``None`` is the adapter's own default name.
    """

    binary: str | None = None
    permissions: Permission = "edit"
    timeout_minutes: float | None = None
    idle_timeout_minutes: float | None = None
    max_parallel: int | None = None
    fallback_model: str | None = None
    max_budget_usd: float | None = None
    env: dict[str, str] = field(default_factory=dict)
    # Ignore the operator's own configuration of the agent.
    isolated: bool = False
    # Appended to the command as given.
    args: tuple[str, ...] = ()
    # Names a step's ``model`` may use, mapped to the agent's own.
    models: dict[str, str] = field(default_factory=dict)

    def model(self, name: str | None) -> str | None:
        """``name`` with this agent's alias resolved."""
        return self.models.get(name, name) if name is not None else None


@dataclass(frozen=True)
class LaunchRequest:
    """What a launched agent is given: the worker bootstrap and its settings."""

    task_id: str
    # The worker's bootstrap, such as ``./ww instruction <task> --role worker``.
    prompt: str
    working_directory: Path
    model: str | None = None
    reasoning: str | None = None
    settings: HeadlessSettings = field(default_factory=HeadlessSettings)
    # ww's own state directory (``.ww``), which the worker must be able to
    # write even where the agent confines writes to its workspace.
    state_directory: Path | None = None


class Launcher(ABC):
    """How ww starts one agent through one transport."""

    transport: ClassVar[Transport]
    recording: ClassVar[ResultRecording]
