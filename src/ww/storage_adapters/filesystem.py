# SPDX-License-Identifier: GPL-3.0-or-later
"""Filesystem implementation of task persistence."""

from __future__ import annotations

import contextlib
import json
import shutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ww.contracts import BOOTSTRAP_REQUEST_PREFIX
from ww.errors import StateError
from ww.execution_models import TaskRunAggregate, validate_task_runs
from ww.items import WorkItem
from ww.locking import FileLocks
from ww.storage_adapters.base import (
    ArtifactAddress,
    CommandOutputAddress,
    TaskMetadata,
    TaskStorageAdapter,
    flatten_metadata,
)
from ww.storage_adapters.task_document import (
    decode_task_document,
    encode_task_document,
)


class FileTaskStorageAdapter(TaskStorageAdapter):
    """Store task state and artifacts beneath a project's ``.ww/tasks/`` directory.

    Every write goes through :mod:`ww.locking`. Mutating methods do not lock:
    `lock_task` is the boundary and `WorkflowService` holds it around the whole
    command, which is the span that actually needs protecting.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.tasks_path = root / ".ww" / "tasks"
        self.locks = FileLocks(root)

    @contextmanager
    def lock_task(self, task_id: str) -> Iterator[None]:
        """Lock a task and its parent, so parent reset excludes child writes."""
        parent = task_id.rsplit("/", 1)[0] if "/" in task_id else None
        with ExitStack() as stack:
            if parent is not None:
                stack.enter_context(
                    self.locks.lock(
                        self.tasks_path / parent, purpose=f"task {parent!r}"
                    )
                )
            stack.enter_context(
                self.locks.lock(self.tasks_path / task_id, purpose=f"task {task_id!r}")
            )
            yield

    def read_task_record(
        self, task_id: str
    ) -> tuple[tuple[TaskRunAggregate, ...], str | None, int]:
        runs, handoff, revision, _ = self._read_task_document(task_id)
        return runs, handoff, revision

    def _read_task_document(
        self, task_id: str
    ) -> tuple[
        tuple[TaskRunAggregate, ...],
        str | None,
        int,
        dict[str, list[dict[str, object]]],
    ]:
        path = self._state_path(task_id)
        if not path.exists():
            return (), None, 0, {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            decoded = decode_task_document(raw, task_id)
            decoded = (*decoded[:3], self._validate_ledger(decoded[3]))
            validate_task_runs(task_id, decoded[0])
            return decoded
        except (OSError, json.JSONDecodeError, StateError, ValueError) as error:
            raise StateError(f"invalid task state {path}: {error}") from error

    def commit_task_aggregate(
        self,
        task_id: str,
        runs: tuple[TaskRunAggregate, ...],
        handoff: str | None = None,
        expected_revision: int | None = None,
    ) -> int:
        # The service normally already holds the task lock.  This separate
        # aggregate lock also makes direct storage-adapter callers safe: the revision
        # check and replacement cannot race another storage-adapter commit, while the
        # lock ordering remains task -> aggregate everywhere in the service.
        with self.locks.lock(
            self._state_path(task_id), purpose=f"task state {task_id!r}"
        ):
            return self._commit_task_aggregate(
                task_id, runs, handoff, expected_revision
            )

    def _commit_task_aggregate(
        self,
        task_id: str,
        runs: tuple[TaskRunAggregate, ...],
        handoff: str | None = None,
        expected_revision: int | None = None,
    ) -> int:
        previous, _, revision, ledger = self._read_task_document(task_id)
        if revision and expected_revision is None:
            raise StateError("existing task aggregate requires expected_revision")
        if expected_revision is not None and revision != expected_revision:
            raise StateError(
                "task aggregate revision conflict: "
                f"expected {expected_revision}, got {revision}"
            )
        validate_task_runs(task_id, runs)
        previous_ids = {run.run_id for run in previous}
        previous_status = {run.run_id: run.state.status for run in previous}
        for run in runs:
            changed = run.run_id not in previous_ids or (
                previous_status.get(run.run_id) != run.state.status
                and not (
                    previous_status.get(run.run_id) == "in_progress"
                    and run.state.status == "pending"
                )
            )
            if changed:
                if (
                    ledger.get(run.run_id)
                    and ledger[run.run_id][-1].get("status") == run.state.status
                ):
                    continue
                ledger.setdefault(run.run_id, []).append(
                    {
                        "workflow": run.workflow,
                        "status": run.state.status,
                        "summary": dict(run.state.workflow_values).get("summary")
                        if run.state.status == "completed"
                        else None,
                    }
                )
        ledger = self._validate_ledger(ledger)
        try:
            payload = encode_task_document(task_id, runs, handoff, revision + 1, ledger)
            # Validate the exact compact representation before publishing it.
            decoded = decode_task_document(payload, task_id)
        except ValueError as error:
            raise StateError(
                f"task {task_id!r} aggregate cannot be encoded: {error}"
            ) from error
        validate_task_runs(task_id, decoded[0])
        if not self._metadata_path(task_id).exists():
            self._write_task_metadata_payload(TaskMetadata(task_id))
        self.locks.atomic_write(
            self._state_path(task_id), json.dumps(payload, indent=2) + "\n"
        )
        return revision + 1

    def write_command_output(self, address: CommandOutputAddress, content: str) -> str:
        path = self._run_path(address.task_id, address.run_id).joinpath(
            "command-output", *address.segments()
        )
        self.locks.atomic_write(path, content)
        return str(path.relative_to(self.root))

    def read_command_output(self, reference: str) -> str:
        return self._read_task_file(reference, "command output")

    def _read_task_file(self, reference: str, kind: str) -> str:
        path = (self.root / reference).resolve()
        task_root = self.tasks_path.resolve()
        if not path.is_relative_to(task_root):
            raise StateError(f"invalid {kind} reference: {reference!r}")
        try:
            return path.read_text(encoding="utf-8")
        except OSError as error:
            raise StateError(f"cannot read {kind} {reference!r}: {error}") from error

    def task_ids(self) -> tuple[str, ...]:
        if not self.tasks_path.is_dir():
            return ()
        return tuple(
            sorted(
                path.name
                for path in self.tasks_path.iterdir()
                if path.is_dir()
                and not path.is_symlink()
                and not path.name.startswith(BOOTSTRAP_REQUEST_PREFIX)
            )
        )

    def task_exists(self, task_id: str) -> bool:
        # Any task directory claims the ID, even one holding only artifacts or
        # a child task, so a generated ID never adopts a partially written task.
        return (self.tasks_path / task_id).exists()

    def read_task_metadata(self, task_id: str) -> TaskMetadata | None:
        path = self._metadata_path(task_id)
        if path.exists():
            return self._read_task_metadata_path(path, task_id)
        return None

    def _read_task_metadata_path(self, path: Path, task_id: str) -> TaskMetadata:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("task_id") != task_id:
                raise ValueError("task metadata has a mismatched task ID")
            unknown = set(raw) - {"task_id", "metadata"}
            if unknown:
                raise ValueError(
                    "task metadata has unknown field(s): " + ", ".join(sorted(unknown))
                )
            values = flatten_metadata(raw.get("metadata", {}), "task metadata")
            return TaskMetadata(task_id, values)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise StateError(f"invalid task metadata {path}: {error}") from error

    def write_task_metadata(self, metadata: TaskMetadata) -> None:
        self._write_task_metadata_payload(metadata)

    def read_shared_items(self, task_id: str) -> tuple[WorkItem, ...]:
        path = self._shared_items_path(task_id)
        if not path.exists():
            return ()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("task_id") != task_id:
                raise ValueError("shared items have a mismatched task ID")
            entries = raw.get("items", [])
            if not isinstance(entries, list):
                raise ValueError("shared items must be a list")
            return tuple(WorkItem.from_dict(entry) for entry in entries)
        except (OSError, ValueError) as error:
            raise StateError(
                f"cannot read shared items of {task_id!r}: {error}"
            ) from error

    def write_shared_items(self, task_id: str, items: tuple[WorkItem, ...]) -> None:
        payload = {"task_id": task_id, "items": [item.to_dict() for item in items]}
        self.locks.atomic_write(
            self._shared_items_path(task_id), json.dumps(payload, indent=2) + "\n"
        )

    def _write_task_metadata_payload(self, metadata: TaskMetadata) -> None:
        payload: dict[str, object] = {"task_id": metadata.task_id}
        if metadata.values:
            payload["metadata"] = metadata.to_dict()
        self.locks.atomic_write(
            self._metadata_path(metadata.task_id),
            json.dumps(payload, indent=2) + "\n",
        )

    def remove_task(self, task_id: str) -> bool:
        path = self.tasks_path / task_id
        if not path.exists():
            return False
        if path.is_symlink() or not path.is_dir():
            raise StateError(f"task path is not a removable directory: {path}")
        # Do not recursively remove nested task directories.  Service-level
        # reset rejects them; direct storage-adapter callers still get exact ownership.
        owned_paths = (
            self._state_path(task_id),
            self._metadata_path(task_id),
            self._shared_items_path(task_id),
        )
        for owned in owned_paths:
            if owned.exists() and not owned.is_symlink() and owned.is_file():
                owned.unlink()
        runs = path / "runs"
        if runs.exists() and not runs.is_symlink() and runs.is_dir():
            shutil.rmtree(runs)
        # A child task or unrecognized file may remain; it stays intact.
        with contextlib.suppress(OSError):
            path.rmdir()
        return True

    def child_task_ids(self, task_id: str) -> tuple[str, ...]:
        path = self.tasks_path / task_id
        if not path.is_dir() or path.is_symlink():
            return ()
        descendants = []
        for state in path.rglob("state.json"):
            relative = state.parent.relative_to(self.tasks_path)
            candidate = relative.as_posix()
            if candidate != task_id:
                descendants.append(candidate)
        return tuple(sorted(set(descendants)))

    def write_execution_artifact(self, address: ArtifactAddress, content: str) -> str:
        path = self._run_path(address.task_id, address.run_namespace).joinpath(
            "steps", *address.segments()
        )
        self.locks.atomic_write(path, content)
        return str(path.relative_to(self.root))

    def _run_path(self, task_id: str, run_id: str) -> Path:
        return self.tasks_path / task_id / "runs" / run_id

    def read_execution_artifact(self, reference: str) -> str:
        return self._read_task_file(reference, "execution artifact")

    def _state_path(self, task_id: str) -> Path:
        return self.tasks_path / task_id / "state.json"

    def _metadata_path(self, task_id: str) -> Path:
        return self.tasks_path / task_id / "metadata.json"

    def _shared_items_path(self, task_id: str) -> Path:
        return self.tasks_path / task_id / "items.json"

    @staticmethod
    def _validate_ledger(value: object) -> dict[str, list[dict[str, object]]]:
        if not isinstance(value, dict):
            raise StateError("task aggregate ledger must be a mapping")
        if any(
            not isinstance(run_id, str)
            or not isinstance(events, list)
            or not events
            or any(
                not isinstance(event, dict)
                or not isinstance(event.get("workflow"), str)
                or not isinstance(event.get("status"), str)
                or event.get("summary") is not None
                and not isinstance(event.get("summary"), str)
                for event in events
            )
            for run_id, events in value.items()
        ):
            raise StateError("task aggregate ledger entries are invalid")
        return value
