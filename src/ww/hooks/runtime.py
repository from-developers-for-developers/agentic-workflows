# SPDX-License-Identifier: GPL-3.0-or-later
"""What ww answers when an agent calls one of its hooks.

The hooks are gentle by design: they add a few lines of context or remind
once, and nothing is ever blocked.

- ``session-start`` prints a reminder that ww coordinates work here, and the
  unfinished tasks with the commands that resume them.
- ``stop`` asks the agent, once per step attempt, to record an agent-owned
  step that is still in progress; the next stop is always allowed.
- ``interrupt`` records, without answering, that a session ended while such
  a step was in progress, so the next session is told to check the work.

An adapter reads the agent's payload and renders the answer; everything else
is decided here, from persisted state alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ww.config_files import WORKFLOWS_FILE
from ww.open_work import OpenTask, OpenWork, open_work, tasks_for_session
from ww.storage import Storage

from .agents import HookAgent, HookEvent, HookPayload
from .notices import session_context, stop_reminder
from .records import HookRecords, Interruption, reminder_key


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
    is told that ww is used only when the user asks for it. Stop reminders
    are unchanged, since they concern tasks already open.
    """
    payload = agent.parse(event, _payload(raw_payload))
    records = HookRecords(storage, storage.task_persistence)
    work = open_work(storage.task_persistence, storage.root)
    if event == "session-start":
        return _session_start(
            storage, agent, payload, records, work, on_request=on_request
        )
    # An unreadable task has no step anyone can close, so stop and interrupt
    # consider only the tasks that could be read.
    if event == "stop" and not payload.interrupted:
        return _stop(agent, payload, records, work.tasks)
    return _interrupt(agent, payload, records, work.tasks)


def _session_start(
    storage: Storage,
    agent: HookAgent,
    payload: HookPayload,
    records: HookRecords,
    work: OpenWork,
    *,
    on_request: bool,
) -> HookAnswer:
    if not payload.wants_context:
        return HookAnswer("", "no context needed")
    tasks = work.tasks
    interruptions = {
        task.task_id: record
        for task in tasks
        if (record := records.interruption(task.task_id)) is not None
    }
    text = session_context(
        tasks,
        interruptions,
        storage.root,
        compacted=payload.source == "compact",
        unreadable=work.unreadable,
        on_request=on_request,
    )
    return HookAnswer(
        agent.context_reply(text.rstrip("\n")) + "\n",
        f"context with {len(tasks)} unfinished task(s)",
    )


def _stop(
    agent: HookAgent,
    payload: HookPayload,
    records: HookRecords,
    tasks: tuple[OpenTask, ...],
) -> HookAnswer:
    if payload.continued:
        return replace(ALLOW, decision="allowed: the agent already continued once")
    # A manager waiting on a worker is not the one to close the step: its own
    # stop skips delegated steps, and the worker's stop reminds instead.
    working = tasks_for_session(
        tuple(
            task
            for task in tasks
            if task.agent_step_in_progress
            and (payload.from_worker or not task.delegated)
        ),
        records.storage.root,
        payload.directory,
        agent.name,
    )
    pending = tuple(
        task
        for task in working
        if task.item_id is not None and records.claim_reminder(task.task_id, _key(task))
    )
    if not pending:
        return ALLOW
    return HookAnswer(
        agent.continue_reply(stop_reminder(pending)) + "\n",
        "reminded: " + ", ".join(task.task_id for task in pending),
    )


def _interrupt(
    agent: HookAgent,
    payload: HookPayload,
    records: HookRecords,
    tasks: tuple[OpenTask, ...],
) -> HookAnswer:
    working = tasks_for_session(
        tuple(task for task in tasks if task.agent_step_in_progress),
        records.storage.root,
        payload.directory,
        agent.name,
    )
    if not working:
        return HookAnswer("", "no step in progress to mark")
    at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    for task in working:
        assert task.item_id is not None
        records.mark_interrupted(
            task.task_id,
            Interruption(
                at=at,
                run_id=task.run_id,
                step=task.label,
                item_id=task.item_id,
                item_name=task.item_name,
                attempt=task.attempt,
                agent=agent.name,
                reason=payload.reason,
            ),
        )
    return HookAnswer(
        "", "marked interrupted: " + ", ".join(task.task_id for task in working)
    )


def _key(task: OpenTask) -> str:
    assert task.item_id is not None
    return reminder_key(task.run_id, task.item_id, task.attempt)


def _payload(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def is_project_root(root: Path) -> bool:
    """Only a directory holding the repo workflow file is a ww root."""
    return (root / WORKFLOWS_FILE).is_file()
