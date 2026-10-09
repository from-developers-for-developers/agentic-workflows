# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed persistence ports used by the plan executor."""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime

from ww.amendments import Amendment
from ww.children import ChildTask
from ww.contracts import RunStatus, run_is_open
from ww.direct_work import DirectWork
from ww.errors import StateError
from ww.execution_models import (
    ExecutionState,
    PlanSnapshot,
    TaskRunAggregate,
    WorkflowRunSummary,
)
from ww.items import WorkItem
from ww.variables import METADATA_PREFIX, PROJECT_METADATA_PREFIX

# A dotted metadata key, e.g. "github.owner"; "github..owner" does not match.
_METADATA_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*")
# One metadata field name, e.g. "owner" or "pr-url".
_METADATA_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
# A leaf is a string, or a list of strings for a key declared ``append: true``.
MetadataLeaf = str | tuple[str, ...]
MetadataValues = tuple[tuple[str, MetadataLeaf], ...]


def is_metadata_leaf(value: object) -> bool:
    return isinstance(value, str) or (
        isinstance(value, tuple) and all(isinstance(item, str) for item in value)
    )


def append_metadata_leaf(
    existing: MetadataLeaf | None, values: tuple[str, ...]
) -> tuple[str, ...]:
    """Append values to a list leaf, keeping order and dropping repeats."""
    if existing is None:
        current: tuple[str, ...] = ()
    elif isinstance(existing, str):
        current = (existing,)
    else:
        current = existing
    return tuple(dict.fromkeys((*current, *values)))


def metadata_leaf_text(value: MetadataLeaf) -> str:
    """The interpolated form of a leaf: a list reads as ``a, b``."""
    return value if isinstance(value, str) else ", ".join(value)


def validate_metadata_values(values: MetadataValues, label: str) -> None:
    """Reject non-string leaves and dotted keys that collide with each other."""
    keys: set[str] = set()
    for key, value in values:
        if (
            not isinstance(key, str)
            or not is_metadata_leaf(value)
            or not _METADATA_KEY.fullmatch(key)
        ):
            raise ValueError(f"invalid {label} entry: {key!r}")
        if key in keys or any(
            key.startswith(f"{other}.") or other.startswith(f"{key}.") for other in keys
        ):
            raise ValueError(f"conflicting {label} key: {key!r}")
        keys.add(key)


def nest_metadata(values: MetadataValues) -> dict[str, object]:
    """Project dotted leaf keys into a nested JSON-ready mapping."""
    result: dict[str, object] = {}
    for key, value in values:
        target = result
        *parents, leaf = key.split(".")
        for segment in parents:
            child = target.setdefault(segment, {})
            if not isinstance(child, dict):  # protected by validation
                raise ValueError(f"conflicting metadata key: {key!r}")
            target = child
        target[leaf] = list(value) if isinstance(value, tuple) else value
    return result


def flatten_metadata(mapping: object, label: str) -> MetadataValues:
    """Read a nested metadata mapping back into dotted leaf values."""
    if not isinstance(mapping, dict):
        raise ValueError(f"{label} value must be a mapping")
    values: list[tuple[str, MetadataLeaf]] = []

    def collect(node: dict[object, object], prefix: str) -> None:
        for field, value in node.items():
            if not isinstance(field, str) or not _METADATA_FIELD.fullmatch(field):
                raise ValueError(f"{label} field names must be normalized names")
            key = f"{prefix}.{field}" if prefix else field
            if isinstance(value, dict):
                if not value:
                    raise ValueError(f"{label} mappings cannot be empty")
                collect(value, key)
            elif isinstance(value, str):
                values.append((key, value))
            elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                values.append((key, tuple(value)))
            else:
                raise ValueError(f"{label} field values must be strings")

    collect(mapping, "")
    return tuple(values)


@dataclass(frozen=True)
class TaskMetadata:
    """Storage-adapter-owned task metadata independent of workflow progress."""

    task_id: str
    values: MetadataValues = ()

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task metadata requires a task ID")
        validate_metadata_values(self.values, "task metadata")

    @property
    def interpolation_values(self) -> dict[str, str]:
        return {
            f"{METADATA_PREFIX}{key}": metadata_leaf_text(value)
            for key, value in self.values
        }

    def to_dict(self) -> dict[str, object]:
        return nest_metadata(self.values)


