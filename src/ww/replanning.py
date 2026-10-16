# SPDX-License-Identifier: GPL-3.0-or-later
"""Mid-run replanning: a running task takes a changed workflow definition.

A task freezes its compiled plan when it starts. When the configuration
changes afterwards, the manager's ``next`` compiles the task's workflow again
and compares the new template with the one the run started from, item by
item. Any item added, removed, moved or defined anew is a change: ww stops for
the operator (``operator_reason: plan_changed``), who either takes the new
definition (``next --replan``) or carries on with the saved plan
(``next --keep-plan``).

Replanning reruns only what the change touches. An item whose own definition
changed, and an added item, get fresh records; so does every later item of
the same step, of a step inside it, or of a step around it (its hooks, the
body after a changed start), since those finish the work the changed item
redoes. Every other item keeps its record wherever it now sits in the plan,
and a removed item drops. The cursor moves to the earliest item to (re)run;
the earlier records of items that run again move to the run's execution
history, so artifacts and streams stay readable. What the plan expanded at
runtime is respected: a verification item belongs to the step it verifies,
and a change to per-child stages already expanded, or a replan that reruns
or moves back past a children step whose child tasks exist, is refused with
the reason.

An extension's identity (version, source, fingerprint) and its settings are
not part of an item's definition: like ww's own code, an extension may be
fixed or reconfigured while a task is in flight. Whenever the run takes the
current configuration, with or without a plan change, its planned extension
items adopt them silently.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, replace
from typing import Literal

from ww.actions import Extension, PlannedAction
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

ChangeKind = Literal["added", "removed", "moved", "changed"]
# Item fields that move with an item's place in the plan rather than its
# definition, so they never count as a change on their own.
_PLACEMENT_FIELDS = ("id", "position", "step_ordinals")
# Extension payload fields the run adopts from the current extensions rather
# than comparing: what the extension is and how the project configures it.
_EXTENSION_ADOPTED = ("version", "api_version", "source", "fingerprint", "settings")
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
    """A step or hook the new definition adds, removes, moves or defines anew."""

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

    ``items`` is the run's concrete plan after a replan and ``sources`` the
    index of the record each item keeps, ``None`` for a fresh one; ``cursor``
    is where the run goes on. ``reruns`` names the finished items a replan
    would run again, which the operator confirms; ``refusal`` says why the
    change cannot be applied to this run, when it cannot.
    """

    configuration_digest: str
    template: WorkflowPlan
    changes: tuple[ChangedItem, ...]
    items: tuple[PlanItem, ...] = ()
    sources: tuple[int | None, ...] = ()
    cursor: int = 0
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
    if _same_definition(old, template):
        return None
    changes = _changes(old.items, template.items) or (
        ChangedItem("changed", f"workflow `{template.workflow}`"),
    )
    change = PlanChange(configuration_digest, template, changes)
    refusal = _expanded_refusal(snapshot, old, template)
    if refusal is not None:
        return replace(change, refusal=refusal)
    concrete = snapshot.plan.items
    records = state.item_executions
    entries = _entries(concrete, old, template)
    position = {item.id: index for index, item in enumerate(concrete)}
    items = tuple(item for item, _ in entries)
    sources = tuple(source for _, source in entries)
    cursor = min(
        (
            index
            for index, source in enumerate(sources)
            if source is None or source >= state.cursor
        ),
        default=len(items),
    )
    rewinds = _rewinds(state, sources, cursor)
    if rewinds:
        # Whatever stopped or occupied the run's current item is gone: an
        # unfinished record from the cursor on starts afresh.
        sources = tuple(
            None
            if source is not None
            and index >= cursor
            and records[source].status not in ("pending", "completed")
            else source
            for index, source in enumerate(sources)
        )
    # The record each fresh item replaces, where the old plan had the item.
    earlier = {
        index: position[item.id]
        for index, (item, source) in enumerate(zip(items, sources, strict=True))
        if source is None and item.id in position
    }
    for index, before in earlier.items():
        if child_workflow(items[index]) is not None and records[before].status != (
            "pending"
        ):
            return replace(
                change,
                refusal=(
                    f"replanning would rerun {_label(items[index])}, whose child "
                    "tasks already exist; finish the run with `--keep-plan`, or "
                    "reset the task and start it again"
                ),
            )
    if rewinds:
        for index in range(cursor, len(items)):
            source = sources[index]
            if (
                source is not None
                and child_workflow(items[index]) is not None
                and records[source].status != "pending"
            ):
                return replace(
                    change,
                    refusal=(
                        "replanning would move the run back before "
                        f"{_label(items[index])}, whose child tasks already "
                        "exist; finish the run with `--keep-plan`, or reset the "
                        "task and start it again"
                    ),
                )
    reruns = tuple(
        dict.fromkeys(
            _label(items[index])
            for index, before in sorted(earlier.items())
            if records[before].status == "completed" and items[index].verifies is None
        )
    )
    return replace(change, items=items, sources=sources, cursor=cursor, reruns=reruns)


