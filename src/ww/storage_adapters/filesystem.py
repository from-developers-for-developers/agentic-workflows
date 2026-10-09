# SPDX-License-Identifier: GPL-3.0-or-later
"""Filesystem implementation of task persistence."""

from __future__ import annotations

import contextlib
import json
import shutil
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ww.amendments import Amendment
from ww.contracts import BOOTSTRAP_REQUEST_PREFIX
from ww.direct_work import DirectWork
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
    RunRevisionMismatchError,
    check_active_run,
    decode_run_document,
    decode_task_index,
    encode_run_document,
    encode_task_index,
)


@dataclass(frozen=True)
class _StoredTask:
    """A decoded task record and the run documents it was read from."""

    runs: tuple[TaskRunAggregate, ...]
    handoff: str | None
    revision: int
    ledger: dict[str, list[dict[str, object]]]
    documents: dict[str, dict[str, object]]


def _same_run(stored: dict[str, object], encoded: dict[str, object]) -> bool:
    """Whether two run documents differ only in the revision they carry."""
    return _canonical({**stored, "revision": None}) == _canonical(
        {**encoded, "revision": None}
    )


def _canonical(document: dict[str, object]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


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
        stored = self._read_task_document(task_id)
        return stored.runs, stored.handoff, stored.revision

    def task_written_at(self, task_id: str) -> datetime | None:
        # Every commit replaces the task index, so its modification time is
        # the last commit's.
        try:
            modified = self._state_path(task_id).stat().st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(modified, timezone.utc)

    def _read_task_document(
        self, task_id: str, *, locked: bool = False
    ) -> _StoredTask:
        """Read the index and every run document it names.

        Reads are unlocked, so a reader can meet run documents a writer
        published after the index it read.  It then waits for the commit lock
        and reads again; a mismatch that survives that is an interrupted
        commit.  ``locked`` callers already hold the lock.
        """
        if not self._state_path(task_id).exists():
            return _StoredTask((), None, 0, {}, {})
        try:
            return self._decode_task_files(task_id)
        except RunRevisionMismatchError as error:
            if locked:
                raise StateError(str(error)) from error
        with self.locks.lock(
            self._state_path(task_id), purpose=f"task state {task_id!r}"
        ):
            try:
                return self._decode_task_files(task_id)
            except RunRevisionMismatchError as error:
                raise StateError(str(error)) from error

    def _decode_task_files(self, task_id: str) -> _StoredTask:
        path = self._state_path(task_id)
        try:
            index = decode_task_index(self._load_json(path), task_id)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise StateError(f"invalid task state {path}: {error}") from error
        runs: list[TaskRunAggregate] = []
        documents: dict[str, dict[str, object]] = {}
        for run_id, revision in index.run_revisions:
            run_path = self._run_state_path(task_id, run_id)
            try:
                document = self._load_json(run_path)
                if not isinstance(document, dict):
                    raise ValueError("run state must be a mapping")
                runs.append(
                    decode_run_document(document, task_id, run_id, revision)
                )
            except RunRevisionMismatchError as error:
                raise RunRevisionMismatchError(
                    f"invalid task state {run_path}: {error}"
                ) from error
            except (OSError, json.JSONDecodeError, StateError, ValueError) as error:
                raise StateError(
                    f"invalid task state {run_path}: {error}"
                ) from error
            documents[run_id] = document
        try:
            check_active_run(index, tuple(runs))
            ledger = self._validate_ledger(index.ledger)
            validate_task_runs(task_id, tuple(runs))
        except (StateError, ValueError) as error:
            raise StateError(f"invalid task state {path}: {error}") from error
        return _StoredTask(
            tuple(runs), index.handoff, index.revision, ledger, documents
        )

    @staticmethod
    def _load_json(path: Path) -> object:
        return json.loads(path.read_text(encoding="utf-8"))

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
        stored = self._read_task_document(task_id, locked=True)
        previous, revision, ledger = stored.runs, stored.revision, stored.ledger
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
        new_revision = revision + 1
        # Only runs whose encoding changed are rewritten; the rest keep the
        # revision their document was written at.
        run_revisions: dict[str, int] = {}
        documents = dict(stored.documents)
        changed_documents: dict[str, dict[str, object]] = {}
        try:
            for run in runs:
                document = encode_run_document(task_id, run, new_revision)
                current = stored.documents.get(run.run_id)
                if current is not None and _same_run(current, document):
                    run_revisions[run.run_id] = current["revision"]  # type: ignore[assignment]
                    continue
                run_revisions[run.run_id] = new_revision
                documents[run.run_id] = changed_documents[run.run_id] = document
            index = encode_task_index(
                task_id, runs, run_revisions, handoff, new_revision, ledger
            )
            # Validate the exact compact representation before publishing it.
            decoded_index = decode_task_index(index, task_id)
            decoded = tuple(
                decode_run_document(documents[run_id], task_id, run_id, written)
                for run_id, written in decoded_index.run_revisions
            )
            check_active_run(decoded_index, decoded)
        except ValueError as error:
            raise StateError(
                f"task {task_id!r} aggregate cannot be encoded: {error}"
            ) from error
        validate_task_runs(task_id, decoded)
        if not self._metadata_path(task_id).exists():
            self._write_task_metadata_payload(TaskMetadata(task_id))
        # Run documents first and the index last: a reader trusts only runs
        # at the revisions the published index names.
        for run_id, document in changed_documents.items():
            self.locks.atomic_write(
                self._run_state_path(task_id, run_id),
                json.dumps(document, indent=2) + "\n",
            )
        self.locks.atomic_write(
            self._state_path(task_id), json.dumps(index, indent=2) + "\n"
        )
        for run_id in set(stored.documents) - set(run_revisions):
            self._run_state_path(task_id, run_id).unlink(missing_ok=True)
        return new_revision

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

    def read_amendments(self, task_id: str) -> tuple[Amendment, ...]:
        path = self._amendments_path(task_id)
        if not path.exists():
            return ()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("task_id") != task_id:
                raise ValueError("amendments have a mismatched task ID")
            entries = raw.get("amendments", [])
            if not isinstance(entries, list):
                raise ValueError("amendments must be a list")
            return tuple(Amendment.from_dict(entry) for entry in entries)
        except (OSError, ValueError) as error:
            raise StateError(
                f"cannot read amendments of {task_id!r}: {error}"
            ) from error

    def append_amendment(self, task_id: str, amendment: Amendment) -> None:
        amendments = (*self.read_amendments(task_id), amendment)
        payload = {
            "task_id": task_id,
            "amendments": [entry.to_dict() for entry in amendments],
        }
        self.locks.atomic_write(
            self._amendments_path(task_id), json.dumps(payload, indent=2) + "\n"
        )

    def read_direct_work(self, task_id: str) -> tuple[DirectWork, ...]:
        path = self._direct_work_path(task_id)
        if not path.exists():
            return ()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("direct work must be a list")
            return tuple(DirectWork.from_dict(entry) for entry in raw)
        except (OSError, ValueError) as error:
            raise StateError(
                f"cannot read direct work of {task_id!r}: {error}"
            ) from error

    def append_direct_work(self, task_id: str, entry: DirectWork) -> None:
        entries = (*self.read_direct_work(task_id), entry)
        self.locks.atomic_write(
            self._direct_work_path(task_id),
            json.dumps([item.to_dict() for item in entries], indent=2) + "\n",
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
            self._amendments_path(task_id),
            self._direct_work_path(task_id),
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
        # A child that was only recorded, never run, has no state file.  A
        # task's own ``runs`` directory holds run documents, not children.
        pending = [path]
        while pending:
            directory = pending.pop()
            for entry in directory.iterdir():
                if entry.is_dir() and not entry.is_symlink() and entry.name != "runs":
                    pending.append(entry)
            if directory != path and any(
                (directory / name).is_file()
                for name in ("state.json", "direct-work.json")
            ):
                descendants.append(directory.relative_to(self.tasks_path).as_posix())
        return tuple(sorted(descendants))

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

    def _run_state_path(self, task_id: str, run_id: str) -> Path:
        return self._run_path(task_id, run_id) / "state.json"

    def _metadata_path(self, task_id: str) -> Path:
        return self.tasks_path / task_id / "metadata.json"

    def _amendments_path(self, task_id: str) -> Path:
        return self.tasks_path / task_id / "amendments.json"

    def _direct_work_path(self, task_id: str) -> Path:
        return self.tasks_path / task_id / "direct-work.json"

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