@dataclass(frozen=True)
class ProjectMetadata:
    """Storage-adapter-owned metadata shared by every task in one project."""

    values: MetadataValues = ()

    def __post_init__(self) -> None:
        validate_metadata_values(self.values, "project metadata")

    @property
    def interpolation_values(self) -> dict[str, str]:
        return {
            f"{PROJECT_METADATA_PREFIX}{key}": metadata_leaf_text(value)
            for key, value in self.values
        }

    def to_dict(self) -> dict[str, object]:
        return nest_metadata(self.values)


@dataclass(frozen=True)
class ArtifactAddress:
    """Where one logical artifact lives beneath its run.

    Rewriting the same address replaces the content and keeps the reference.
    Declaration ordinals are part of the address so nested step executions
    do not overwrite earlier results.
    """

    task_id: str
    workflow: str
    step_path: str
    position: int
    name: str
    phase: str
    run_id: str | None = None
    step_ordinals: tuple[int, ...] = ()

    @property
    def run_namespace(self) -> str:
        return self.run_id or f"01-{self.workflow}"

    def segments(self) -> tuple[str, ...]:
        """Return the path parts beneath the run's ``steps`` directory."""
        names = self.step_path.split("/")
        ordinals = self.step_ordinals or tuple(range(1, len(names) + 1))
        if len(ordinals) != len(names):
            raise StateError("step ordinals must match the step path")
        parts: list[str] = []
        for ordinal, segment in zip(ordinals, names, strict=True):
            parts.append(f"{ordinal:02d}-{segment}")
        if self.phase == "step":
            return (*parts[:-1], f"{parts[-1]}.md")
        return (*parts, ".hooks", self.phase, f"{self.position:02d}-{self.name}.md")


@dataclass(frozen=True)
class CommandOutputAddress:
    """Immutable evidence address for one command stream of one attempt.

    Later retries never replace a stream returned for an
    earlier operation attempt, so ``attempt`` is part of the address.
    """

    task_id: str
    run_id: str
    item_id: str
    operation_id: str
    attempt: int
    command_index: int
    stream: str

    def __post_init__(self) -> None:
        if self.stream not in {"stdout", "stderr"}:
            raise StateError(f"invalid command output stream: {self.stream!r}")
        if self.attempt < 1:
            raise StateError("command output attempt must be positive")

    def segments(self) -> tuple[str, ...]:
        """Return the path parts beneath the run's ``command-output`` directory."""
        return (
            _short_digest(self.item_id),
            _short_digest(self.operation_id),
            f"attempt-{self.attempt:02d}",
            f"{self.command_index:02d}.{self.stream}",
        )