def replan(
    state: ExecutionState, snapshot: PlanSnapshot, change: PlanChange, now: Clock
) -> tuple[ExecutionState, PlanSnapshot]:
    """Take the new definition: rerun what it changed, keep everything else."""
    if change.refusal is not None:
        raise ValueError(change.refusal)
    items = number_step_paths(
        tuple(
            replace(item, position=index) for index, item in enumerate(change.items, 1)
        )
    )
    plan = replace(change.template, items=items)
    revision = snapshot.plan_revision + 1
    scope = f"{operation_scope_for(state)}:replan-{revision}"
    records = tuple(
        new_item_execution(state.task_id, scope, item)
        if source is None
        else replace(state.item_executions[source], position=item.position)
        for item, source in zip(items, change.sources, strict=True)
    )
    kept = {source for source in change.sources if source is not None}
    displaced = tuple(
        record
        for index, record in enumerate(state.item_executions)
        if index not in kept and record.status != "pending"
    )
    revised = replace(
        snapshot,
        plan=plan,
        template_plan=change.template,
        plan_revision=revision,
        configuration_digest=change.configuration_digest,
        compiled_at=now(),
    )
    rewinds = _rewinds(state, change.sources, change.cursor)
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
    if rewinds:
        # The current item is redefined, or the run moves back to an item to
        # rerun: whatever stopped or occupied it is gone with its record.
        state = replace(
            state,
            cursor=change.cursor,
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
    else:
        state = replace(state, cursor=change.cursor)
    return project_steps(state, plan, now), revised


def keep_plan(
    state: ExecutionState,
    snapshot: PlanSnapshot,
    configuration_digest: str,
    template: WorkflowPlan,
) -> tuple[ExecutionState, PlanSnapshot]:
    """Carry on with the saved plan; this configuration is not offered again.

    The saved items still adopt the current extensions' identity and
    settings from ``template``, the workflow compiled from this
    configuration, so they run with the extensions as they are now.
    """
    old = snapshot.template_plan or snapshot.plan
    current = {item.id: item for item in template.items}
    origins = _origins(snapshot.plan.items, {item.id for item in old.items})
    plan = replace(
        snapshot.plan,
        items=tuple(
            _adopted(item, current.get(origin) if origin is not None else None)
            for item, origin in zip(snapshot.plan.items, origins, strict=True)
        ),
    )
    template_plan = replace(
        old, items=tuple(_adopted(item, current.get(item.id)) for item in old.items)
    )
    revised = replace(snapshot, configuration_digest=configuration_digest)
    state = replace(state, snapshot_digest=configuration_digest)
    if plan == snapshot.plan and template_plan == old:
        return state, revised
    revised = replace(
        revised,
        plan=plan,
        template_plan=template_plan,
        plan_revision=snapshot.plan_revision + 1,
    )
    return (
        replace(
            state,
            plan_revision=revised.plan_revision,
            plan_digest=revised.plan_digest,
        ),
        revised,
    )


def _same_definition(old: WorkflowPlan, new: WorkflowPlan) -> bool:
    return (
        _plan_fields(old) == _plan_fields(new)
        and [item.id for item in old.items] == [item.id for item in new.items]
        and all(
            _definition(before) == _definition(after)
            for before, after in zip(old.items, new.items, strict=True)
        )
    )


def _definition(item: PlanItem) -> dict[str, object]:
    data = item.to_dict()
    for name in _PLACEMENT_FIELDS:
        data.pop(name, None)
    operation = data["operation"]
    if _extension(item) is not None and isinstance(operation, dict):
        payload = operation["payload"]
        assert isinstance(payload, dict)
        data["operation"] = {
            **operation,
            "payload": {
                name: value
                for name, value in payload.items()
                if name not in _EXTENSION_ADOPTED
            },
        }
    return data


def _extension(item: PlanItem) -> Extension | None:
    operation = item.operation
    if isinstance(operation, PlannedAction) and isinstance(
        operation.payload, Extension
    ):
        return operation.payload
    return None


def _adopted(item: PlanItem, current: PlanItem | None) -> PlanItem:
    """``item`` running the extension as ``current`` plans it now."""
    before = _extension(item)
    after = _extension(current) if current is not None else None
    if before is None or after is None or before.reference != after.reference:
        return item
    payload = replace(
        before,
        version=after.version,
        api_version=after.api_version,
        source=after.source,
        fingerprint=after.fingerprint,
        settings=after.settings,
    )
    if payload == before:
        return item
    assert isinstance(item.operation, PlannedAction)
    return replace(item, operation=PlannedAction(item.operation.identifier, payload))


def _plan_fields(plan: WorkflowPlan) -> dict[str, object]:
    data = plan.to_dict()
    data.pop("items", None)
    return data


def _generated(item: PlanItem) -> str | None:
    """The plan item a runtime-generated item belongs to, if it is one."""
    if item.verifies is not None:
        return item.verifies.item_id
    return item.verification_hook or item.repair_of


def _origins(
    concrete: tuple[PlanItem, ...], template_ids: set[str]
) -> list[str | None]:
    """The template item each concrete item comes from.

    A generated verification or repair item comes from the item it serves,
    and an expanded per-child stage from its template. An item of unknown
    origin goes with the item after it, as generated items sit before the
    step they serve.
    """
    by_id = {item.id: item for item in concrete}

    def origin(item: PlanItem) -> str | None:
        if item.id in template_ids:
            return item.id
        owner = _generated(item)
        if owner is not None:
            served = by_id.get(owner)
            if served is not None and served is not item:
                return origin(served)
            return owner if owner in template_ids else None
        if item.child_number is not None:
            base = item.id.removesuffix(f":child:{item.child_number}")
            if base in template_ids:
                return base
        return None

    origins = [origin(item) for item in concrete]
    following: str | None = None
    for index in reversed(range(len(origins))):
        if origins[index] is None:
            origins[index] = following
        else:
            following = origins[index]
    preceding: str | None = None
    for index, value in enumerate(origins):
        if value is None:
            origins[index] = preceding
        else:
            preceding = value
    return origins


def _expanded_stages(concrete: tuple[PlanItem, ...], old: WorkflowPlan) -> set[str]:
    """The per-child stages the run has already expanded for its children."""
    present = {item.id for item in concrete}
    return {
        item.child_stage
        for item in old.items
        if item.child_template
        and item.child_stage is not None
        and item.id not in present
    }


def _expanded_refusal(
    snapshot: PlanSnapshot, old: WorkflowPlan, template: WorkflowPlan
) -> str | None:
    """Why the change cannot reach the run's expanded per-child stages."""
    stages = _expanded_stages(snapshot.plan.items, old)

    def stage(plan: WorkflowPlan, name: str) -> list[PlanItem]:
        return [
            item
            for item in plan.items
            if item.child_template and item.child_stage == name
        ]

    for name in sorted(stages):
        before, after = stage(old, name), stage(template, name)
        if [item.id for item in before] == [item.id for item in after] and all(
            _definition(a) == _definition(b) for a, b in zip(before, after, strict=True)
        ):
            continue
        known = {item.id: item for item in before}
        reached = next(
            (
                item
                for item in after
                if item.id not in known
                or _definition(known[item.id]) != _definition(item)
            ),
            after[0] if after else before[0],
        )
        return (
            f"the change reaches the stages of {_label(reached)}, which "
            "this run has already expanded for its children; finish "
            "the run with `--keep-plan`, or reset the task and start it again"
        )
    return None


def _entries(
    concrete: tuple[PlanItem, ...], old: WorkflowPlan, template: WorkflowPlan
) -> list[tuple[PlanItem, int | None]]:
    """The concrete plan after a replan, each item with the record it keeps.

    Items follow the new template's order. An unchanged item keeps its
    concrete items (itself, or its expansion per child, with the generated
    items that serve them) and their records; a changed or added item is the
    newly compiled one with a fresh record. Then every later item of the
    same step, of a step inside it, or of a step around it starts afresh too,
    and generated items serving a fresh item drop: the work they finished is
    done again.
    """
    previous = {item.id: item for item in old.items}
    current = {item.id: item for item in template.items}
    origins = _origins(concrete, set(previous))
    groups: dict[str, list[int]] = {}
    for index, origin in enumerate(origins):
        if origin is not None:
            groups.setdefault(origin, []).append(index)
    stages = _expanded_stages(concrete, old)
    entries: list[tuple[PlanItem, int | None]] = []
    emitted: set[str] = set()
    for item in template.items:
        if item.child_template and item.child_stage in stages:
            if item.child_stage in emitted:
                continue
            emitted.add(item.child_stage)
            stage = {
                entry.id
                for entry in old.items
                if entry.child_template and entry.child_stage == item.child_stage
            }
            entries.extend(
                (_adopted(concrete[index], current.get(origin)), index)
                for index, origin in enumerate(origins)
                if origin in stage
            )
            continue
        before = previous.get(item.id)
        indexes = groups.get(item.id)
        if before is None or not indexes or _definition(before) != _definition(item):
            entries.append((item, None))
            continue
        entries.extend((_adopted(concrete[index], item), index) for index in indexes)
    fresh: list[tuple[str, str]] = []
    for position, (item, source) in enumerate(entries):
        if _generated(item) is not None:
            continue
        if source is None or any(
            _related((item.workflow, item.step), root) for root in fresh
        ):
            entries[position] = (item, None)
            fresh.append((item.workflow, item.step))
    renewed = {item.id for item, source in entries if source is None}
    by_id = {item.id: item for item, _ in entries}

    def serves_fresh(item: PlanItem) -> bool:
        owner = _generated(item)
        while owner is not None:
            if owner in renewed:
                return True
            served = by_id.get(owner)
            owner = _generated(served) if served is not None else None
        return False

    return [
        (item, source)
        for item, source in entries
        if not (source is not None and serves_fresh(item))
    ]


def _related(path: tuple[str, str], root: tuple[str, str]) -> bool:
    """Whether ``path`` is ``root``'s own step, inside it, or around it."""
    (workflow, step), (root_workflow, root_step) = path, root
    return workflow == root_workflow and (
        step == root_step
        or step.startswith(f"{root_step}/")
        or root_step.startswith(f"{step}/")
    )


def _rewinds(
    state: ExecutionState, sources: tuple[int | None, ...], cursor: int
) -> bool:
    """Whether the replan takes the run off the item it is on."""
    current = next(
        (
            index
            for index, source in enumerate(sources)
            if source is not None and source == state.cursor
        ),
        None,
    )
    return current is None or cursor < current


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
    opcodes = matcher.get_opcodes()
    removed = {
        item.id: item
        for tag, a0, a1, _, _ in opcodes
        if tag != "equal"
        for item in old[a0:a1]
    }
    added = {
        item.id
        for tag, _, _, b0, b1 in opcodes
        if tag != "equal"
        for item in new[b0:b1]
    }
    moved = removed.keys() & added
    changes: list[ChangedItem] = []
    for tag, a0, a1, b0, b1 in opcodes:
        before, after = old[a0:a1], new[b0:b1]
        if tag == "equal":
            changes.extend(
                ChangedItem("changed", _label(b), _fields(a, b))
                for a, b in zip(before, after, strict=True)
                if _definition(a) != _definition(b)
            )
            continue
        changes.extend(
            ChangedItem("removed", _label(item))
            for item in before
            if item.id not in moved
        )
        changes.extend(
            ChangedItem("moved", _label(item), _fields(removed[item.id], item))
            if item.id in moved
            else ChangedItem("added", _label(item))
            for item in after
        )
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
