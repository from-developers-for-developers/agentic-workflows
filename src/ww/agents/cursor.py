# SPDX-License-Identifier: GPL-3.0-or-later
"""Cursor.

Hooks: https://cursor.com/docs/hooks.  The question tool's name is taken
from the askmux question-tool matrix (https://github.com/iShaldam/askmux,
MIT, Copyright (c) 2026 iShaldam).
"""

from __future__ import annotations

import json
import shlex
from typing import Any

from .base import Agent
from .hooks import FlatHooks, HookEvent, HookPayload, Registration, path_value
from .interaction import question_tool
from .layout import SHARED_INSTRUCTION_FILE, Layout


class CursorHooks(FlatHooks):
    settings_file = ".cursor/hooks.json"
    user_settings_file = "~/.cursor/hooks.json"
    registrations = (Registration("sessionStart", "session-start"),)

    def command(self, agent: str, event: HookEvent) -> str:
        # Cursor runs project hooks from the project root.
        return f"./ww hook {event} --agent {shlex.quote(agent)}"

    def parse(self, event: HookEvent, payload: dict[str, Any]) -> HookPayload:
        roots = payload.get("workspace_roots")
        directory = path_value(roots[0]) if isinstance(roots, list) and roots else None
        return HookPayload(directory=directory)

    def context_reply(self, text: str) -> str:
        return json.dumps({"additional_context": text})


class Cursor(Agent):
    key = "cursor"
    title = "Cursor"
    layout = Layout(
        ".cursor",
        instruction_file=SHARED_INSTRUCTION_FILE,
        markers=(".cursor/rules*", ".cursorrules"),
    )
    hooks = CursorHooks()
    choices = question_tool(
        "AskQuestion", "A free-form answer is a comment, not a choice."
    )