def _short_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class TaskRunStorage(ABC):
    """Authoritative storage for task runs and their transition data.

    Missing tasks have revision zero and return an empty run tuple. Malformed
    records and compare-and-swap conflicts must raise ``StateError``. A commit
    publishes the complete run tuple and handoff atomically; no earlier value
    may become unreadable if a commit fails.
    """

    def lock_task(self, task_id: str) -> AbstractContextManager[None]:
        """Serialize a complete read/modify/commit transition for one task.

        Shared storage adapters must override this method. Process-local
        storage adapters may use the default no-op lock.
        """
        del task_id
        return nullcontext()

    @abstractmethod
    def read_task_record(
        self, task_id: str
    ) -> tuple[tuple[TaskRunAggregate, ...], str | None, int]:
        """Return all runs, the handoff, and the CAS revision in one read.

        A missing task is ``((), None, 0)``.
        """

    @abstractmethod
    def task_written_at(self, task_id: str) -> datetime | None:
        """When the task's runs were last committed, or ``None`` without any.

        Scans that only care about recent tasks call this first, so it must
        not read or decode the record itself.
        """

    def read_task_aggregate(
        self, task_id: str
    ) -> tuple[tuple[TaskRunAggregate, ...], str | None]:
        runs, handoff, _ = self.read_task_record(task_id)
        return runs, handoff

    def task_aggregate_revision(self, task_id: str) -> int:
        return self.read_task_record(task_id)[2]

    @abstractmethod
    def commit_task_aggregate(
        self,
        task_id: str,
        runs: tuple[TaskRunAggregate, ...],
        handoff: str | None = None,
        expected_revision: int | None = None,
    ) -> int:
        """Atomically replace a task aggregate and return its new revision.

        The caller holds :meth:`lock_task` across the complete transition.
        ``expected_revision`` is required when replacing an existing record.
        The implementation must not expose a partial publication.
        """

    def execution_runs(self, task_id: str) -> tuple[WorkflowRunSummary, ...]:
        """Return run summaries in creation order; missing tasks return empty."""
        return self.summarize_runs(self.read_task_aggregate(task_id)[0])

    @classmethod
    def summarize_runs(
        cls, runs: tuple[TaskRunAggregate, ...]
    ) -> tuple[WorkflowRunSummary, ...]:
        """Summarize already-read runs without another storage read."""
        return tuple(
            WorkflowRunSummary(
                run_id=run.run_id,
                workflow=run.workflow,
                status=cls._run_status(run),
                summary=dict(run.state.workflow_values).get("summary")
                if run.state.status == "completed"
                else None,
            )
            for run in runs
        )

    @staticmethod
    def _run_status(run: TaskRunAggregate) -> RunStatus:
        if run.state.status in {"completed", "failed"}:
            return run.state.status
        if any(record.status != "pending" for record in run.state.item_executions):
            return "in_progress"
        return "pending"

    @staticmethod
    def _active_run(runs: tuple[TaskRunAggregate, ...]) -> TaskRunAggregate | None:
        return next(
            (run for run in reversed(runs) if run_is_open(run.state.status)), None
        )

    def active_execution_run(self, task_id: str) -> str | None:
        """Return the sole non-completed run ID, or ``None``."""
        active = self._active_run(self.read_task_aggregate(task_id)[0])
        return active.run_id if active is not None else None

    @staticmethod
    def run_id_for(number: int, workflow: str) -> str:
        """Format the run namespace for the ``number``-th run of a task."""
        return f"{number:02d}-{workflow}"

    def next_execution_run_id(self, task_id: str, workflow: str) -> str:
        """Return the next run namespace without publishing a partial run."""
        runs, _ = self.read_task_aggregate(task_id)
        if self._active_run(runs) is not None:
            raise StateError(f"task {task_id!r} already has an active workflow run")
        return self.run_id_for(len(runs) + 1, workflow)

    def _run(self, task_id: str, run_id: str | None = None) -> TaskRunAggregate | None:
        """Return the named run, else the active run, else the latest run."""
        runs, _ = self.read_task_aggregate(task_id)
        if run_id is not None:
            return next((run for run in runs if run.run_id == run_id), None)
        active = self._active_run(runs)
        if active is not None:
            return active
        return runs[-1] if runs else None

    def read_execution_state(
        self, task_id: str, run_id: str | None = None
    ) -> ExecutionState | None:
        """Return one run's state, or ``None`` when it is missing."""
        run = self._run(task_id, run_id)
        return run.state if run is not None else None

    def read_plan_snapshot(
        self, task_id: str, run_id: str | None = None
    ) -> PlanSnapshot | None:
        """Return one run's plan, or ``None`` when it is missing."""
        run = self._run(task_id, run_id)
        return run.snapshot if run is not None else None

    def read_items(
        self, task_id: str, run_id: str | None = None
    ) -> tuple[WorkItem, ...]:
        """Return one run's work items; missing runs have no items."""
        run = self._run(task_id, run_id)
        return run.items if run is not None else ()

    def read_children(
        self, task_id: str, run_id: str | None = None
    ) -> tuple[ChildTask, ...]:
        """Return one run's children; missing runs have no children."""
        run = self._run(task_id, run_id)
        return run.children if run is not None else ()

    def read_handoff(self, task_id: str) -> str | None:
        """Return the task handoff, or ``None`` when absent."""
        return self.read_task_aggregate(task_id)[1]


class TaskArtifactStorage(ABC):
    """Storage for stable references to agent-produced artifacts."""

    @abstractmethod
    def write_execution_artifact(self, address: ArtifactAddress, content: str) -> str:
        """Store content and return its stable caller-facing reference.

        Storage errors must be raised to the caller.
        """

    @abstractmethod
    def read_execution_artifact(self, reference: str) -> str:
        """Read an artifact previously returned by ``write_execution_artifact``."""

    @abstractmethod
    def write_command_output(self, address: CommandOutputAddress, content: str) -> str:
        """Store one operation attempt's command stream and return its reference."""

    @abstractmethod
    def read_command_output(self, reference: str) -> str:
        """Read a command stream previously returned by ``write_command_output``."""


