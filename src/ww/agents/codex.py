# SPDX-License-Identifier: GPL-3.0-or-later
"""Codex.

Hooks: https://learn.chatgpt.com/docs/hooks; skills, mentioned with ``$``:
https://developers.openai.com/codex/skills.
"""

from __future__ import annotations

from typing import Any

from .base import Agent
from .hooks import (
    HookEvent,
    HookPayload,
    HookProtocol,
    Registration,
    path_value,
    text_value,
)
from .interaction import ChoiceMechanism
from .layout import SHARED_INSTRUCTION_FILE, Layout


class CodexHooks(HookProtocol):
    settings_file = ".codex/hooks.json"
    user_settings_file = "~/.codex/hooks.json"
    registrations = (Registration("SessionStart", "session-start"),)

    def parse(self, event: HookEvent, payload: dict[str, Any]) -> HookPayload:
        return HookPayload(
            directory=path_value(payload.get("cwd")),
            source=text_value(payload.get("source")),
        )


class Codex(Agent):
    key = "codex"
    title = "Codex"
    layout = Layout(".codex", instruction_file=SHARED_INSTRUCTION_FILE)
    hooks = CodexHooks()
    # Outside plan mode Codex's question tool is asynchronous and its form
    # vanishes when the turn ends, so the agent follows the schema it has.
    choices = ChoiceMechanism(
        "host question tool",
        "Present the matter in your reply first. Inspect the host's available "
        "question-tool schema and follow its supported fields, using structured "
        "options when offered and a text-only question only when required. "
        "Keep the choice pending until the operator explicitly answers; a "
        "timeout, dismissal, or preselected value is not an answer. If no "
        "suitable question tool is available, present the choices as a numbered "
        "list and ask for the number or label.",
    )
    skill_prefix = "$"
