# SPDX-License-Identifier: GPL-3.0-or-later
"""What ww answers when an agent calls one of its hooks.

The hooks are gentle by design: they add a few lines of context or remind
once, and nothing is ever blocked.

- ``session-start`` prints a reminder that ww coordinates work here, and the
  recently updated unfinished tasks with the commands that resume them,
  unless ``agent_hooks.check_unfinished`` switches that scan off.
- ``stop`` asks the agent, once per step attempt, to record an agent-owned
  step that is still in progress; the next stop is always allowed.
- ``interrupt`` records, without answering, that a session ended while such
  a step was in progress, so the next session is told to check the work.
  When the step was talking with the operator, it first recovers what the
  two sides said from the session's transcript into the interaction record.

An adapter reads the agent's payload and renders the answer; everything else
is decided here, from persisted state alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ww.config_files import WORKFLOWS_FILE
from ww.interactions import RECOVERED, InteractionLog
from ww.open_work import OpenTask, open_work, tasks_for_session
from ww.project_config import AgentHooks
from ww.storage import Storage

from .agents import HookAgent, HookEvent, HookPayload
from .notices import session_context, stop_reminder
from .records import HookRecords, Interruption, reminder_key
from .transcripts import recover_conversation


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
    settings: AgentHooks,
    on_request: bool = False,
) -> HookAnswer:
    """ww's answer to one hook call; the caller contains every error.

    ``on_request`` is the project's ``"enabled": "on_request"``: the session
    is told that ww is used only when the user asks for it. Stop reminders
    are unchanged, since they concern tasks already open. ``settings`` is
    the project's ``agent_hooks``, which only ``session-start`` reads.
    """
    payload = agent.parse(event, _payload(raw_payload))
    records = HookRecords(storage, storage.task_persistence)
    if event == "session-start":
        return _session_start(
            storage, agent, payload, records, settings, on_request=on_request
        )
    # An unreadable task has no step anyone can close, so stop and interrupt
    # consider only the tasks that could be read.
    tasks = open_work(storage.task_persistence, storage.root).tasks
    if event == "stop" and not payload.interrupted:
        return _stop(agent, payload, records, tasks)
    return _interrupt(agent, payload, records, tasks)


def _session_start(
    storage: Storage,
    agent: HookAgent,
    payload: HookPayload,
    records: HookRecords,
    settings: AgentHooks,
    *,
    on_request: bool,
) -> HookAnswer:
    if not payload.wants_context:
        return HookAnswer("", "no context needed")
    compacted = payload.source == "compact"
    if not settings.check_unfinished:
        text = session_context(
            (), {}, storage.root, compacted=compacted, on_request=on_request
        )
        return HookAnswer(
            agent.context_reply(text.rstrip("\n")) + "\n",
            "context without the unfinished-task scan",
        )
    horizon = datetime.now(timezone.utc) - timedelta(days=settings.recent_days)
    work = open_work(storage.task_persistence, storage.root, since=horizon)
    interruptions = {
        task.task_id: record
        for task in work.tasks
        if (record := records.interruption(task.task_id)) is not None
    }
    text = session_context(
        work.tasks,
        interruptions,
        storage.root,
        compacted=compacted,
        unreadable=work.unreadable,
        on_request=on_request,
        skipped=work.skipped,
    )
    return HookAnswer(
        agent.context_reply(text.rstrip("\n")) + "\n",
        f"context with {len(work.tasks)} unfinished task(s)",
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
    # stop skips delegated steps, and the worker's stop reminds instead. A
    # step in conversation with the operator stops to hear them.
    if not payload.from_worker and any(
        task.waiting_on_another(tasks)
        for task in tasks_for_session(
            tasks, records.storage.root, payload.directory, agent.name
        )
    ):
        return replace(
            ALLOW, decision="allowed: the manager is waiting on a child or worker"
        )
    working = tasks_for_session(
        tuple(
            task
            for task in tasks
            if task.agent_step_in_progress
            and not task.in_conversation
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
        recovered = (
            _recover(agent, payload, records.storage, task, at)
            if task.in_conversation
            else 0
        )
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
                in_conversation=task.in_conversation,
                recovered_entries=recovered,
            ),
        )
    return HookAnswer(
        "", "marked interrupted: " + ", ".join(task.task_id for task in working)
    )


def _recover(
    agent: HookAgent,
    payload: HookPayload,
    storage: Storage,
    task: OpenTask,
    at: str,
) -> int:
    """Append the conversation the session's transcript holds; its size.

    Only what was said since the step's attempt started is taken.  The
    task lock is not taken: the session that held the conversation is the
    one ending, and the hook has seconds, not minutes.  Any failure
    recovers nothing, so the hook never fails over it.
    """
    if payload.transcript_path is None:
        return 0
    try:
        since = datetime.fromisoformat(
            (task.started_at or task.updated_at).replace("Z", "+00:00")
        )
        entries = list(recover_conversation(agent.name, payload.transcript_path, since))
        if entries and entries[-1][0] != "agent" and payload.last_agent_message:
            entries.append(("agent", payload.last_agent_message.strip()))
        InteractionLog(storage).append_entries(
            task.task_id,
            [(speaker + RECOVERED, text) for speaker, text in entries],
            run_id=task.run_id,
            step=task.item_name or task.label,
            item_id=task.work_item_id,
            at=at,
        )
    except Exception:  # noqa: BLE001 - a hook must never fail over recovery
        return 0
    return len(entries)


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
