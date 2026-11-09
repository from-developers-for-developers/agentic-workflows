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
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

from ww.errors import ConfigurationError

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
    ``None`` is the adapter's own default name.  Settings are read-only once
    parsed: nothing changes ``env`` or ``models`` after that.
    """

    binary: str | None = None
    permissions: Permission = "edit"
    timeout_minutes: float | None = None
    idle_timeout_minutes: float | None = None
    max_parallel: int | None = None
    fallback_model: str | None = None
    max_budget_usd: float | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    # Ignore the operator's own configuration of the agent.
    isolated: bool = False
    # Appended to the command as given.
    args: tuple[str, ...] = ()
    # Names a step's ``model`` may use, mapped to the agent's own.
    models: Mapping[str, str] = field(default_factory=dict)

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


# The keys of ``agents.<name>.headless`` in ``ww.json``.
HEADLESS_KEYS = frozenset(
    {
        "binary",
        "permissions",
        "timeout_minutes",
        "idle_timeout_minutes",
        "max_parallel",
        "fallback_model",
        "max_budget_usd",
        "env",
        "isolated",
        "args",
        "models",
    }
)


def parse_headless_settings(data: Any, where: str) -> HeadlessSettings:
    """``agents.<name>.headless`` from ``ww.json``, every key checked."""
    if not isinstance(data, dict):
        raise ConfigurationError(f"{where} must be an object")
    unknown = set(data) - HEADLESS_KEYS
    if unknown:
        raise ConfigurationError(
            f"{where} has unknown key(s): {', '.join(sorted(map(str, unknown)))}"
        )
    permissions = data.get("permissions", "edit")
    if permissions not in PERMISSIONS:
        raise ConfigurationError(
            f"{where}.permissions must be one of: {', '.join(PERMISSIONS)}"
        )
    isolated = data.get("isolated", False)
    if not isinstance(isolated, bool):
        raise ConfigurationError(f"{where}.isolated must be true or false")
    max_parallel = data.get("max_parallel")
    if max_parallel is not None and (
        isinstance(max_parallel, bool)
        or not isinstance(max_parallel, int)
        or max_parallel < 1
    ):
        raise ConfigurationError(f"{where}.max_parallel must be a positive integer")
    args = data.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ConfigurationError(f"{where}.args must be a list of strings")
    return HeadlessSettings(
        binary=_text(data, "binary", where),
        permissions=cast(Permission, permissions),
        timeout_minutes=_positive(data, "timeout_minutes", where),
        idle_timeout_minutes=_positive(data, "idle_timeout_minutes", where),
        max_parallel=max_parallel,
        fallback_model=_text(data, "fallback_model", where),
        max_budget_usd=_positive(data, "max_budget_usd", where),
        env=_strings(data, "env", where),
        isolated=isolated,
        args=tuple(args),
        models=_strings(data, "models", where),
    )


def _text(data: dict[str, Any], key: str, where: str) -> str | None:
    value = data.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ConfigurationError(f"{where}.{key} must be a non-empty string")
    return value


def _positive(data: dict[str, Any], key: str, where: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigurationError(f"{where}.{key} must be a positive number")
    return float(value)


def _strings(data: dict[str, Any], key: str, where: str) -> dict[str, str]:
    value = data.get(key, {})
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(item, str) and item
        for name, item in value.items()
    ):
        raise ConfigurationError(
            f"{where}.{key} must be an object of non-empty strings"
        )
    return dict(value)
