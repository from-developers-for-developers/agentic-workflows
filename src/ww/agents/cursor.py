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
from .hooks import (
    FlatHooks,
    HookEvent,
    HookPayload,
    Registration,
    path_value,
    text_value,
)
from .interaction import question_tool
from .layout import SHARED_INSTRUCTION_FILE, Layout
from .usage import TokenCounts, TurnUsage, count


class CursorHooks(FlatHooks):
    settings_file = ".cursor/hooks.json"
    user_settings_file = "~/.cursor/hooks.json"
    # ``stop`` reports the turn's tokens; ww records them and answers nothing.
    registrations = (
        Registration("sessionStart", "session-start"),
        Registration("stop", "stop"),
    )

    def command(self, agent: str, event: HookEvent) -> str:
        # Cursor runs project hooks from the project root.
        return f"./ww hook {event} --agent {shlex.quote(agent)}"

    def parse(self, event: HookEvent, payload: dict[str, Any]) -> HookPayload:
        roots = payload.get("workspace_roots")
        directory = path_value(roots[0]) if isinstance(roots, list) and roots else None
        return HookPayload(
            directory=directory,
            usage=_turn_usage(payload) if event == "stop" else None,
        )

    def context_reply(self, text: str) -> str:
        return json.dumps({"additional_context": text})


def _turn_usage(payload: dict[str, Any]) -> TurnUsage | None:
    """The turn's tokens from a stop payload, which counts the parent only.

    Cursor's input figure includes cache reads and writes.  The field names
    of the cache figures vary between releases, so each known one is read.
    """
    usage = payload.get("usage")
    figures = usage if isinstance(usage, dict) else payload
    session = text_value(payload.get("conversation_id"))
    turn = text_value(payload.get("generation_id"))
    if session is None or turn is None or "input_tokens" not in figures:
        return None
    read = _first(figures, _CACHE_READ)
    written = _first(figures, _CACHE_WRITE)
    return TurnUsage(
        session,
        turn,
        text_value(payload.get("model")),
        TokenCounts(
            input=max(0, count(figures.get("input_tokens")) - read - written),
            cache_write=written,
            cache_read=read,
            output=count(figures.get("output_tokens")),
        ),
    )


_CACHE_READ = ("cache_read_tokens", "cache_read_input_tokens", "cached_input_tokens")
_CACHE_WRITE = (
    "cache_write_tokens",
    "cache_write_input_tokens",
    "cache_creation_input_tokens",
)


def _first(figures: dict[str, Any], names: tuple[str, ...]) -> int:
    return next((count(figures[name]) for name in names if name in figures), 0)


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
