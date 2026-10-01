# SPDX-License-Identifier: GPL-3.0-or-later
"""Persisted, plan-driven workflow execution models.

These models deliberately contain no YAML or handler-resolution concepts.  A
task starts with a compiled :class:`WorkflowPlan` snapshot and progresses only
through the normalized plan items in that snapshot.
"""

from __future__ import annotations

from typing import Any

from ww.actions import CommandDefinition, PlannedAction, actions
from ww.plan import PlanItem, WorkflowPlan

from .records import CommandExecution, ExecutionState, PlanItemExecution, StepProgress
from .runs import PlanSnapshot


def initial_state(
    snapshot: PlanSnapshot,
    modes: tuple[str, ...],
    now: str,
    run_id: str,
    execution_instance_id: str | None = None,
    parent_task_id: str | None = None,
    start_operation_id: str | None = None,
    workflow_runtime: str = "single",
    model: str = "auto",
    reasoning: str = "auto",
) -> ExecutionState:
    plan = snapshot.plan
    operation_scope = _operation_scope(run_id, execution_instance_id)
    records = tuple(
        new_item_execution(plan.task_id or "", operation_scope, item)
        for item in plan.items
    )
    return ExecutionState(
        task_id=plan.task_id or "",
        workflow=plan.workflow,
        agent=plan.agent,
        modes=modes,
        status="pending",
        created_at=now,
        updated_at=now,
        snapshot_digest=snapshot.configuration_digest,
        cursor=0,
        active_item_id=None,
        item_executions=records,
        steps=build_step_projection(plan),
        run_id=run_id,
        execution_instance_id=execution_instance_id,
        plan_revision=snapshot.plan_revision,
        plan_digest=snapshot.plan_digest,
        parent_task_id=parent_task_id,
        start_operation_id=start_operation_id,
        workflow_runtime=workflow_runtime,
        model=model,
        reasoning=reasoning,
    )


def new_item_execution(
    task_id: str, operation_scope: str, item: PlanItem
) -> PlanItemExecution:
    """Create the complete execution ledger for one concrete plan item."""
    operation_id = f"{task_id}:{operation_scope}:{item.id}"
    return PlanItemExecution(
        plan_item_id=item.id,
        position=item.position,
        operation_id=operation_id,
        commands=tuple(
            CommandExecution(
                index=index,
                operation_id=f"{operation_id}:command:{index}",
            )
            for index, _ in enumerate(_commands_for(item), 1)
        ),
    )


def _commands_for(item: PlanItem) -> tuple[CommandDefinition, ...]:
    """Return tracked command segments when the registered action supports them."""
    if not isinstance(item.operation, PlannedAction):
        return ()
    implementation = actions.get(item.kind)
    traits = implementation.traits(item.payload_as(implementation.planned_type))
    return traits.command_segments or ()


def operation_scope_for(state: ExecutionState) -> str:
    """Return the durable idempotency namespace for an execution instance."""
    return _operation_scope(state.run_id or state.workflow, state.execution_instance_id)


def _operation_scope(run_id: str, execution_instance_id: str | None) -> str:
    return (
        f"{run_id}:{execution_instance_id}"
        if execution_instance_id is not None
        else run_id
    )


def _persisted_step_chain(item: PlanItem) -> tuple[str, ...]:
    """Return the hierarchy encoded by a plan item."""
    chain = (*item.ancestors, item.step)
    if item.parent and item.parent not in chain[:-1]:
        return item.parent, item.step
    return chain


def build_step_projection(
    plan: WorkflowPlan, previous: tuple[StepProgress, ...] = ()
) -> tuple[StepProgress, ...]:
    """Build the recursive tree, retaining progress for paths that still exist."""
    prior: dict[str, StepProgress] = {}

    def remember(entries: tuple[StepProgress, ...]) -> None:
        for entry in entries:
            prior[entry.path] = entry
            remember(entry.children)

    remember(previous)
    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in plan.items:
        chain = _persisted_step_chain(item)
        for index, path in enumerate(chain):
            parent = chain[index - 1] if index else None
            if path not in nodes:
                nodes[path] = {"parent": parent, "children": []}
                order.append(path)
            if parent and path not in nodes[parent]["children"]:
                nodes[parent]["children"].append(path)

    def node(path: str) -> StepProgress:
        info = nodes[path]
        existing = prior.get(path)
        return StepProgress(
            path.rsplit("/", 1)[-1],
            path,
            status=existing.status if existing is not None else "pending",
            started_at=existing.started_at if existing is not None else None,
            completed_at=existing.completed_at if existing is not None else None,
            children=tuple(node(child) for child in info["children"]),
        )

    return tuple(node(path) for path in order if nodes[path]["parent"] is None)
