# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent-generalised feedback candidates; no candidate is an installed rule."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from ww.errors import StateError
from ww.storage import Storage
from ww.task_ids import validate_task_id

STORE_SCHEMA = 1
# Rule review may prune after five subsequently completed unmatched tasks.
STALE_TASKS = 5


class FeedbackStore:
    """Project-scoped, locked storage shared by tasks and their worktrees."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.path = storage.runtime_path / "feedback.json"

    def _lock(self) -> AbstractContextManager[None]:
        return self.storage.locks.lock(self.path, purpose="operator feedback")

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema": STORE_SCHEMA, "tasks": [], "completed": [], "points": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != STORE_SCHEMA:
                raise ValueError("unsupported feedback store schema")
            for key in ("tasks", "completed"):
                if not isinstance(data.get(key), list) or any(
                    not isinstance(task, str) for task in data[key]
                ):
                    raise ValueError(f"{key} must be a list of task IDs")
            if not isinstance(data.get("points"), dict):
                raise ValueError("points must be an object")
            for point in data["points"].values():
                if not isinstance(point, dict):
                    raise ValueError("point must be an object")
                _point_fields(point)
                if not isinstance(point.get("events"), list):
                    raise ValueError("events must be a list")
                if type(point.get("last_seen")) is not int:
                    raise ValueError("last_seen must be an integer")
                for event in point["events"]:
                    if not isinstance(event, dict):
                        raise ValueError("event must be an object")
                    _text(event, "task")
                    _text(event, "quote")
                    if "entry" in event:
                        if type(event["entry"]) is not int:
                            raise ValueError("event entry must be an integer")
                    else:
                        _text(event, "source")
                    _timestamp(event.get("at"))
                point["last_encountered_at"] = _last_encountered(point["events"])
            return dict(data)
        except (OSError, ValueError, StateError) as error:
            raise StateError(f"invalid feedback store {self.path}: {error}") from error

    def _save(self, data: dict[str, Any]) -> None:
        self.storage.locks.atomic_write(self.path, json.dumps(data, indent=2) + "\n")

    def get(self, identifier: str) -> dict[str, object]:
        points = self.listing()["points"]
        assert isinstance(points, list)
        for point in points:
            if point["id"] == identifier:
                return dict(point)
        raise StateError(f"unknown feedback point {identifier!r}")

    def listing(self) -> dict[str, object]:
        data = self._load()
        total = len(data["tasks"])
        completed = set(data["completed"])
        points = []
        for identifier, point in data["points"].items():
            task_ids = {event["task"] for event in point["events"]}
            points.append(
                {
                    "id": identifier,
                    **point,
                    "occurrences": len(point["events"]),
                    "task_occurrences": len(task_ids),
                    "task_ratio": len(task_ids) / total if total else 0,
                    "occurrence_ratio": len(point["events"]) / total if total else 0,
                    "completed_task_ratio": (
                        len(task_ids & completed) / len(completed) if completed else 0
                    ),
                    "tasks_since_last_seen": len(data["completed"])
                    - point["last_seen"],
                }
            )
        return {
            "total_tasks": total,
            "completed_tasks": len(completed),
            "retire_after_tasks": STALE_TASKS,
            "points": points,
        }

    def record(
        self,
        task_id: str,
        run_id: str | None,
        analysis: object,
        sources: object,
    ) -> dict[str, object]:
        """Record deductions with quoted artifact evidence and explicit match IDs."""
        validate_task_id(task_id)
        if not isinstance(analysis, list):
            raise StateError("feedback analysis must be a JSON array")
        if not isinstance(sources, list):
            raise StateError("feedback sources must be a list")
        by_id = {source["id"]: source for source in sources}
        prepared = []
        for raw in analysis:
            if not isinstance(raw, dict):
                raise StateError("each feedback point must be an object")
            unknown = set(raw) - {
                "id",
                "summary",
                "reason",
                "enforcement",
                "approach",
                "evidence",
            }
            if unknown:
                raise StateError(
                    "unknown feedback fields: " + ", ".join(sorted(unknown))
                )
            fields = _point_fields(raw)
            identifier = raw.get("id")
            if identifier is not None and (
                not isinstance(identifier, str) or not identifier.strip()
            ):
                raise StateError("feedback id must be a non-empty string")
            evidence = raw.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                raise StateError("feedback evidence must be a non-empty list")
            events = []
            for supplied in evidence:
                if not isinstance(supplied, dict) or set(supplied) != {
                    "source",
                    "quote",
                }:
                    raise StateError("feedback evidence needs source and quote")
                source_id = _text(supplied, "source")
                source = by_id.get(source_id)
                if source is None:
                    raise StateError(
                        "feedback source is not a completed learnable artifact"
                    )
                quote = _text(supplied, "quote")
                if quote not in source["content"] and quote not in (
                    source.get("adjustments") or ""
                ):
                    raise StateError("feedback quote does not occur in its artifact")
                events.append(
                    {
                        "task": task_id,
                        "run_id": run_id,
                        "source": source_id,
                        "artifact": source["artifact"],
                        "step": source["step"],
                        "quote": quote,
                        "at": source["encountered_at"],
                    }
                )
            prepared.append((identifier, fields, events))
        with self._lock():
            data = self._load()
            if task_id not in data["tasks"]:
                data["tasks"].append(task_id)
            if task_id not in data["completed"]:
                data["completed"].append(task_id)
            for identifier, fields, events in prepared:
                if identifier is not None and identifier not in data["points"]:
                    raise StateError(
                        f"unknown feedback point {identifier!r}; list first"
                    )
                if identifier is None:
                    existing = next(
                        (
                            key
                            for key, point in data["points"].items()
                            if point["summary"].casefold()
                            == fields["summary"].casefold()
                        ),
                        None,
                    )
                    if existing is not None:
                        seen = {
                            _event_key(event)
                            for event in data["points"][existing]["events"]
                        }
                        if any(_event_key(event) not in seen for event in events):
                            raise StateError(
                                f"existing feedback point {existing!r}; "
                                "pass its id to update"
                            )
                        identifier = existing
                    else:
                        identifier = "feedback-" + uuid.uuid4().hex[:12]
                point = data["points"].setdefault(identifier, {"events": []})
                seen = {_event_key(event) for event in point["events"]}
                for event in events:
                    key = _event_key(event)
                    if key not in seen:
                        point["events"].append(event)
                        seen.add(key)
                point.update(fields)
                point["last_seen"] = max(
                    (
                        data["completed"].index(event["task"]) + 1
                        for event in point["events"]
                        if event["task"] in data["completed"]
                    ),
                    default=0,
                )
                point["last_encountered_at"] = _last_encountered(point["events"])
            self._save(data)
        return self.listing()

    def complete_task(self, task_id: str) -> None:
        """Count completed task exposure once; never deduce or delete candidates."""
        with self._lock():
            data = self._load()
            if task_id in data["completed"]:
                return
            if task_id not in data["tasks"]:
                data["tasks"].append(task_id)
            data["completed"].append(task_id)
            for point in data["points"].values():
                if any(event["task"] == task_id for event in point["events"]):
                    point["last_seen"] = len(data["completed"])
            self._save(data)

    def prune(
        self,
        *,
        dry_run: bool = False,
        keep: tuple[str, ...] = (),
    ) -> dict[str, object]:
        """Explicit maintenance called by rule review, never by workflow completion."""
        with self._lock():
            data = self._load()
            unknown = set(keep) - set(data["points"])
            if unknown:
                raise StateError(
                    "unknown feedback point(s): " + ", ".join(sorted(unknown))
                )
            stale = []
            for identifier, point in data["points"].items():
                pending = {event["task"] for event in point["events"]} - set(
                    data["completed"]
                )
                if (
                    identifier not in keep
                    and not pending
                    and len(data["completed"]) - point["last_seen"] >= STALE_TASKS
                ):
                    stale.append(identifier)
            if not dry_run and stale:
                for identifier in stale:
                    del data["points"][identifier]
                self._save(data)
            return {"dry_run": dry_run, "pruned": stale, "kept": list(keep)}


def _event_key(event: dict[str, Any]) -> tuple[object, ...]:
    # Legacy transcript points remain readable without losing IDs or counts.
    source = event.get("source", f"interaction:{event.get('entry')}")
    return event["task"], event.get("run_id"), source, event["quote"]


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise StateError("feedback encounter time must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone is required")
        return parsed.astimezone(timezone.utc)
    except ValueError as error:
        raise StateError(f"invalid feedback encounter time: {value!r}") from error


def _last_encountered(events: list[dict[str, Any]]) -> str | None:
    return (
        max(events, key=lambda event: _timestamp(event["at"]))["at"] if events else None
    )


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StateError(f"feedback {key} must be a non-empty string")
    return value.strip()


def _point_fields(raw: dict[str, Any]) -> dict[str, str]:
    fields = {
        key: _text(raw, key) for key in ("summary", "reason", "enforcement", "approach")
    }
    if fields["enforcement"] not in {"scripted", "reasoning"}:
        raise StateError("feedback enforcement must be scripted or reasoning")
    return fields


class MemoryFeedbackStore(FeedbackStore):
    """Injectable candidate storage for embedded services without disk writes."""

    def __init__(self, storage: Storage) -> None:
        super().__init__(storage)
        self.data: dict[str, Any] = {
            "schema": STORE_SCHEMA,
            "tasks": [],
            "completed": [],
            "points": {},
        }
        self.guard = threading.RLock()

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self.guard:
            yield

    def _load(self) -> dict[str, Any]:
        with self.guard:
            return deepcopy(self.data)

    def _save(self, data: dict[str, Any]) -> None:
        self.data = deepcopy(data)
