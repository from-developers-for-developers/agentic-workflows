# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude Code.

Hooks: https://code.claude.com/docs/en/hooks; permissions:
https://code.claude.com/docs/en/permissions.
"""

from __future__ import annotations

import shlex
from typing import Any

from ww.errors import StateError

from .base import Agent
from .hooks import (
    HookEvent,
    HookPayload,
    HookProtocol,
    Registration,
    path_value,
    text_value,
)
from .interaction import BACKGROUND_WAIT_SECONDS, WaitMechanism, question_tool
from .layout import Layout
from .permissions import Permissions


class ClaudeCodeHooks(HookProtocol):
    settings_file = ".claude/settings.json"
    local_settings_file = ".claude/settings.local.json"
    # No matcher on SessionStart, so it fires for every source, compaction
    # included. SubagentStart is left alone: workers get only their bootstrap.
    # ``stop`` and ``interrupt`` are no longer registered; calls from an older
    # installation are still accepted and answered with nothing.
    registrations = (Registration("SessionStart", "session-start"),)

    def command(self, agent: str, event: HookEvent) -> str:
        return f'"$CLAUDE_PROJECT_DIR"/ww hook {event} --agent {shlex.quote(agent)}'

    def parse(self, event: HookEvent, payload: dict[str, Any]) -> HookPayload:
        return HookPayload(
            directory=path_value(payload.get("cwd")),
            source=text_value(payload.get("source")),
        )


class ClaudePermissions(Permissions):
    shared_file = ".claude/settings.json"
    local_file = ".claude/settings.local.json"
    ignore_patterns = (".claude/*.local.json",)

    def rule(self, command: str) -> str:
        # A trailing " *" allows the command with any arguments and alone
        # ("Wildcard patterns").
        return f"Bash({command})"

    def merged(
        self, document: dict[str, Any], rules: tuple[str, ...]
    ) -> dict[str, Any]:
        permissions = document.get("permissions", {})
        if not isinstance(permissions, dict):
            raise StateError("permissions in the settings file must be an object")
        allowed = permissions.get("allow", [])
        if not isinstance(allowed, list):
            raise StateError("permissions.allow in the settings file must be a list")
        missing = [rule for rule in rules if rule not in allowed]
        return {
            **document,
            "permissions": {**permissions, "allow": [*allowed, *missing]},
        }


class ClaudeCode(Agent):
    key = "claudecode"
    title = "Claude Code"
    layout = Layout(".claude", instruction_file="CLAUDE.md")
    hooks = ClaudeCodeHooks()
    permissions = ClaudePermissions()
    choices = question_tool(
        "AskUserQuestion",
        "The operator picks with the keyboard; a free-form answer through "
        '"Other" is a comment, not a choice.',
    )
    waiting = WaitMechanism(
        True,
        BACKGROUND_WAIT_SECONDS,
        "Run the command in the background with your shell tool's "
        "`run_in_background` option and go on with the conversation; its "
        "output reaches you when it returns. While it runs, do not run "
        "commands that change this task, because the wait applies the answers "
        "the moment it ends; `status` and `instruction` are fine. Run it again "
        "while items remain.",
    )
