# SPDX-License-Identifier: GPL-3.0-or-later
"""What ww answers when an agent calls one of its hooks.

The hooks are gentle by design: they add one line of context, and nothing is
ever blocked.

- ``session-start`` prints one line saying that ww coordinates work here and
  how to list its workflows (or that ww is used only on request).
- ``stop`` and ``interrupt`` are still accepted, so a hook file written by an
  older ww keeps working, and answer with nothing.

An adapter reads the agent's payload and renders the answer; everything else
is decided here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ww.config_files import WORKFLOWS_FILE
from ww.storage import Storage

from .agents import HookAgent, HookEvent
from .notices import session_context


@dataclass(frozen=True)
class HookAnswer:
    """What the hook prints, and a word for the audit log about why."""

    text: str
    decision: str


ALLOW = HookAnswer("", "allowed")


def answer_hook(
    storage: Storage,
    agent: HookAgent,
    event: HookEvent,
    raw_payload: str,
    *,
    on_request: bool = False,
) -> HookAnswer:
    """ww's answer to one hook call; the caller contains every error.

    ``on_request`` is the project's ``"enabled": "on_request"``: the session
    is told that ww is used only when the user asks for it.
    """
    payload = agent.parse(event, _payload(raw_payload))
    if event != "session-start":
        return HookAnswer("", "nothing to answer: stop and interrupt are retired")
    if not payload.wants_context:
        return HookAnswer("", "no context needed")
    text = session_context(compacted=payload.source == "compact", on_request=on_request)
    return HookAnswer(agent.context_reply(text.rstrip("\n")) + "\n", "context")


def _payload(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def is_project_root(root: Path) -> bool:
    """Only a directory holding the repo workflow file is a ww root."""
    return (root / WORKFLOWS_FILE).is_file()
