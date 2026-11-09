# SPDX-License-Identifier: GPL-3.0-or-later
"""Codex.

Hooks: https://learn.chatgpt.com/docs/hooks; skills, mentioned with ``$``:
https://developers.openai.com/codex/skills.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
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
from .usage import (
    Call,
    LogFile,
    Reading,
    TokenCounts,
    Usage,
    UsageReader,
    count,
    first_line,
    lines_from,
    within,
    ww_calls,
)


class CodexHooks(HookProtocol):
    settings_file = ".codex/hooks.json"
    user_settings_file = "~/.codex/hooks.json"
    registrations = (Registration("SessionStart", "session-start"),)

    def parse(self, event: HookEvent, payload: dict[str, Any]) -> HookPayload:
        return HookPayload(
            directory=path_value(payload.get("cwd")),
            source=text_value(payload.get("source")),
        )


class CodexUsage(UsageReader):
    """Codex's rollouts: ``sessions/<y>/<m>/<d>/rollout-<time>-<thread>.jsonl``.

    Every thread, a subagent's included, has its own rollout and its own
    thread ID; a subagent's first line names its parent thread.  Newer
    rollouts carry one ``token_usage_record`` per model response; older ones
    only ``token_count`` events, whose ``last_token_usage`` is the response's
    own.  Codex's input figure includes cached and cache-written tokens.
    """

    def homes(self, environ: Mapping[str, str]) -> tuple[Path, ...]:
        configured = environ.get("CODEX_HOME")
        return (Path(configured) if configured else Path.home() / ".codex",)

    def files(self, session: str, roots: tuple[Path, ...]) -> tuple[LogFile, ...]:
        if not _THREAD.fullmatch(session):
            return ()
        for root in roots:
            for base in (root / "sessions", root):
                own = sorted(base.glob(f"*/*/*/rollout-*-{session}.jsonl"))
                if own and own[0].is_file():
                    return (
                        LogFile(f"codex:{session}", own[0]),
                        *self._descendants(session, base, own[0]),
                    )
        return ()

    def _descendants(self, session: str, base: Path, own: Path) -> list[LogFile]:
        """Rollouts of threads started, directly or not, by ``session``.

        Subagents start after their parent, so only rollouts from the
        parent's day on, in name (time) order, are read, parents first.
        """
        day = own.parent
        threads = {session: 0}
        found = []
        for path in sorted(base.glob("*/*/*/rollout-*.jsonl")):
            if path.parent < day or path == own:
                continue
            header = first_line(path)
            payload = header.get("payload") if header else None
            if not isinstance(payload, dict):
                continue
            spawn = _thread_spawn(payload)
            parent = spawn.get("parent_thread_id") if spawn else None
            thread = payload.get("id")
            if parent in threads and isinstance(thread, str):
                threads[thread] = threads[parent] + 1
                found.append(
                    LogFile(
                        f"codex:{thread}",
                        path,
                        depth=threads[thread],
                        parent=f"codex:{parent}",
                        spawned_at=_text(payload.get("timestamp")),
                    )
                )
        return found

    def read(self, file: LogFile, offset: int, carry: dict[str, Any]) -> Reading:
        model = carry.get("model")
        records = bool(carry.get("records"))
        reading = Reading(offset=offset)
        for position, line in lines_from(file.path, offset):
            reading.offset = position
            payload = line.get("payload")
            at = line.get("timestamp")
            if not isinstance(payload, dict) or not isinstance(at, str):
                continue
            kind = line.get("type")
            if kind == "session_meta":
                reading.directory = _text(payload.get("cwd"))
            elif kind == "turn_context":
                model = _text(payload.get("model")) or model
            elif kind == "token_usage_record":
                records = True
                reading.events.append(Usage(at, model, _counts(payload.get("usage"))))
            elif payload.get("type") == "token_count" and not records:
                info = payload.get("info")
                if isinstance(info, dict) and info.get("last_token_usage"):
                    reading.events.append(
                        Usage(at, model, _counts(info["last_token_usage"]))
                    )
            else:
                reading.events.extend(_shell_calls(payload, at))
        reading.carry = {"model": model, "records": records}
        return reading

    def sessions_in(self, roots: tuple[Path, ...], directory: Path) -> tuple[str, ...]:
        found = []
        for root in roots:
            for base in (root / "sessions", root):
                for path in sorted(base.glob("*/*/*/rollout-*.jsonl")):
                    header = first_line(path)
                    payload = header.get("payload") if header else None
                    if (
                        isinstance(payload, dict)
                        and header is not None
                        and header.get("type") == "session_meta"
                        and not _thread_spawn(payload)
                        and within(_text(payload.get("cwd")), directory)
                        and isinstance(payload.get("id"), str)
                    ):
                        found.append(payload["id"])
        return tuple(dict.fromkeys(found))


_THREAD = re.compile(r"[A-Za-z0-9][\w-]*")
# The tools through which Codex runs a shell command; another tool's input,
# such as a subagent's prompt, may quote a ww command it never ran.
_SHELL_TOOLS = frozenset({"exec", "exec_command", "shell", "shell_command"})


def _thread_spawn(payload: dict[str, Any]) -> dict[str, Any] | None:
    source = payload.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    return spawn if isinstance(spawn, dict) else None


def _counts(usage: object) -> TokenCounts:
    if not isinstance(usage, dict):
        return TokenCounts()
    cached = count(usage.get("cached_input_tokens"))
    written = count(usage.get("cache_write_input_tokens"))
    return TokenCounts(
        input=max(0, count(usage.get("input_tokens")) - cached - written),
        cache_write=written,
        cache_read=cached,
        output=count(usage.get("output_tokens")),
        reasoning=count(usage.get("reasoning_output_tokens")),
    )


def _shell_calls(payload: dict[str, Any], at: str) -> list[Call]:
    kind = payload.get("type")
    if kind == "function_call" and payload.get("name") in _SHELL_TOOLS:
        text = payload.get("arguments")
    elif kind == "custom_tool_call" and payload.get("name") in _SHELL_TOOLS:
        text = payload.get("input")
    elif kind == "local_shell_call":
        action = payload.get("action")
        command = action.get("command") if isinstance(action, dict) else None
        text = " ".join(command) if isinstance(command, list) else None
    else:
        return []
    return ww_calls(text, at) if isinstance(text, str) else []


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


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
    session_variable = "CODEX_THREAD_ID"
    usage = CodexUsage()
