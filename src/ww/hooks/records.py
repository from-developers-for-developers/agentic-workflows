# SPDX-License-Identifier: GPL-3.0-or-later
"""The two small per-task records agent hooks keep beside the interaction log.

``interrupted.json`` marks a task whose agent session ended, or was
interrupted, while one of its agent-owned steps was in progress.  It holds
one record, overwritten by the next interruption, and is cleared once that
step's attempt completes or fails, never merely because a notice showed it:
a compaction or a new session between showing and acting must not lose it.

``stop-reminders.json`` lists the step attempts ww already reminded an agent
about when it stopped, so the reminder is given once and the next stop is
always allowed.

Both belong to the task and are forgotten when the task is reset.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ww.errors import StateError
from ww.storage import Storage
from ww.storage_adapters import TaskStorageAdapter

INTERRUPTED_FILE = "interrupted.json"
REMINDERS_FILE = "stop-reminders.json"


@dataclass(frozen=True)
class Interruption:
    """What a session was doing when it stopped short."""

    at: str
    run_id: str | None
    step: str | None
    item_id: str
    # ``step`` is how messages name the work, a hook as "<name> (a hook of
    # <step>)"; ``item_name`` is the plan item's own name.
    item_name: str | None
    attempt: int
    agent: str
    # The agent's own word for why the session ended, when it gives one.
    reason: str | None = None
    # The step was talking with the operator when the session ended.
    in_conversation: bool = False

    @property
    def moment(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.at.replace("Z", "+00:00"))
        except ValueError:
            return None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class HookRecords:
    """Read and write the hook records of the tasks in one project root."""

    def __init__(self, storage: Storage, tasks: TaskStorageAdapter) -> None:
        self.storage = storage
        self.tasks = tasks

    def _directory(self, task_id: str) -> Path:
        return self.storage.runtime_path / "tasks" / task_id

    # Interruptions

    def mark_interrupted(self, task_id: str, record: Interruption) -> None:
        path = self._directory(task_id) / INTERRUPTED_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        self.storage.locks.atomic_write(
            path, json.dumps(record.to_dict(), indent=2) + "\n"
        )

    def interruption(self, task_id: str) -> Interruption | None:
        """The task's interruption, or ``None`` once its attempt has ended.

        A record whose attempt completed or failed, or was superseded by a
        later attempt, is removed here: this is the one place that decides
        whether the notice still applies.
        """
        path = self._directory(task_id) / INTERRUPTED_FILE
        record = _read_interruption(path)
        if record is None:
            return None
        if self._attempt_ended(task_id, record):
            path.unlink(missing_ok=True)
            return None
        return record

    def interruptions(self) -> tuple[tuple[str, Interruption], ...]:
        """Every task still marked as interrupted, newest first."""
        root = self.storage.runtime_path / "tasks"
        if not root.is_dir():
            return ()
        found = []
        for path in root.rglob(INTERRUPTED_FILE):
            task_id = path.parent.relative_to(root).as_posix()
            try:
                record = self.interruption(task_id)
            except StateError:
                # open_work() reports the unreadable task; skip it here.
                continue
            if record is not None:
                found.append((task_id, record))
        return tuple(sorted(found, key=lambda entry: entry[1].at, reverse=True))

    def recent(self, days: int) -> tuple[tuple[str, Interruption], ...]:
        """The interruptions of the last ``days`` days, newest first."""
        horizon = datetime.now(timezone.utc) - timedelta(days=days)
        return tuple(
            (task_id, record)
            for task_id, record in self.interruptions()
            if record.moment is not None and record.moment >= horizon
        )

    def _attempt_ended(self, task_id: str, record: Interruption) -> bool:
        runs, _, _ = self.tasks.read_task_record(task_id)
        run = next((run for run in runs if run.run_id == record.run_id), None)
        if run is None:
            return False
        execution = next(
            (
                entry
                for entry in run.state.item_executions
                if entry.plan_item_id == record.item_id
            ),
            None,
        )
        if execution is None:
            return False
        return execution.status in {"completed", "failed"} or (
            execution.attempts > record.attempt
        )

    # Stop reminders

    def claim_reminder(self, task_id: str, key: str) -> bool:
        """Record the reminder for ``key``; ``False`` when it was already given.

        Agents may run the same hook twice at once (Claude Code runs matching
        hooks in parallel), so checking and recording happen under one lock of
        their own: exactly one call reminds. It is not the task lock, which a
        long-running command may hold for minutes.
        """
        path = self._directory(task_id) / REMINDERS_FILE
        with self.storage.locks.lock(path, purpose="stop reminders"):
            keys = self._reminders(task_id)
            if key in keys:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            self.storage.locks.atomic_write(
                path, json.dumps([*keys, key], indent=2) + "\n"
            )
        return True

    def _reminders(self, task_id: str) -> list[str]:
        path = self._directory(task_id) / REMINDERS_FILE
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return (
            [entry for entry in value if isinstance(entry, str)]
            if isinstance(value, list)
            else []
        )

    def remove(self, task_id: str) -> None:
        """Forget both records, as part of resetting the task."""
        for name in (INTERRUPTED_FILE, REMINDERS_FILE):
            (self._directory(task_id) / name).unlink(missing_ok=True)


def reminder_key(run_id: str | None, item_id: str, attempt: int) -> str:
    """One step attempt, which ww reminds an agent about at most once."""
    return f"{run_id or '-'}:{item_id}:{attempt}"


def _read_interruption(path: Path) -> Interruption | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    try:
        return Interruption(
            at=str(value["at"]),
            run_id=value.get("run_id"),
            step=value.get("step"),
            item_id=str(value["item_id"]),
            item_name=value.get("item_name"),
            attempt=int(value.get("attempt", 0)),
            agent=str(value.get("agent", "")),
            reason=value.get("reason"),
            in_conversation=bool(value.get("in_conversation", False)),
        )
    except (KeyError, TypeError, ValueError):
        return None
