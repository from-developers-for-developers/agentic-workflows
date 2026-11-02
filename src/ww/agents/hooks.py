# SPDX-License-Identifier: GPL-3.0-or-later
"""Hook protocols: what an agent sends ww, and what it expects back.

Every decision a hook makes belongs to ww (see :mod:`ww.hooks.runtime`).  A
protocol only translates: it reads the agent's JSON payload into a
:class:`HookPayload`, renders ww's answer in the agent's reply shape, and
says where and how the agent registers project hooks.  A protocol holds no
agent identity: the name ww addresses the agent by is passed in, so the same
protocol serves every agent that shares its file shape.

The base class is the nested shape Claude Code and Codex share
(``hooks.<event>[].hooks[]``); :class:`FlatHooks` is the shape Cursor uses
(``hooks.<event>[]``).
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

HookEvent = Literal["session-start", "stop", "interrupt"]
HOOK_EVENTS: tuple[HookEvent, ...] = ("session-start", "stop", "interrupt")
# Every command ww registers carries this, so ww recognises its own entries
# among the agent's other hooks without any marker the agent would reject.
HOOK_SIGNATURE = "ww hook "


@dataclass(frozen=True)
class HookPayload:
    """What ww needs from one hook call, whatever agent sent it."""

    # The directory the agent session works in, when the agent says.
    directory: Path | None = None
    # Why the session started: startup, resume, clear, compact, or unknown.
    source: str | None = None
    # ``False`` when the agent calls the session-start hook for a later turn
    # that needs no context.
    wants_context: bool = True


@dataclass(frozen=True)
class Registration:
    """One native event ww listens to, and the ww event it maps onto."""

    native: str
    event: HookEvent
    timeout: int = 10


class HookProtocol:
    """The hook protocol of one agent; subclasses fill in what differs."""

    # The project file the agent reads hooks from, relative to the root.
    settings_file: str
    # The project file the agent keeps out of version control, if it has one.
    local_settings_file: str | None = None
    # Where the agent reads hooks for every project, for agents without one.
    user_settings_file: str | None = None
    registrations: tuple[Registration, ...]

    def command(self, agent: str, event: HookEvent) -> str:
        """The command the agent runs; it must work from task worktrees too."""
        return (
            f'"$(git rev-parse --show-toplevel 2>/dev/null || pwd)"/ww hook '
            f"{event} --agent {shlex.quote(agent)}"
        )

    def owns(self, agent: str, command: object) -> bool:
        return (
            isinstance(command, str)
            and HOOK_SIGNATURE in command
            and f"--agent {shlex.quote(agent)}" in command
        )

    def parse(self, event: HookEvent, payload: dict[str, Any]) -> HookPayload:
        return HookPayload(directory=path_value(payload.get("cwd")))

    def context_reply(self, text: str) -> str:
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": text,
                }
            }
        )

    # Registration in the agent's hooks file

    def entries(self, agent: str) -> dict[str, Any]:
        """ww's own entries, in the file's shape, keyed by native event."""
        return {
            registration.native: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": self.command(agent, registration.event),
                            "timeout": registration.timeout,
                        }
                    ]
                }
            ]
            for registration in self.registrations
        }

    def merged(self, agent: str, document: dict[str, Any]) -> dict[str, Any]:
        """``document`` with ww's entries in place of any earlier ones."""
        result = self.without(agent, document)
        hooks = result.setdefault("hooks", {})
        for native, groups in self.entries(agent).items():
            hooks.setdefault(native, []).extend(groups)
        return result

    def without(self, agent: str, document: dict[str, Any]) -> dict[str, Any]:
        """``document`` with every entry ww registered removed."""
        result: dict[str, Any] = json.loads(json.dumps(document))
        hooks = result.get("hooks")
        if not isinstance(hooks, dict):
            return result
        for native in list(hooks):
            entries = hooks[native]
            if not isinstance(entries, list):
                continue
            kept = [entry for entry in entries if not self._own_entry(agent, entry)]
            if kept:
                hooks[native] = kept
            else:
                del hooks[native]
        return result

    def _own_entry(self, agent: str, group: object) -> bool:
        if not isinstance(group, dict):
            return False
        handlers = group.get("hooks")
        return (
            isinstance(handlers, list)
            and bool(handlers)
            and all(
                isinstance(handler, dict) and self.owns(agent, handler.get("command"))
                for handler in handlers
            )
        )


class FlatHooks(HookProtocol):
    """A hooks file listing each handler directly under its event."""

    # The version key the file declares.
    version = 1

    def entries(self, agent: str) -> dict[str, Any]:
        return {
            registration.native: [
                {
                    "command": self.command(agent, registration.event),
                    "timeout": registration.timeout,
                }
            ]
            for registration in self.registrations
        }

    def merged(self, agent: str, document: dict[str, Any]) -> dict[str, Any]:
        result = super().merged(agent, document)
        result.setdefault("version", self.version)
        return result

    def _own_entry(self, agent: str, handler: object) -> bool:
        return isinstance(handler, dict) and self.owns(agent, handler.get("command"))


def path_value(value: object) -> Path | None:
    return Path(value) if isinstance(value, str) and value else None


def text_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
