# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude Code.

Hooks: https://code.claude.com/docs/en/hooks; permissions:
https://code.claude.com/docs/en/permissions.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
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
from .usage import (
    Event,
    LogFile,
    Reading,
    Spawn,
    TokenCounts,
    Usage,
    UsageReader,
    count,
    lines_from,
    within,
    ww_calls,
)


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


class ClaudeCodeUsage(UsageReader):
    """Claude Code's session transcripts.

    A session is ``projects/<directory>/<session>.jsonl``; each subagent it
    starts writes ``<session>/subagents/agent-<id>.jsonl`` beside it, with a
    ``.meta.json`` naming the tool call that started it.  Every assistant line
    carries ``message.usage``, repeated on each line of the same message, so a
    message counts once by its ID.  Subagents run with their parent's
    session ID, so the session ID finds them all.
    """

    # How many message IDs a read remembers to skip a message's later lines.
    _REMEMBERED = 64

    def homes(self, environ: Mapping[str, str]) -> tuple[Path, ...]:
        configured = environ.get("CLAUDE_CONFIG_DIR")
        return (Path(configured) if configured else Path.home() / ".claude",)

    def files(self, session: str, roots: tuple[Path, ...]) -> tuple[LogFile, ...]:
        if not _IDENTIFIER.fullmatch(session):
            return ()
        for root in roots:
            for pattern in (f"projects/*/{session}.jsonl", f"*/{session}.jsonl"):
                for own in sorted(root.glob(pattern)):
                    if own.is_file():
                        return (
                            LogFile(session, own),
                            *self._subagents(session, own.parent / session),
                        )
        return ()

    def _subagents(self, session: str, directory: Path) -> list[LogFile]:
        found = []
        for path in sorted((directory / "subagents").glob("agent-*.jsonl")):
            meta = _json_file(path.with_name(path.stem + ".meta.json"))
            depth = meta.get("spawnDepth")
            tool = meta.get("toolUseId")
            found.append(
                LogFile(
                    f"{session}/{path.stem}",
                    path,
                    depth=depth if isinstance(depth, int) else 1,
                    spawn_tool=tool if isinstance(tool, str) else None,
                )
            )
        return sorted(found, key=lambda file: file.depth)

    def read(self, file: LogFile, offset: int, carry: dict[str, Any]) -> Reading:
        """A message's commands come before its tokens.

        Claude Code writes a message as one line per content block, the
        tool calls after the thinking and text, each line with the message's
        whole usage.  A response that runs a ww command did its work for the
        task that command names, so each message's events are given once it
        ends: its ww commands and subagent starts first, then its tokens.
        """
        seen = list(carry.get("seen", []))
        reading = Reading(offset=offset)
        message_id: object = None
        held: list[Event] = []
        usage: Usage | None = None

        def flush() -> None:
            nonlocal held, usage
            reading.events.extend(held)
            if usage is not None:
                reading.events.append(usage)
            held, usage = [], None

        for position, line in lines_from(file.path, offset):
            reading.offset = position
            if reading.directory is None and isinstance(line.get("cwd"), str):
                reading.directory = line["cwd"]
            message = line.get("message")
            at = line.get("timestamp")
            if line.get("type") != "assistant" or not isinstance(message, dict):
                if line.get("type") in {"user", "assistant"}:
                    flush()
                continue
            if not isinstance(at, str):
                continue
            identifier = message.get("id")
            if identifier != message_id:
                flush()
                message_id = identifier
            figures = message.get("usage")
            if isinstance(figures, dict) and identifier not in seen:
                seen = [*seen, identifier][-self._REMEMBERED :]
                usage = Usage(at, text_value(message.get("model")), _counts(figures))
            held.extend(_tool_events(message.get("content"), at))
        flush()
        reading.carry = {"seen": seen}
        return reading

    def sessions_in(self, roots: tuple[Path, ...], directory: Path) -> tuple[str, ...]:
        # A project directory is named after the session's working directory
        # with every other character replaced; worktrees below the root share
        # its prefix. The name narrows the search, the logged ``cwd`` decides.
        prefix = re.sub(r"[^A-Za-z0-9]", "-", str(directory))
        found = []
        for root in roots:
            for base in (root / "projects", root):
                for project in sorted(base.glob(prefix + "*")):
                    for path in sorted(project.glob("*.jsonl")):
                        if within(_logged_directory(path), directory):
                            found.append(path.stem)
        return tuple(dict.fromkeys(found))


def _counts(usage: dict[str, Any]) -> TokenCounts:
    details = usage.get("output_tokens_details")
    return TokenCounts(
        input=count(usage.get("input_tokens")),
        cache_write=count(usage.get("cache_creation_input_tokens")),
        cache_read=count(usage.get("cache_read_input_tokens")),
        output=count(usage.get("output_tokens")),
        reasoning=count(
            details.get("thinking_tokens") if isinstance(details, dict) else None
        ),
    )


def _tool_events(content: object, at: str) -> list[Event]:
    """The ww commands a message runs through Bash, and the subagents it starts."""
    events: list[Event] = []
    for block in content if isinstance(content, list) else ():
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_input = block.get("input")
        if block.get("name") in _SPAWN_TOOLS and isinstance(block.get("id"), str):
            events.append(Spawn(at, block["id"]))
        elif block.get("name") == "Bash" and isinstance(tool_input, dict):
            command = tool_input.get("command")
            if isinstance(command, str):
                events.extend(ww_calls(command, at))
    return events


# The tools through which Claude Code starts a subagent.
_SPAWN_TOOLS = frozenset({"Agent", "Task"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][\w.-]*")


def _json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _logged_directory(path: Path) -> str | None:
    """The first ``cwd`` among a transcript's opening lines."""
    try:
        with path.open("rb") as handle:
            for _, raw in zip(range(20), handle, strict=False):
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(line, dict) and isinstance(line.get("cwd"), str):
                    return str(line["cwd"])
    except OSError:
        return None
    return None


class ClaudeCode(Agent):
    key = "claudecode"
    title = "Claude Code"
    layout = Layout(".claude", instruction_file="CLAUDE.md")
    hooks = ClaudeCodeHooks()
    permissions = ClaudePermissions()
    session_variable = "CLAUDE_CODE_SESSION_ID"
    usage = ClaudeCodeUsage()
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