class TaskItemStorage(ABC):
    """Storage for the items a task shares across its runs.

    A run keeps its own copy of the items it worked on; this is the task's
    canonical list, seeded into every new run and refreshed as a run changes
    or resolves them.  Only item flows declared ``shared`` use it.
    """

    @abstractmethod
    def read_shared_items(self, task_id: str) -> tuple[WorkItem, ...]:
        """The task's shared items; a task without any has none."""

    @abstractmethod
    def write_shared_items(self, task_id: str, items: tuple[WorkItem, ...]) -> None:
        """Atomically replace the task's shared items."""


class TaskAmendmentStorage(ABC):
    """Storage for the amendments appended to a task's requirements.

    They belong to the task, not to a run, and are only ever appended.
    """

    @abstractmethod
    def read_amendments(self, task_id: str) -> tuple[Amendment, ...]:
        """The task's amendments, oldest first; a task without any has none."""

    @abstractmethod
    def append_amendment(self, task_id: str, amendment: Amendment) -> None:
        """Append one amendment; the caller holds the task's lock."""


class TaskDirectWorkStorage(ABC):
    """Storage for the direct work registered against a task.

    Direct work is a change made outside any workflow and registered
    afterwards. It belongs to the task, not to a run, and is only ever
    appended. A task may hold it without having any run.
    """

    @abstractmethod
    def read_direct_work(self, task_id: str) -> tuple[DirectWork, ...]:
        """The task's direct-work entries, oldest first; none when absent."""

    @abstractmethod
    def append_direct_work(self, task_id: str, entry: DirectWork) -> None:
        """Append one entry, creating the task's storage; the caller holds the lock."""


class TaskMetadataStorage(ABC):
    """Storage for durable metadata shared by every run of one task."""

    @abstractmethod
    def read_task_metadata(self, task_id: str) -> TaskMetadata | None:
        """Return validated metadata, or ``None`` when it is missing."""

    @abstractmethod
    def write_task_metadata(self, metadata: TaskMetadata) -> None:
        """Atomically create or replace metadata for exactly one task."""


class ProjectMetadataStorage(ABC):
    """Storage for durable metadata shared by every task in one project."""

    def lock_project_metadata(self) -> AbstractContextManager[None]:
        """Serialize a complete project-metadata read/modify/write operation."""
        return nullcontext()

    @abstractmethod
    def read_project_metadata(self) -> ProjectMetadata | None:
        """Return validated metadata, or ``None`` when it is missing."""

    @abstractmethod
    def write_project_metadata(self, metadata: ProjectMetadata) -> None:
        """Atomically create or replace project metadata."""


class TaskStorageAdapter(
    TaskRunStorage,
    TaskArtifactStorage,
    TaskMetadataStorage,
    TaskItemStorage,
    TaskAmendmentStorage,
    TaskDirectWorkStorage,
    ABC,
):
    """Complete storage-adapter boundary required by ``WorkflowService``.

    Removal covers the aggregate, artifacts, metadata, and all projections for
    exactly one task. It returns ``False`` only when no task-owned data existed.
    """

    def task_exists(self, task_id: str) -> bool:
        """Return whether any task-owned data claims ``task_id``.

        Runs, metadata, and descendant tasks all count; adapters whose backend
        can hold other task-owned data (a directory, stored artifacts) extend
        this so a generated ID never collides with a partially written task.
        """
        runs, _, _ = self.read_task_record(task_id)
        return (
            bool(runs)
            or self.read_task_metadata(task_id) is not None
            or bool(self.read_direct_work(task_id))
            or bool(self.child_task_ids(task_id))
        )

    @abstractmethod
    def task_ids(self) -> tuple[str, ...]:
        """Return every top-level task ID that holds task-owned data, sorted.

        Child tasks are left out: a person names a task by its own ID, and a
        child is reached through its parent.
        """

    @abstractmethod
    def remove_task(self, task_id: str) -> bool:
        """Remove all data for exactly one task and report whether it existed."""

    def child_task_ids(self, task_id: str) -> tuple[str, ...]:
        """Return persisted descendants of ``task_id`` in deterministic order.

        Reset uses this to reject a parent reset rather than deleting state
        protected by a child task's independent lock.
        """
        del task_id
        return ()


__all__ = [
    "ArtifactAddress",
    "CommandOutputAddress",
    "MetadataValues",
    "ProjectMetadata",
    "ProjectMetadataStorage",
    "TaskArtifactStorage",
    "TaskDirectWorkStorage",
    "TaskMetadata",
    "TaskMetadataStorage",
    "TaskRunStorage",
    "TaskStorageAdapter",
    "flatten_metadata",
    "nest_metadata",
    "validate_metadata_values",
]
