# SPDX-License-Identifier: GPL-3.0-or-later
"""Parent and child workflow coordination.

A parent workflow collects children, then a child-workflow coordinator item
runs the configured workflow once per child, one at a time.  With per-child
parent stages each child has its own coordinator item, which runs only that
child.  The parent's child records are a projection of the children's own
run records; every reconciliation re-reads those records so an interrupted
publication can be repaired instead of replayed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from ww.children import ChildTask
from ww.completion_artifacts import write_completion_artifacts
from ww.contracts import ChildStatus, run_is_open
from ww.control import child_workflow
from ww.errors import StateError
from ww.execution_models import ExecutionState, PlanSnapshot
from ww.instructions import Instruction
from ww.plan import PlanItem
from ww.run_coordination import RunLifecycle
from ww.runtimes import runtime_instruction
from ww.storage_adapters import TaskStorageAdapter
from ww.task_ids import is_bootstrap_request, validate_child_id, validate_task_id
from ww.transitions import (
    Clock,
    begin_child_workflow,
    complete_child_summary,
    complete_child_workflow,
    fail_child_workflow,
    retry_failed_item,
)


class StartChildRun(Protocol):
    """Start the child's own workflow run bound to its parent."""

    def __call__(
        self,
        task_id: str,
        workflow_name: str,
        mode_names: tuple[str, ...],
        agent: str,
        *,
        parent_task_id: str,
        start_operation_id: str,
        model: str,
        reasoning: str,
        workflow_runtime: str,
        init_artifact: str,
        project: str | None,
    ) -> Instruction: ...


class StartChildIdentity(Protocol):
    """Open the identity request through which a child obtains its own ID."""

    def __call__(
        self, child: ChildTask, workflow_name: str, parent: ExecutionState
    ) -> Instruction: ...


