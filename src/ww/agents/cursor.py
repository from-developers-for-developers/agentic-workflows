# SPDX-License-Identifier: GPL-3.0-or-later
"""Cursor.

Hooks: https://cursor.com/docs/hooks.  The question tool's name is taken
from the askmux question-tool matrix (https://github.com/iShaldam/askmux,
MIT, Copyright (c) 2026 iShaldam).
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from typing import Any

from .base import Agent
from .headless import HeadlessLauncher, ModelCatalog, Output, StreamFacts
from .hooks import (
    FlatHooks,
    HookEvent,
    HookPayload,
    Registration,
    path_value,
    text_value,
)
from .interaction import question_tool
from .launch import LaunchRequest
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


class CursorHeadless(HeadlessLauncher):
    """``cursor-agent -p``: https://cursor.com/docs/cli/headless.

    Cursor takes no reasoning setting: a model's variants carry it in their
    names, such as ``sonnet-4-thinking``.  ``cursor-agent models`` lists the
    account's models, one per line (an account may have none listed).
    ``status`` can report a login that ``-p`` then refuses ("unable to fetch
    user details"), so only a plain answer counts.  The plan and ask modes
    cannot run commands, so ``read-only`` is not offered: the worker could
    not run ``./ww``.  ``-p`` may write files and run commands; ``--force``
    also runs the commands a project's permissions do not allow.
    """

    agent = "cursor"
    binary = "cursor-agent"
    minimum_version = (2026, 6, 11)
    tested_up_to = (2026, 6)
    reasoning_levels = None
    lists_models = True
    permission_levels = ("edit", "full")
    owned_flags = frozenset(
        {
            "-p",
            "--print",
            "--output-format",
            "--trust",
            "--workspace",
            "--model",
            "--force",
            "--yolo",
            "--resume",
            "--mode",
            "--plan",
        }
    )

    def auth_command(self, binary: str) -> list[str]:
        return [binary, "status"]

    def logged_in(self, output: Output) -> bool | None:
        text = output.text.lower()
        if "not logged in" in text or "authentication required" in text:
            return False
        if "unable to fetch" in text:
            return None
        if output.returncode == 0 and "logged in" in text:
            return True
        return None

    def catalog_command(self, binary: str) -> list[str]:
        return [binary, "models"]

    def parse_catalog(self, output: Output) -> ModelCatalog | None:
        if output.returncode != 0:
            return None
        found: dict[str, tuple[str, ...] | None] = {}
        for line in output.text.splitlines():
            match = _MODEL_LINE.match(line)
            if match:
                found[match.group(1)] = None
        return ModelCatalog(found)

    def _options(self, request: LaunchRequest) -> list[str]:
        model, _ = self.model_arguments(request)
        options = [
            "--output-format",
            "stream-json",
            "--trust",
            "--workspace",
            str(request.working_directory),
        ]
        if model is not None:
            options += ["--model", model]
        if request.settings.permissions == "full":
            options.append("--force")
        return [*options, *request.settings.args]

    def launch_command(self, binary: str, request: LaunchRequest) -> list[str]:
        return [binary, "-p", *self._options(request), "--", request.prompt]

    def resume_command(
        self, binary: str, request: LaunchRequest, session_id: str
    ) -> list[str]:
        return [
            binary,
            "-p",
            "--resume",
            session_id,
            *self._options(request),
            "--",
            request.prompt,
        ]

    def stream_facts(self, line: Mapping[str, Any]) -> StreamFacts:
        session = text_value(line.get("session_id"))
        usage = line.get("usage")
        if line.get("type") == "result" and isinstance(usage, dict):
            return StreamFacts(session, usage=_stream_usage(usage))
        return StreamFacts(session, model=text_value(line.get("model")))


# A model line: its name first, optionally followed by " - " and a title.
_MODEL_LINE = re.compile(r"^\s*([A-Za-z0-9][\w.-]*)(?:\s+-\s+.*)?\s*$")


def _stream_usage(usage: dict[str, Any]) -> TokenCounts:
    read = _first(usage, _CACHE_READ)
    written = _first(usage, _CACHE_WRITE)
    return TokenCounts(
        input=max(0, count(usage.get("input_tokens")) - read - written),
        cache_write=written,
        cache_read=read,
        output=count(usage.get("output_tokens")),
    )


class Cursor(Agent):
    key = "cursor"
    title = "Cursor"
    launchers = (CursorHeadless(),)
    layout = Layout(
        ".cursor",
        instruction_file=SHARED_INSTRUCTION_FILE,
        markers=(".cursor/rules*", ".cursorrules"),
    )
    hooks = CursorHooks()
    choices = question_tool(
        "AskQuestion", "A free-form answer is a comment, not a choice."
    )
