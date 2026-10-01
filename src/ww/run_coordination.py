# SPDX-License-Identifier: GPL-3.0-or-later
"""Atomic run loading and publication across storage adapters."""

from __future__ import annotations

from typing import Protocol

from ww.children import ChildTask
from ww.contracts import run_is_open
from ww.errors import StateError
from ww.execution_models import ExecutionState, PlanSnapshot, TaskRunAggregate
from ww.instructions import Instruction
from ww.items import WorkItem
from ww.storage_adapters import TaskStorageAdapter


class RunLifecycle(Protocol):
    """The run operations the workflow service lends to its coordinators.

    Coordinators (recovery, children) decide *what* a transition means; the
    service keeps ownership of locks, commits, draining, and rendering.
    """

    def load(
        self, task_id: str, run_id: str | None = None
    ) -> tuple[ExecutionState, PlanSnapshot]: ...

    def commit(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        *,
        items: tuple[WorkItem, ...] | None = None,
        children: tuple[ChildTask, ...] | None = None,
        handoff: str | None = None,
        bootstrap_request_id: str | None = None,
    ) -> None: ...

    def drain(
        self, state: ExecutionState, snapshot: PlanSnapshot
    ) -> tuple[ExecutionState, PlanSnapshot]: ...

    def resume(self, state: ExecutionState, snapshot: PlanSnapshot) -> Instruction:
        """Commit, continue the active assignment or drain, then instruct."""
        ...

    def render(self, state: ExecutionState, snapshot: PlanSnapshot) -> Instruction:
        """Build the instruction for a loaded run without changing it."""
        ...

    def instruction_status(self, task_id: str, run_id: str | None) -> Instruction: ...

    def mark_interrupted(
        self, state: ExecutionState, snapshot: PlanSnapshot
    ) -> ExecutionState: ...


class RunCoordinator:
    """Coordinate complete run records without owning lifecycle decisions."""

    def __init__(self, tasks: TaskStorageAdapter) -> None:
        self.tasks = tasks

    def commit_run(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        *,
        items: tuple[WorkItem, ...] | None = None,
        children: tuple[ChildTask, ...] | None = None,
        handoff: str | None = None,
        bootstrap_request_id: str | None = None,
    ) -> None:
        """Atomically publish the complete record for one run transition."""
        runs, existing_handoff, revision = self.tasks.read_task_record(state.task_id)
        current = next((run for run in runs if run.run_id == state.run_id), None)
        aggregate = TaskRunAggregate(
            run_id=state.run_id,
            workflow=state.workflow,
            snapshot=snapshot,
            state=state,
            items=items if items is not None else (current.items if current else ()),
            children=(
                children
                if children is not None
                else (current.children if current else ())
            ),
            bootstrap_request_id=(
                bootstrap_request_id
                if bootstrap_request_id is not None
                else (current.bootstrap_request_id if current else None)
            ),
        )
        replaced = tuple(
            aggregate if run.run_id == aggregate.run_id else run for run in runs
        )
        if current is None:
            replaced += (aggregate,)
        self.tasks.commit_task_aggregate(
            state.task_id,
            replaced,
            handoff if handoff is not None else existing_handoff,
            expected_revision=revision,
        )

    def commit_runs(
        self,
        task_id: str,
        aggregates: tuple[TaskRunAggregate, ...],
        *,
        handoff: str | None = None,
    ) -> None:
        """Atomically publish a coordinated multi-run transition."""
        _, existing_handoff, revision = self.tasks.read_task_record(task_id)
        self.tasks.commit_task_aggregate(
            task_id,
            aggregates,
            handoff if handoff is not None else existing_handoff,
            expected_revision=revision,
        )

    @staticmethod
    def select_run(
        runs: tuple[TaskRunAggregate, ...], run_id: str | None = None
    ) -> TaskRunAggregate | None:
        """Return the named run, else the active run, else the latest run."""
        resolved = run_id or next(
            (run.run_id for run in reversed(runs) if run_is_open(run.state.status)),
            runs[-1].run_id if runs else None,
        )
        return next((run for run in runs if run.run_id == resolved), None)

    def load(
        self, task_id: str, run_id: str | None = None
    ) -> tuple[ExecutionState, PlanSnapshot]:
        """Load one complete run and verify plan/state identity."""
        runs, _, _ = self.tasks.read_task_record(task_id)
        return self.resolve(task_id, runs, run_id)

    def resolve(
        self,
        task_id: str,
        runs: tuple[TaskRunAggregate, ...],
        run_id: str | None = None,
    ) -> tuple[ExecutionState, PlanSnapshot]:
        """Select and verify one run from an already-read record."""
        aggregate = self.select_run(runs, run_id)
        if aggregate is None:
            raise StateError(f"task {task_id!r} has not been started; use start")
        state = aggregate.state
        snapshot = aggregate.snapshot
        if state.plan_revision != snapshot.plan_revision:
            raise StateError(
                "task state does not match its authoritative plan revision"
            )
        if state.plan_digest and state.plan_digest != snapshot.plan_digest:
            raise StateError("task state does not match its authoritative plan digest")
        return state, snapshot