class ChildCoordinator:
    def __init__(
        self,
        tasks: TaskStorageAdapter,
        lifecycle: RunLifecycle,
        start_run: StartChildRun,
        now: Clock,
        validate_workflow: Callable[[str, ChildTask, ExecutionState], None],
        start_identity: StartChildIdentity | None = None,
    ) -> None:
        self.tasks = tasks
        self.lifecycle = lifecycle
        self.start_run = start_run
        self.now = now
        self.validate_workflow = validate_workflow
        self.start_identity = start_identity

    def start_child(
        self,
        parent_task_id: str,
        child_id: str,
        *,
        workflow_name: str | None = None,
        workflow_runtime: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        agent: str | None = None,
    ) -> Instruction:
        validate_task_id(parent_task_id)
        validate_child_id(child_id)
        with self.tasks.lock_task(parent_task_id):
            parent, snapshot = self.lifecycle.load(parent_task_id)
            if parent.cursor >= len(snapshot.plan.items):
                raise StateError("parent workflow is already completed")
            coordinator = child_workflow(snapshot.plan.items[parent.cursor])
            if coordinator is None:
                raise StateError("parent workflow is not running its children")
            children = list(self.tasks.read_children(parent_task_id, parent.run_id))
            by_id = {child.id: child for child in children}
            child = by_id.get(child_id)
            if child is None:
                raise StateError(f"child {child_id!r} was not found")
            current = snapshot.plan.items[parent.cursor].child_number
            if current is not None and children[current - 1].id != child_id:
                raise StateError(
                    f"the parent runs child {children[current - 1].id!r} now; "
                    f"child {child_id!r} waits for its own stages"
                )
            if child.status not in {"pending", "starting"}:
                raise StateError(f"child {child_id!r} is not pending")
            start_operation_id = child.start_operation_id
            if start_operation_id is None:
                raise StateError(f"child {child_id!r} has no start operation")
            if any(
                other.id != child_id and other.status in {"starting", "in_progress"}
                for other in children
            ):
                raise StateError("another child is already in progress")
            workflow = (
                workflow_name
                if workflow_name is not None
                else (
                    child.workflow
                    if child.status == "starting"
                    else coordinator.workflow
                )
            )
            if child.status == "starting" and workflow != child.workflow:
                raise StateError(
                    f"child {child.id!r} is already starting; workflow cannot change"
                )
            child = self._launch_settings(
                child, parent, workflow_runtime, model, reasoning, agent
            )
            self.validate_workflow(workflow, child, parent)
            child = replace(child, workflow=workflow, status="starting")
            children[children.index(by_id[child_id])] = child
            self.lifecycle.commit(parent, snapshot, children=tuple(children))
        if is_bootstrap_request(child_id):
            # The child obtains its own external ID first; ``bind_child`` then
            # renames this record and publishes the child run.
            if self.start_identity is None:  # pragma: no cover - service wires it
                raise StateError("child identity requests are not supported here")
            return self.start_identity(child, workflow, parent)
        child_task_id = f"{parent_task_id}/{child_id}"
        existing, _, _ = self.tasks.read_task_record(child_task_id)
        published = next(
            (
                run
                for run in reversed(existing)
                if run.state.parent_task_id == parent_task_id
                and run.state.start_operation_id == child.start_operation_id
            ),
            None,
        )
        if published is not None:
            # A previous process published the child before dying while
            # publishing the parent projection.  Reconcile instead of
            # creating a duplicate run.
            self.reconcile_after_child(child_task_id)
            return self.lifecycle.instruction_status(child_task_id, published.run_id)
        if any(run_is_open(run.state.status) for run in existing):
            raise StateError("existing child run does not match its parent binding")
        with self.tasks.lock_task(child_task_id):
            child_instruction = self.start_run(
                child_task_id,
                workflow,
                (),
                child.agent or parent.agent,
                parent_task_id=parent_task_id,
                start_operation_id=start_operation_id,
                model=child.model or parent.model,
                reasoning=child.reasoning or parent.reasoning,
                workflow_runtime=child.workflow_runtime or parent.workflow_runtime,
                init_artifact=(
                    f"Requirements for child task {child_id}: {child.description}"
                ),
                project=child.project,
            )
        self._publish_child(parent_task_id, child_id, child_task_id, workflow)
        return child_instruction

    @staticmethod
    def _launch_settings(
        child: ChildTask,
        parent: ExecutionState,
        workflow_runtime: str | None,
        model: str | None,
        reasoning: str | None,
        agent: str | None,
    ) -> ChildTask:
        """Resolve and freeze settings before any child launch side effect."""
        inherited_model = child.model or parent.model
        inherited_reasoning = child.reasoning or parent.reasoning
        inherited_runtime = child.workflow_runtime or parent.workflow_runtime
        resolved_model = model if model is not None else inherited_model
        resolved_reasoning = (
            reasoning
            if reasoning is not None
            else "auto"
            if resolved_model != inherited_model
            else inherited_reasoning
        )
        resolved_runtime = (
            workflow_runtime if workflow_runtime is not None else inherited_runtime
        )
        inherited_agent = child.agent or parent.agent
        resolved_agent = agent if agent is not None else inherited_agent
        if not resolved_model.strip() or not resolved_reasoning.strip():
            raise StateError("execution requires non-empty --model and --reasoning")
        if not resolved_agent.strip():
            raise StateError("execution requires a non-empty --agent")
        runtime_instruction(resolved_runtime)
        if child.status == "starting" and (
            resolved_model != inherited_model
            or resolved_reasoning != inherited_reasoning
            or resolved_runtime != inherited_runtime
            or resolved_agent != inherited_agent
        ):
            raise StateError(
                f"child {child.id!r} is already starting; launch settings cannot change"
            )
        return replace(
            child,
            model=resolved_model,
            reasoning=resolved_reasoning,
            workflow_runtime=resolved_runtime,
            agent=resolved_agent,
        )

    def release_child(self, parent_task_id: str, child_id: str) -> None:
        """Return a child whose identity request was reset to ``pending``.

        The record keeps its start operation and frozen launch settings, so
        ``start-child`` opens a fresh request for it as it did the first time.
        """
        with self.tasks.lock_task(parent_task_id):
            parent, snapshot = self.lifecycle.load(parent_task_id)
            children = list(self.tasks.read_children(parent_task_id, parent.run_id))
            index = next(
                (
                    i
                    for i, entry in enumerate(children)
                    if entry.id == child_id and entry.status == "starting"
                ),
                None,
            )
            if index is None:
                return
            children[index] = replace(children[index], status="pending")
            self.lifecycle.commit(parent, snapshot, children=tuple(children))

    def bind_child(
        self, parent_task_id: str, temporary_id: str, child_task_id: str
    ) -> None:
        """Rename a child from its identity request to its bound external ID.

        Safe to repeat: once the record carries the bound ID nothing changes.
        """
        _, _, bound_id = child_task_id.rpartition("/")
        with self.tasks.lock_task(parent_task_id):
            parent, snapshot = self.lifecycle.load(parent_task_id)
            children = list(self.tasks.read_children(parent_task_id, parent.run_id))
            index = next(
                (i for i, entry in enumerate(children) if entry.id == temporary_id),
                None,
            )
            if index is None:
                if not any(entry.id == bound_id for entry in children):
                    raise StateError(
                        f"child {temporary_id!r} was not found under {parent_task_id!r}"
                    )
                return
            if any(entry.id == bound_id for entry in children):
                raise StateError(f"child {bound_id!r} already exists")
            children[index] = replace(
                children[index], id=bound_id, task_id=child_task_id
            )
            self.lifecycle.commit(parent, snapshot, children=tuple(children))
        self._publish_child(
            parent_task_id, bound_id, child_task_id, children[index].workflow
        )

    def _publish_child(
        self, parent_task_id: str, child_id: str, child_task_id: str, workflow: str
    ) -> None:
        """Project the started child's run onto the parent's child record."""
        child_runs = self.tasks.execution_runs(child_task_id)
        child_run = child_runs[-1] if child_runs else None
        child_status = (
            child_run.status
            if child_run and child_run.status in {"completed", "failed"}
            else "in_progress"
        )
        with self.tasks.lock_task(parent_task_id):
            parent, snapshot = self.lifecycle.load(parent_task_id)
            children = list(self.tasks.read_children(parent_task_id, parent.run_id))
            for index, entry in enumerate(children):
                if entry.id == child_id:
                    children[index] = replace(
                        entry,
                        workflow=workflow,
                        status=child_status,
                        run_id=child_run.run_id if child_run else None,
                        summary=child_run.summary if child_run else None,
                    )
                    break
            self.lifecycle.commit(parent, snapshot, children=tuple(children))
        if child_status in {"completed", "failed"}:
            # The child may have drained to a terminal state during start (an
            # all-automatic workflow).  Reconcile after the parent relink so
            # the terminal state is not overwritten by the in-progress binding.
            self.reconcile_after_child(child_task_id)

    def refresh_parent(self, task_id: str) -> Instruction | None:
        """Refresh a waiting parent from authoritative child run records."""
        validate_task_id(task_id)
        if "/" in task_id:
            return None
        runs, _, _ = self.tasks.read_task_record(task_id)
        run = next(
            (entry for entry in reversed(runs) if entry.state.status != "completed"),
            runs[-1] if runs else None,
        )
        if (
            run is None
            or run.state.cursor >= len(run.snapshot.plan.items)
            or child_workflow(run.snapshot.plan.items[run.state.cursor]) is None
            or not run.children
        ):
            return None
        self.reconcile_after_child(f"{task_id}/{run.children[0].id}")
        refreshed, _, _ = self.tasks.read_task_record(task_id)
        current = next(
            (entry for entry in refreshed if entry.run_id == run.run_id), None
        )
        # A parent that was already failed is left to the command's own
        # failure handling, so ``next --retry`` can reach recovery.
        if (
            current is not None
            and current.state.status in {"completed", "failed"}
            and current.state.status != run.state.status
        ):
            return self.lifecycle.render(current.state, current.snapshot)
        return None

    def reconcile_after_child(self, child_task_id: str) -> None:
        """Project a child's run records onto its parent and advance the parent."""
        parent_task_id, separator, child_id = child_task_id.rpartition("/")
        if not separator:
            return
        with self.tasks.lock_task(parent_task_id):
            parent, snapshot = self.lifecycle.load(parent_task_id)
            children = list(self.tasks.read_children(parent_task_id, parent.run_id))
            if not any(child.id == child_id for child in children):
                return
            changed = False
            for index, child in enumerate(children):
                refreshed = self._refresh_child(parent_task_id, child)
                if refreshed is None:
                    continue
                changed = changed or refreshed != child
                children[index] = refreshed
            projected = tuple(children)

            def commit_children() -> None:
                self.lifecycle.commit(parent, snapshot, children=projected)

            if parent.cursor >= len(snapshot.plan.items):
                if changed:
                    commit_children()
                return
            item = snapshot.plan.items[parent.cursor]
            if child_workflow(item) is None:
                if changed:
                    commit_children()
                return
            # A per-child coordinator waits for its own child only.
            watched = (
                (projected[item.child_number - 1],)
                if item.child_number is not None
                else projected
            )
            failed = next(
                (child for child in watched if child.status == "failed"), None
            )
            if failed is not None:
                if parent.status != "failed":
                    parent = fail_child_workflow(
                        parent, snapshot.plan, item, failed.id, self.now
                    )
                    commit_children()
                elif changed:
                    commit_children()
                return
            if not watched or any(child.status != "completed" for child in watched):
                # A recovery retry may find a published child whose parent
                # relink was interrupted while the child was still pending.
                # Persist the in-progress binding while the parent keeps waiting.
                # A child ww could not launch has no run: it stays failed.
                if parent.status == "failed" and any(
                    child.run_id is not None for child in watched
                ):
                    parent = retry_failed_item(parent, snapshot.plan, self.now)
                    parent = begin_child_workflow(parent, item, self.now)
                    commit_children()
                elif changed:
                    commit_children()
                return
            parent = complete_child_workflow(
                parent,
                snapshot.plan,
                self.now,
                artifact=(
                    self._write_child_result(parent, snapshot, item, watched[0])
                    if item.child_number is not None
                    else None
                ),
            )
            commit_children()
            parent, snapshot = self.lifecycle.drain(parent, snapshot)
            if (
                parent.cursor < len(snapshot.plan.items)
                and snapshot.plan.items[parent.cursor].summary
            ):
                summary = "Completed child tasks: " + ", ".join(
                    child.summary or child.id
                    for child in projected
                    if child.status == "completed"
                )
                parent = complete_child_summary(parent, summary, self.now)
                self.lifecycle.commit(parent, snapshot, children=projected)
                self.lifecycle.drain(parent, snapshot)

    def _write_child_result(
        self,
        parent: ExecutionState,
        snapshot: PlanSnapshot,
        item: PlanItem,
        child: ChildTask,
    ) -> str | None:
        """Save a per-child run's result, the child's summary, as its artifact.

        Later stages read it like any step's artifact (``artifact_from``).
        """
        return write_completion_artifacts(
            self.tasks,
            parent.task_id,
            parent,
            snapshot,
            item,
            f"Child task `{child.task_id}` completed its `{child.workflow}` "
            f"workflow.\n\nSummary: {child.summary or 'none recorded'}",
        )

    def _refresh_child(self, parent_task_id: str, child: ChildTask) -> ChildTask | None:
        """Return the child record as its own run records describe it."""
        runs, _, _ = self.tasks.read_task_record(
            child.task_id or f"{parent_task_id}/{child.id}"
        )
        run = next(
            (
                entry
                for entry in reversed(runs)
                if entry.state.parent_task_id == parent_task_id
                and entry.state.start_operation_id == child.start_operation_id
            ),
            None,
        )
        if run is None:
            return None
        status: ChildStatus
        if run.state.status == "completed":
            status = "completed"
        elif run.state.status == "failed":
            status = "failed"
        else:
            status = "in_progress"
        return replace(
            child,
            status=status,
            run_id=run.run_id,
            summary=(
                dict(run.state.workflow_values).get("summary")
                if run.state.status == "completed"
                else None
            ),
        )
