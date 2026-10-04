# SPDX-License-Identifier: GPL-3.0-or-later
"""In-memory task persistence for tests and embedded callers."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone

from ww.amendments import Amendment
from ww.errors import StateError
from ww.execution_models import TaskRunAggregate, validate_task_runs
from ww.items import WorkItem
from ww.storage_adapters.base import (
    ArtifactAddress,
    CommandOutputAddress,
    TaskMetadata,
    TaskStorageAdapter,
)


class MemoryTaskStorageAdapter(TaskStorageAdapter):
    """Store task aggregates, metadata, and artifacts in dictionaries.

    This storage adapter has no filesystem side effects. Artifact keys use stable
    ``memory://`` references, mirroring the run-aware addressing contract of
    the filesystem storage adapter.
    """

    def __init__(self) -> None:
        self.artifacts: dict[str, str] = {}
        self._artifact_owners: dict[str, str] = {}
        self.metadata: dict[str, TaskMetadata] = {}
        self.shared_items: dict[str, tuple[WorkItem, ...]] = {}
        self.amendments: dict[str, tuple[Amendment, ...]] = {}
        self.aggregates: dict[str, tuple[tuple[TaskRunAggregate, ...], str | None]] = {}
        self.aggregate_revisions: dict[str, int] = {}
        # When each task's runs were last committed.
        self.written_at: dict[str, datetime] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def lock_task(self, task_id: str) -> Iterator[None]:
        """Match the filesystem storage adapter's whole-command task lock in memory."""
        # A parent reset must exclude concurrent writes to any child task.
        task_ids = tuple(
            part
            for part in (task_id.rsplit("/", 1)[0] if "/" in task_id else None, task_id)
            if part is not None
        )
        with self._locks_guard:
            locks = tuple(
                self._locks.setdefault(value, threading.RLock()) for value in task_ids
            )
        with ExitStack() as stack:
            for lock in locks:
                stack.enter_context(lock)
            yield

    def read_task_record(
        self, task_id: str
    ) -> tuple[tuple[TaskRunAggregate, ...], str | None, int]:
        aggregate = self.aggregates.get(task_id)
        if aggregate is None:
            return (), None, 0
        runs, handoff = aggregate
        try:
            validate_task_runs(task_id, runs)
        except (StateError, ValueError) as error:
            # The same error as the filesystem adapter's, so callers that
            # tolerate one unreadable task treat both adapters alike.
            raise StateError(f"invalid task state {task_id}: {error}") from error
        return runs, handoff, self.aggregate_revisions.get(task_id, 0)

    def commit_task_aggregate(
        self,
        task_id: str,
        runs: tuple[TaskRunAggregate, ...],
        handoff: str | None = None,
        expected_revision: int | None = None,
    ) -> int:
        revision = self.task_aggregate_revision(task_id)
        if revision and expected_revision is None:
            raise StateError("existing task aggregate requires expected_revision")
        if expected_revision is not None and revision != expected_revision:
            raise StateError(
                "task aggregate revision conflict: "
                f"expected {expected_revision}, got {revision}"
            )
        validate_task_runs(task_id, runs)
        # Match the filesystem boundary: snapshots are encoded and decoded at
        # commit time, retaining plain action payloads for later inspection if
        # a registration disappears from the process.
        try:
            persisted_runs = tuple(
                TaskRunAggregate.from_dict(run.to_dict()) for run in runs
            )
        except ValueError as error:
            raise StateError(
                f"task {task_id!r} aggregate cannot be encoded: {error}"
            ) from error
        self.aggregates[task_id] = (persisted_runs, handoff)
        self.aggregate_revisions[task_id] = revision + 1
        self.written_at[task_id] = datetime.now(timezone.utc)
        self.metadata.setdefault(task_id, TaskMetadata(task_id))
        return revision + 1

    def task_written_at(self, task_id: str) -> datetime | None:
        return self.written_at.get(task_id)

    def task_ids(self) -> tuple[str, ...]:
        owners = {*self.aggregates, *self.metadata, *self._artifact_owners.values()}
        return tuple(sorted({owner.split("/")[0] for owner in owners}))

    def task_exists(self, task_id: str) -> bool:
        # Stored artifacts also claim the ID; see the port docstring.
        return super().task_exists(task_id) or task_id in self._artifact_owners.values()

    def read_task_metadata(self, task_id: str) -> TaskMetadata | None:
        return self.metadata.get(task_id)

    def write_task_metadata(self, metadata: TaskMetadata) -> None:
        self.metadata[metadata.task_id] = metadata

    def read_shared_items(self, task_id: str) -> tuple[WorkItem, ...]:
        return self.shared_items.get(task_id, ())

    def write_shared_items(self, task_id: str, items: tuple[WorkItem, ...]) -> None:
        self.shared_items[task_id] = items

    def read_amendments(self, task_id: str) -> tuple[Amendment, ...]:
        return self.amendments.get(task_id, ())

    def append_amendment(self, task_id: str, amendment: Amendment) -> None:
        self.amendments[task_id] = (*self.read_amendments(task_id), amendment)

    def remove_task(self, task_id: str) -> bool:
        existed = bool(
            task_id in self.aggregates
            or task_id in self.metadata
            or task_id in self._artifact_owners.values()
        )
        self.metadata.pop(task_id, None)
        self.shared_items.pop(task_id, None)
        self.amendments.pop(task_id, None)
        self.aggregates.pop(task_id, None)
        self.aggregate_revisions.pop(task_id, None)
        self.written_at.pop(task_id, None)
        for reference, owner in tuple(self._artifact_owners.items()):
            if owner == task_id:
                del self.artifacts[reference]
                del self._artifact_owners[reference]
        return existed

    def child_task_ids(self, task_id: str) -> tuple[str, ...]:
        prefix = f"{task_id}/"
        task_ids = set(self.aggregates) | set(self.metadata)
        task_ids.update(self._artifact_owners.values())
        return tuple(
            sorted(candidate for candidate in task_ids if candidate.startswith(prefix))
        )

    def write_execution_artifact(self, address: ArtifactAddress, content: str) -> str:
        reference = self._reference(
            address.task_id, address.run_namespace, "steps", *address.segments()
        )
        self.artifacts[reference] = content
        self._artifact_owners[reference] = address.task_id
        return reference

    def read_execution_artifact(self, reference: str) -> str:
        try:
            return self.artifacts[reference]
        except KeyError as error:
            raise StateError(f"missing execution artifact: {reference}") from error

    def write_command_output(self, address: CommandOutputAddress, content: str) -> str:
        reference = self._reference(
            address.task_id, address.run_id, "command-output", *address.segments()
        )
        self.artifacts[reference] = content
        self._artifact_owners[reference] = address.task_id
        return reference

    @staticmethod
    def _reference(task_id: str, run_id: str, *segments: str) -> str:
        return f"memory://{task_id}/runs/{run_id}/" + "/".join(segments)

    def read_command_output(self, reference: str) -> str:
        try:
            return self.artifacts[reference]
        except KeyError as error:
            raise StateError(f"missing command output: {reference}") from error
