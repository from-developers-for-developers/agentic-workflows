# SPDX-License-Identifier: GPL-3.0-or-later
"""Mid-run replanning: a running task takes a changed workflow definition.

A task freezes its compiled plan when it starts. When the configuration
changes afterwards, the manager's ``next`` compiles the task's workflow again
and compares the new template with the one the run started from, item by
item in plan order. The first item that differs is the *change point*; ww
stops for the operator (``operator_reason: plan_changed``), who either takes
the new definition from that item on (``next --replan``) or carries on with
the saved plan (``next --keep-plan``).

Replanning splices the plan: every item before the change point keeps its
record, the change point and everything after it are the newly compiled
items with fresh records. When the change point is an item that already ran,
the cursor moves back to it and the finished items from there on run again;
their earlier records move to the run's execution history, so artifacts and
streams stay readable. What the plan expanded at runtime is respected: a
verification item belongs to the step it verifies, and a change that reaches
per-item or per-child stages already expanded, or rewinds past a children
step whose child tasks exist, is refused with the reason.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, replace
from typing import Literal

from ww.control import child_workflow
from ww.execution_models import ExecutionState, PlanSnapshot
from ww.execution_models.construction import (
    build_step_projection,
    new_item_execution,
    operation_scope_for,
)
from ww.plan import PlanItem, WorkflowPlan
from ww.plan.models import number_step_paths
from ww.transitions import Clock, project_steps

ChangeKind = Literal["added", "removed", "changed"]
# Item fields that move with an item's place in the plan rather than its
# definition, so they never count as a change on their own.
_PLACEMENT_FIELDS = ("id", "position", "step_ordinals")
# A field's value is shown up to this many characters on each side.
_VALUE_LIMIT = 160


@dataclass(frozen=True)
class FieldChange:
    """One field of an item, before and after, as compact JSON."""

    name: str
    before: str
    after: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class ChangedItem:
    """A step or hook the new definition adds, removes, or defines anew."""

    kind: ChangeKind
    label: str
    fields: tuple[FieldChange, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "label": self.label,
            "fields": [field.to_dict() for field in self.fields],
        }


@dataclass(frozen=True)
class PlanChange:
    """How the current configuration's plan differs from the saved one.

    ``first`` is the change point in the template, ``splice`` the same place
    in the run's concrete plan. ``reruns`` names the finished items a replan
    would run again, which the operator confirms; ``refusal`` says why the
    change cannot be applied to this run, when it cannot.
    """

    configuration_digest: str
    template: WorkflowPlan
    first: int
    splice: int
    changes: tuple[ChangedItem, ...]
    reruns: tuple[str, ...] = ()
    refusal: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "changes": [change.to_dict() for change in self.changes],
            "reruns": list(self.reruns),
            "refusal": self.refusal,
        }


def plan_change(
    state: ExecutionState,
    snapshot: PlanSnapshot,
    template: WorkflowPlan,
    configuration_digest: str,
) -> PlanChange | None:
    """The change ``template`` makes to the run, or ``None`` when it makes none.

    ``template`` is the run's workflow compiled from the current
    configuration with the options the run started with.
    """
    old = snapshot.template_plan or snapshot.plan
    first = _first_difference(old.items, template.items)
    if first is None:
        if _plan_fields(old) == _plan_fields(template):
            return None
        # Only the workflow's own fields changed: they apply as they are.
        first = len(old.items)
    changes = _changes(old.items[first:], template.items[first:])
    if not changes and first == len(old.items):
        changes = (ChangedItem("changed", f"workflow `{template.workflow}`"),)
    splice, refusal = _splice(state, snapshot, old, first)
    reruns = (
        ()
        if refusal is not None
        else tuple(
            dict.fromkeys(
                _label(item)
                for item, record in zip(
                    snapshot.plan.items[splice : state.cursor],
                    state.item_executions[splice : state.cursor],
                    strict=True,
                )
                if record.status == "completed" and item.verifies is None
            )
        )
    )
    return PlanChange(
        configuration_digest, template, first, splice, changes, reruns, refusal
    )


def replan(
    state: ExecutionState, snapshot: PlanSnapshot, change: PlanChange, now: Clock
) -> tuple[ExecutionState, PlanSnapshot]:
    """Take the new definition from the change point on."""
    if change.refusal is not None:
        raise ValueError(change.refusal)
    old = snapshot.plan
    splice = change.splice
    kept = old.items[:splice]
    items = number_step_paths(
        tuple(
            replace(item, position=index)
            for index, item in enumerate(
                (*kept, *change.template.items[change.first :]), 1
            )
        )
    )
    plan = replace(change.template, items=items)
    revision = snapshot.plan_revision + 1
    scope = f"{operation_scope_for(state)}:replan-{revision}"
    records = (
        *(
            replace(record, position=index)
            for index, record in enumerate(state.item_executions[:splice], 1)
        ),
        *(new_item_execution(state.task_id, scope, item) for item in items[splice:]),
    )
    displaced = tuple(
        record
        for record in state.item_executions[splice:]
        if record.status != "pending"
    )
    revised = replace(
        snapshot,
        plan=plan,
        template_plan=change.template,
        plan_revision=revision,
        configuration_digest=change.configuration_digest,
        compiled_at=now(),
    )
    reaches_cursor = splice <= state.cursor
    state = replace(
        state,
        item_executions=records,
        execution_history=(*state.execution_history, *displaced),
        steps=build_step_projection(plan, state.steps),
        snapshot_digest=change.configuration_digest,
        plan_revision=revision,
        plan_digest=revised.plan_digest,
        updated_at=now(),
    )
    if reaches_cursor:
        # The current item is redefined, or the run rewinds to the change
        # point: whatever stopped or occupied it is gone with its record.
        state = replace(
            state,
            cursor=splice,
            status="pending",
            active_item_id=None,
            pending_input_request=None,
            last_error=None,
            failure_kind=None,
            assignment_item_id=None,
            assignment_token=None,
            assignment_model=None,
            assignment_reasoning=None,
            assignment_selected_agent=None,
            assignment_selected_model=None,
            assignment_selected_reasoning=None,
        )
    return project_steps(state, plan, now), revised


def keep_plan(
    state: ExecutionState, snapshot: PlanSnapshot, configuration_digest: str
) -> tuple[ExecutionState, PlanSnapshot]:
    """Carry on with the saved plan; this configuration is not offered again."""
    return (
        replace(state, snapshot_digest=configuration_digest),
        replace(snapshot, configuration_digest=configuration_digest),
    )


def _first_difference(
    old: tuple[PlanItem, ...], new: tuple[PlanItem, ...]
) -> int | None:
    for index, (before, after) in enumerate(zip(old, new, strict=False)):
        if _definition(before) != _definition(after):
            return index
    if len(old) != len(new):
        return min(len(old), len(new))
    return None


def _definition(item: PlanItem) -> dict[str, object]:
    data = item.to_dict()
    for name in _PLACEMENT_FIELDS:
        data.pop(name, None)
    return data


def _plan_fields(plan: WorkflowPlan) -> dict[str, object]:
    data = plan.to_dict()
    data.pop("items", None)
    return data


def _identity(item: PlanItem) -> tuple[object, ...]:
    """What makes two items the same step or hook across definitions."""
    return (item.workflow, item.step, item.parent, item.phase, item.name)


def _changes(
    old: tuple[PlanItem, ...], new: tuple[PlanItem, ...]
) -> tuple[ChangedItem, ...]:
    matcher = difflib.SequenceMatcher(
        a=[_identity(item) for item in old],
        b=[_identity(item) for item in new],
        autojunk=False,
    )
    changes: list[ChangedItem] = []
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        before, after = old[a0:a1], new[b0:b1]
        if tag == "equal":
            changes.extend(
                ChangedItem("changed", _label(b), _fields(a, b))
                for a, b in zip(before, after, strict=True)
                if _definition(a) != _definition(b)
            )
            continue
        changes.extend(ChangedItem("removed", _label(item)) for item in before)
        changes.extend(ChangedItem("added", _label(item)) for item in after)
    return tuple(changes)


def _fields(before: PlanItem, after: PlanItem) -> tuple[FieldChange, ...]:
    old, new = _definition(before), _definition(after)
    # A prompt's operation repeats its description, which is shown already.
    prompts = _is_prompt(old) and _is_prompt(new)
    return tuple(
        FieldChange(name, _shown(old.get(name)), _shown(new.get(name)))
        for name in dict.fromkeys((*old, *new))
        if old.get(name) != new.get(name) and not (prompts and name == "operation")
    )


def _is_prompt(definition: dict[str, object]) -> bool:
    operation = definition.get("operation")
    return isinstance(operation, dict) and operation.get("identifier") == "prompt"


def _shown(value: object) -> str:
    text = (
        "(none)"
        if value is None
        else json.dumps(value, sort_keys=True, separators=(",", ":"))
    )
    return text if len(text) <= _VALUE_LIMIT else text[: _VALUE_LIMIT - 1] + "…"


def _label(item: PlanItem) -> str:
    if item.phase == "step":
        return f"step `{item.name}`"
    return f"`{item.name}` ({item.phase.replace('_', ' ')} of `{item.step}`)"


def _splice(
    state: ExecutionState,
    snapshot: PlanSnapshot,
    template: WorkflowPlan,
    first: int,
) -> tuple[int, str | None]:
    """Where the change point falls in the concrete plan, or why it cannot."""
    order = {item.id: index for index, item in enumerate(template.items)}
    concrete = snapshot.plan.items
    present = {item.id for item in concrete}
    expanded = [
        item
        for item in template.items[first:]
        if item.id not in present and item.item_template
    ]
    if expanded:
        return len(concrete), (
            f"the change reaches the stages of {_label(expanded[0])}, which "
            "this run has already expanded for its items or children; finish "
            "the run with `--keep-plan`, or reset the task and start it again"
        )
    splice = len(concrete)
    for index, item in enumerate(concrete):
        origin = order.get(item.verifies.item_id if item.verifies else item.id)
        if origin is not None and origin >= first:
            splice = index
            break
    for item, record in zip(
        concrete[splice : state.cursor + 1],
        state.item_executions[splice : state.cursor + 1],
        strict=False,
    ):
        if child_workflow(item) is not None and record.status != "pending":
            return splice, (
                f"replanning would rerun {_label(item)}, whose child tasks "
                "already exist; finish the run with `--keep-plan`, or reset "
                "the task and start it again"
            )
    return splice, None


def _kept(item_id: str | None, kept: set[str]) -> str | None:
    return item_id if item_id in kept else None
