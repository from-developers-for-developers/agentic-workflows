# SPDX-License-Identifier: GPL-3.0-or-later
"""Collection ownership and completion predicates; never schedules work items."""

from __future__ import annotations

from ww.errors import StateError
from ww.execution_models import ExecutionState
from ww.items import WorkItem
from ww.plan import PlanItem, WorkflowPlan


def item_collection(plan: WorkflowPlan, context: str | None = None) -> PlanItem | None:
    return next(
        (
            item
            for item in plan.items
            if item.item_operation == "collect"
            and (context is None or item.item_context == context)
        ),
        None,
    )


def select_context(
    plan: WorkflowPlan, state: ExecutionState, context: str | None = None
) -> str:
    choices = tuple(
        dict.fromkeys(
            item.item_context
            for item in plan.items
            if item.item_operation == "collect" and not item.child_template
        )
    )
    if context is not None:
        if context not in choices:
            raise StateError(f"unknown items context {context!r}; choices: {choices}")
        return context
    if state.cursor < len(plan.items):
        current = plan.items[state.cursor].item_context
        if current is not None:
            return current
    if len(choices) == 1 and choices[0] is not None:
        return choices[0]
    raise StateError(f"select an items context with --context; choices: {choices}")


def missing_fields(
    items: tuple[WorkItem, ...], fields: tuple[str, ...]
) -> tuple[str, ...]:
    return tuple(
        f"{item.id}: field.{name}"
        for item in items
        for name in fields
        if not item.field(name) or not str(item.field(name)).strip()
    )


def collection_failures(
    plan: WorkflowPlan, state: ExecutionState, context: str, items: tuple[WorkItem, ...]
) -> tuple[str, ...]:
    owned = tuple(item for item in items if item.context == context)
    fields = tuple(
        dict.fromkeys(
            field.name
            for step, record in zip(plan.items, state.item_executions, strict=True)
            if step.item_context == context
            and record.status == "completed"
            and record.started_at is not None
            for field in step.update_item
        )
    )
    return (
        *tuple(f"{item.id}: unresolved" for item in owned if not item.resolved),
        *tuple(f"{item.id}: unreported" for item in owned if not item.reported),
        *missing_fields(owned, fields),
    )
