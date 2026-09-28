# SPDX-License-Identifier: GPL-3.0-or-later
"""Register ww's hooks in an agent's project hooks file, or take them out.

The shared project file is the default, since the hooks call the project's
tracked launcher; ``local`` writes the file an agent keeps out of version
control instead, for agents that have one (Claude Code).

Installing merges ww's entries into whatever the file already holds and
leaves every other entry alone; ww recognises its own entries by their
command, so installing twice changes nothing and uninstalling removes
exactly what ww added.  When the file cannot be read or written, the error
carries the path and the snippet to add by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ww.errors import StateError
from ww.storage import Storage

from .agents import HookAgent

InstallAction = Literal["installed", "unchanged", "removed", "absent"]


class HookInstallError(StateError):
    """The hooks file could not be updated; the message says how to do it."""


@dataclass(frozen=True)
class HookInstallation:
    agent: str
    path: str
    action: InstallAction

    @property
    def changed(self) -> bool:
        return self.action in {"installed", "removed"}


def hook_snippet(agent: HookAgent) -> str:
    """ww's entries alone, in the shape the agent's hooks file takes."""
    return json.dumps(agent.merged({}), indent=2) + "\n"


def hooks_file(agent: HookAgent, local: bool = False) -> str:
    """The project file ww writes the agent's hooks to.

    ``local`` chooses the file the agent keeps out of version control, so
    the hooks stay one person's choice; only some agents have one.
    """
    if not local:
        return agent.settings_file
    if agent.local_settings_file is None:
        elsewhere = (
            f"; hooks for every project live in {agent.user_settings_file}"
            if agent.user_settings_file
            else ""
        )
        raise HookInstallError(
            f"{agent.name} has no project-local hooks file, so --local cannot "
            f"apply to it; its project file is {agent.settings_file}{elsewhere}"
        )
    return agent.local_settings_file


def manual_instructions(agent: HookAgent, local: bool = False) -> str:
    """How to add ww's hooks for ``agent`` when ww cannot do it."""
    return (
        f"Merge this into {hooks_file(agent, local)} (create the file if it does "
        f"not exist):\n\n{hook_snippet(agent)}"
    )


def install_hooks(
    storage: Storage, agent: HookAgent, *, local: bool = False
) -> HookInstallation:
    relative = hooks_file(agent, local)
    path = storage.root / relative
    document = _read(path, agent, local)
    merged = agent.merged(document)
    if merged == document:
        return HookInstallation(agent.name, relative, "unchanged")
    _write(storage, path, merged, agent, local)
    return HookInstallation(agent.name, relative, "installed")


def uninstall_hooks(
    storage: Storage, agent: HookAgent, *, local: bool = False
) -> HookInstallation:
    relative = hooks_file(agent, local)
    path = storage.root / relative
    if not path.exists():
        return HookInstallation(agent.name, relative, "absent")
    document = _read(path, agent, local)
    remaining = agent.without(document)
    if remaining == document:
        return HookInstallation(agent.name, relative, "absent")
    _write(storage, path, remaining, agent, local)
    return HookInstallation(agent.name, relative, "removed")


def hooks_installed(storage: Storage, agent: HookAgent, *, local: bool = False) -> bool:
    """Whether the agent's hooks file already holds ww's current entries."""
    path = storage.root / hooks_file(agent, local)
    try:
        document = _read(path, agent, local)
    except HookInstallError:
        return False
    return path.exists() and agent.merged(document) == document


def registered_elsewhere(
    storage: Storage, agent: HookAgent, *, local: bool = False
) -> str | None:
    """The agent's other project file, when it already holds ww's hooks.

    Hooks registered in both the shared and the local file fire twice for
    every event, so the session-start context lands twice in the context.
    """
    if agent.local_settings_file is None:
        return None
    other = hooks_file(agent, not local)
    path = storage.root / other
    try:
        document = _read(path, agent, not local)
    except HookInstallError:
        return None
    return other if agent.without(document) != document else None


def _read(path: Path, agent: HookAgent, local: bool) -> dict[str, Any]:
    if not path.exists():
        return {}
    relative = hooks_file(agent, local)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HookInstallError(
            f"cannot read {relative}: {error}. " + manual_instructions(agent, local)
        ) from error
    if not isinstance(value, dict):
        raise HookInstallError(
            f"{relative} does not hold a JSON object. "
            + manual_instructions(agent, local)
        )
    return value


def _write(
    storage: Storage,
    path: Path,
    document: dict[str, Any],
    agent: HookAgent,
    local: bool,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        storage.locks.atomic_write(path, json.dumps(document, indent=2) + "\n")
    except OSError as error:
        raise HookInstallError(
            f"cannot write {hooks_file(agent, local)}: {error}. "
            + manual_instructions(agent, local)
        ) from error
