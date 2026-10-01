# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-agent hook protocols: what each agent sends ww, and what it expects back.

Every decision a hook makes belongs to ww (see :mod:`ww.hooks.runtime`).  An
adapter only translates: it reads the agent's JSON payload into a
:class:`HookPayload`, renders ww's answer in the agent's reply shape, and
says where and how the agent registers project hooks.  Adding an agent is
one more adapter in :data:`HOOK_AGENTS`.

The protocols were taken from each agent's documentation (September 2026):
Claude Code https://code.claude.com/docs/en/hooks, Codex
https://learn.chatgpt.com/docs/hooks, Cursor https://cursor.com/docs/hooks,
and Antigravity https://antigravity.google/docs/hooks/.
"""

from __future__ import annotations

import json
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
    # that needs no context (Antigravity's pre-invocation hook runs per call).
    wants_context: bool = True
    # The agent already continued once because of a stop hook.
    continued: bool = False
    # The stop is really an interruption: the user aborted the turn.
    interrupted: bool = False
    # The stop is a worker's, which the session delegated a step to, rather
    # than the session's own; only agents that tell the two apart set it.
    from_worker: bool = False
    # The agent's own word for why a session ended or was interrupted.
    reason: str | None = None


@dataclass(frozen=True)
class Registration:
    """One native event ww listens to, and the ww event it maps onto."""

    native: str
    event: HookEvent
    timeout: int = 10


class HookAgent:
    """The protocol of one agent; subclasses fill in what differs."""

    name: str
    # The project file the agent reads hooks from, relative to the root.
    settings_file: str
    # The project file the agent keeps out of version control, if it has one.
    local_settings_file: str | None = None
    # Where the agent reads hooks for every project, for agents without one.
    user_settings_file: str | None = None
    registrations: tuple[Registration, ...]
    # The project file the agent reads command permissions from, for agents
    # whose permission format ww knows; see :meth:`permissions`.
    permissions_file: str | None = None

    def permissions(self, commands: tuple[str, ...]) -> dict[str, Any] | None:
        """The ``permissions_file`` content that allows ``commands`` unasked.

        ``None`` for an agent whose permission format ww does not know.
        """
        return None

    def command(self, event: HookEvent) -> str:
        """The command the agent runs; it must work from task worktrees too."""
        return (
            f'"$(git rev-parse --show-toplevel 2>/dev/null || pwd)"/ww hook '
            f'{event} --agent {self.name}'
        )

    def owns(self, command: object) -> bool:
        return (
            isinstance(command, str)
            and HOOK_SIGNATURE in command
            and f"--agent {self.name}" in command
        )

    def parse(self, event: HookEvent, payload: dict[str, Any]) -> HookPayload:
        return HookPayload(directory=_path(payload.get("cwd")))

    def context_reply(self, text: str) -> str:
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": text,
                }
            }
        )

    def continue_reply(self, message: str) -> str:
        return json.dumps({"decision": "block", "reason": message})

    # Registration in the agent's hooks file

    def entries(self) -> dict[str, Any]:
        """ww's own entries, in the file's shape, keyed by native event."""
        return {
            registration.native: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": self.command(registration.event),
                            "timeout": registration.timeout,
                        }
                    ]
                }
            ]
            for registration in self.registrations
        }

    def merged(self, document: dict[str, Any]) -> dict[str, Any]:
        """``document`` with ww's entries in place of any earlier ones."""
        result = self.without(document)
        hooks = result.setdefault("hooks", {})
        for native, groups in self.entries().items():
            hooks.setdefault(native, []).extend(groups)
        return result

    def without(self, document: dict[str, Any]) -> dict[str, Any]:
        """``document`` with every entry ww registered removed."""
        result: dict[str, Any] = json.loads(json.dumps(document))
        hooks = result.get("hooks")
        if not isinstance(hooks, dict):
            return result
        for native in list(hooks):
            groups = hooks[native]
            if not isinstance(groups, list):
                continue
            kept = [group for group in groups if not self._own_group(group)]
            if kept:
                hooks[native] = kept
            else:
                del hooks[native]
        return result

    def _own_group(self, group: object) -> bool:
        if not isinstance(group, dict):
            return False
        handlers = group.get("hooks")
        return (
            isinstance(handlers, list)
            and bool(handlers)
            and all(
                isinstance(handler, dict) and self.owns(handler.get("command"))
                for handler in handlers
            )
        )


class ClaudeCode(HookAgent):
    name = "claudecode"
    settings_file = ".claude/settings.json"
    local_settings_file = ".claude/settings.local.json"
    permissions_file = ".claude/settings.json"
    # No matcher on SessionStart, so it fires for every source, compaction
    # included. SubagentStart is left alone: workers get only their bootstrap.
    registrations = (
        Registration("SessionStart", "session-start"),
        Registration("Stop", "stop"),
        Registration("SubagentStop", "stop"),
        # Esc fires no hook in Claude Code; the end of a session does.
        Registration("SessionEnd", "interrupt", 5),
    )

    def command(self, event: HookEvent) -> str:
        return f'"$CLAUDE_PROJECT_DIR"/ww hook {event} --agent {self.name}'

    def permissions(self, commands: tuple[str, ...]) -> dict[str, Any] | None:
        # A trailing " *" allows the command with any arguments and alone
        # (https://code.claude.com/docs/en/permissions, "Wildcard patterns").
        allowed = [f"Bash({command} *)" for command in commands]
        return {"permissions": {"allow": allowed}}

    def parse(self, event: HookEvent, payload: dict[str, Any]) -> HookPayload:
        return HookPayload(
            directory=_path(payload.get("cwd")),
            source=_text(payload.get("source")),
            continued=payload.get("stop_hook_active") is True,
            reason=_text(payload.get("reason")),
            from_worker=payload.get("hook_event_name") == "SubagentStop",
        )


