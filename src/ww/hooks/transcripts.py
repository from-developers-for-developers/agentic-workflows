# SPDX-License-Identifier: GPL-3.0-or-later
"""Recover a conversation with the operator from an agent's own transcript.

When a session ends while an interactive step is still talking, the
``interrupt`` hook reads the session's transcript file and keeps only what
the two sides said to each other: the operator's typed messages and the
agent's text replies.  Tool calls and their results, reasoning, injected
context and subagent lines are left out.

The transcript formats are the agents' internal ones, not a published
contract, so every reader is best effort: a line it does not understand is
skipped, and an unreadable file recovers nothing.  Only the end of the file
is read, since the conversation of the current step is at its end.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

# The conversation of the step in progress is recent; reading only the end
# of a long session's file keeps the hook within its few seconds.
READ_LIMIT = 2 * 1024 * 1024
# Text that starts like this was put into the conversation by the agent's
# tooling, not typed by the operator: slash-command echoes, injected
# context, and the agent's own note that a turn was interrupted.
_INJECTED = (
    "<command-",
    "<local-command-",
    "<system-reminder>",
    "<environment_context>",
    "<user_instructions>",
    "[Request interrupted",
)

Entry = tuple[str, str]


def recover_conversation(agent: str, path: Path, since: datetime) -> tuple[Entry, ...]:
    """The ``(speaker, text)`` entries newer than ``since``, in order.

    Speakers are ``operator`` and ``agent``; consecutive messages of one
    side form one entry.  An agent whose format ww does not read, or a file
    it cannot read, recovers nothing.
    """
    reader = _READERS.get(agent)
    if reader is None:
        return ()
    try:
        records = list(_records(path))
    except OSError:
        return ()
    return _merged(reader(records, since))


def _records(path: Path) -> Iterator[dict[str, Any]]:
    """The JSON objects of the file's last ``READ_LIMIT`` bytes, one per line."""
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - READ_LIMIT))
        if size > READ_LIMIT:
            handle.readline()  # the first line read is cut short
        for raw in handle:
            try:
                value = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def _claude_code(records: list[dict[str, Any]], since: datetime) -> Iterator[Entry]:
    """Claude Code's session JSONL.

    A kept line looks like ``{"type": "user", "timestamp":
    "2026-10-01T09:00:00.000Z", "message": {"role": "user", "content":
    "Use a queue."}}``; an assistant line holds a list of blocks, of which
    only ``{"type": "text", "text": ...}`` is kept, never ``tool_use`` or
    ``thinking``.  Subagent (``isSidechain``) and injected (``isMeta``)
    lines are skipped, and so is a user line carrying a ``tool_result``.
    """
    for record in records:
        kind = record.get("type")
        if kind not in ("user", "assistant"):
            continue
        if record.get("isSidechain") is True or record.get("isMeta") is True:
            continue
        if not _newer(record.get("timestamp"), since):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        text = _text_of(message.get("content"), ("text",))
        if text:
            yield ("operator" if kind == "user" else "agent", text)


def _codex(records: list[dict[str, Any]], since: datetime) -> Iterator[Entry]:
    """A Codex rollout JSONL.

    The conversation is read from event lines such as ``{"timestamp":
    "2026-10-01T09:00:00.000Z", "type": "event_msg", "payload": {"type":
    "user_message", "message": "Use a queue."}}`` and ``agent_message``.
    A rollout without them falls back to ``response_item`` lines of payload
    type ``message``, whose ``role`` is ``user`` or ``assistant`` and whose
    content blocks are ``input_text`` or ``output_text``.
    """
    recent = [record for record in records if _newer(record.get("timestamp"), since)]
    payloads: list[tuple[object, dict[str, Any]]] = [
        (record.get("type"), payload)
        for record in recent
        if isinstance(payload := record.get("payload"), dict)
    ]
    speakers = {"user_message": "operator", "agent_message": "agent"}
    if any(kind == "event_msg" for kind, _ in payloads):
        for kind, payload in payloads:
            speaker = speakers.get(str(payload.get("type")), "")
            message = payload.get("message")
            if kind == "event_msg" and speaker and isinstance(message, str):
                text = _kept(message)
                if text:
                    yield (speaker, text)
        return
    roles = {"user": "operator", "assistant": "agent"}
    for kind, payload in payloads:
        speaker = roles.get(str(payload.get("role")), "")
        if kind != "response_item" or payload.get("type") != "message" or not speaker:
            continue
        text = _text_of(payload.get("content"), ("input_text", "output_text", "text"))
        if text:
            yield (speaker, text)


def _text_of(content: object, kinds: tuple[str, ...]) -> str:
    """The kept text of a message: a plain string, or its text blocks joined."""
    if isinstance(content, str):
        return _kept(content)
    if not isinstance(content, list):
        return ""
    parts = (
        _kept(block["text"])
        for block in content
        if isinstance(block, dict)
        and block.get("type") in kinds
        and isinstance(block.get("text"), str)
    )
    return "\n\n".join(part for part in parts if part)


def _kept(text: str) -> str:
    """``text`` stripped, or empty when the agent's tooling put it there."""
    text = text.strip()
    return "" if text.startswith(_INJECTED) else text


def _newer(value: object, since: datetime) -> bool:
    if not isinstance(value, str):
        return False
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return moment.tzinfo is not None and moment > since


def _merged(entries: Iterator[Entry]) -> tuple[Entry, ...]:
    """Consecutive messages of one side as one entry, as a reader sees a turn."""
    result: list[Entry] = []
    for speaker, text in entries:
        if result and result[-1][0] == speaker:
            result[-1] = (speaker, result[-1][1] + "\n\n" + text)
        else:
            result.append((speaker, text))
    return tuple(result)


_READERS: dict[str, Callable[[list[dict[str, Any]], datetime], Iterator[Entry]]] = {
    "claudecode": _claude_code,
    "codex": _codex,
}