class Codex(HookAgent):
    name = "codex"
    settings_file = ".codex/hooks.json"
    user_settings_file = "~/.codex/hooks.json"
    registrations = (
        Registration("SessionStart", "session-start"),
        Registration("Stop", "stop"),
        Registration("SubagentStop", "stop"),
        # Codex allows its interrupt hook at most three seconds.
        Registration("Interrupt", "interrupt", 3),
        Registration("SessionEnd", "interrupt", 5),
    )

    def parse(self, event: HookEvent, payload: dict[str, Any]) -> HookPayload:
        native = _text(payload.get("hook_event_name"))
        return HookPayload(
            directory=_path(payload.get("cwd")),
            source=_text(payload.get("source")),
            continued=payload.get("stop_hook_active") is True,
            reason=(
                "interrupted" if native == "Interrupt" else _text(payload.get("reason"))
            ),
            from_worker=native == "SubagentStop",
        )


class Cursor(HookAgent):
    name = "cursor"
    settings_file = ".cursor/hooks.json"
    user_settings_file = "~/.cursor/hooks.json"
    registrations = (
        Registration("sessionStart", "session-start"),
        Registration("stop", "stop"),
        Registration("subagentStop", "stop"),
        Registration("sessionEnd", "interrupt", 5),
    )

    def command(self, event: HookEvent) -> str:
        # Cursor runs project hooks from the project root.
        return f"./ww hook {event} --agent {self.name}"

    def parse(self, event: HookEvent, payload: dict[str, Any]) -> HookPayload:
        roots = payload.get("workspace_roots")
        directory = _path(roots[0]) if isinstance(roots, list) and roots else None
        status = _text(payload.get("status"))
        loop_count = payload.get("loop_count")
        return HookPayload(
            directory=directory,
            continued=isinstance(loop_count, int) and loop_count > 0,
            interrupted=status == "aborted",
            reason=_text(payload.get("reason")) or status,
            from_worker=payload.get("hook_event_name") == "subagentStop",
        )

    def context_reply(self, text: str) -> str:
        return json.dumps({"additional_context": text})

    def continue_reply(self, message: str) -> str:
        return json.dumps({"followup_message": message})

    def entries(self) -> dict[str, Any]:
        return {
            registration.native: [
                {
                    "command": self.command(registration.event),
                    "timeout": registration.timeout,
                }
            ]
            for registration in self.registrations
        }

    def merged(self, document: dict[str, Any]) -> dict[str, Any]:
        result = super().merged(document)
        result.setdefault("version", 1)
        return result

    def without(self, document: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(json.dumps(document))
        hooks = result.get("hooks")
        if not isinstance(hooks, dict):
            return result
        for native in list(hooks):
            handlers = hooks[native]
            if not isinstance(handlers, list):
                continue
            kept = [
                handler
                for handler in handlers
                if not (isinstance(handler, dict) and self.owns(handler.get("command")))
            ]
            if kept:
                hooks[native] = kept
            else:
                del hooks[native]
        return result


# Termination reasons an Antigravity Stop hook reports for a normal end. The
# documentation names these; any reason naming a cancellation is treated as
# an interruption, and every other one as a normal stop.
_ANTIGRAVITY_INTERRUPTS = ("cancel", "interrupt", "abort", "user")


class Antigravity(HookAgent):
    name = "antigravity"
    settings_file = ".agents/hooks.json"
    user_settings_file = "~/.gemini/config/hooks.json"
    # Antigravity groups hooks under a name of the owner's choosing.
    group = "ww"
    # There is no session-start event: the first pre-invocation of a
    # conversation is its start.
    registrations = (
        Registration("PreInvocation", "session-start"),
        Registration("Stop", "stop"),
    )

    def parse(self, event: HookEvent, payload: dict[str, Any]) -> HookPayload:
        paths = payload.get("workspacePaths")
        directory = _path(paths[0]) if isinstance(paths, list) and paths else None
        reason = _text(payload.get("terminationReason"))
        return HookPayload(
            directory=directory,
            wants_context=payload.get("invocationNum", 0) == 0,
            interrupted=(
                reason is not None
                and any(word in reason.lower() for word in _ANTIGRAVITY_INTERRUPTS)
            ),
            reason=reason,
        )

    def context_reply(self, text: str) -> str:
        # An ephemeral message is not kept in the trajectory, so the context
        # never accumulates even if the counter restarts every turn.
        return json.dumps({"injectSteps": [{"ephemeralMessage": text}]})

    def continue_reply(self, message: str) -> str:
        return json.dumps({"decision": "continue", "reason": message})

    def merged(self, document: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(json.dumps(document))
        result[self.group] = {"enabled": True, **self.entries()}
        return result

    def without(self, document: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(json.dumps(document))
        result.pop(self.group, None)
        return result


HOOK_AGENTS: dict[str, HookAgent] = {
    agent.name: agent for agent in (ClaudeCode(), Codex(), Cursor(), Antigravity())
}


def hook_agent(name: str) -> HookAgent:
    agent = HOOK_AGENTS.get(name)
    if agent is None:
        raise KeyError(name)
    return agent


def _path(value: object) -> Path | None:
    return Path(value) if isinstance(value, str) and value else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
