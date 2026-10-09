# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural worker-assignment boundaries for compiled workflow plans."""

from __future__ import annotations

from dataclasses import dataclass

from ww.control import is_coordinator, workflow_transition
from ww.errors import StateError
from ww.plan import PlanItem, WorkflowPlan
from ww.workflow_config import ProvidedVariable


@dataclass(frozen=True)
class Assignment:
    """One manager-dispatched unit of agent and automatic lifecycle work."""

    first_item_id: str
    start: int
    stop: int


def selection_item(plan: WorkflowPlan, assignment: Assignment) -> PlanItem | None:
    """Return the item whose request drives a worker selection.

    The main step wins; in a hook-only assignment an agent-owned hook wins
    over a handler that merely waits for values, because the worker is
    shaped for the work it performs, not for the value it types in.
    """
    items = plan.items[assignment.start : assignment.stop]
    for choose in (
        lambda item: item.phase == "step" and item.owner == "agent",
        lambda item: item.owner == "agent",
        lambda item: item.requires_agent_input,
    ):
        found = next((item for item in items if choose(item)), None)
        if found is not None:
            return found
    return None


def assignment_at(
    plan: WorkflowPlan, cursor: int, *, runtime: str
) -> Assignment | None:
    """Select adjacent work with matching ownership and worker settings."""
    if cursor >= len(plan.items):
        return None
    first = plan.items[cursor]
    if _coordinator(first):
        return None
    if first.verifies is not None:
        return Assignment(first.id, cursor, cursor + 1)
    spans_steps = runtime != "single"
    lineage = {first.step, *first.ancestors}
    worker = first if first.owner == "agent" else None
    stop = cursor + 1
    while stop < len(plan.items):
        item = plan.items[stop]
        if _coordinator(item) or item.verifies is not None:
            break
        if item.summary:
            stop += 1
            continue
        if item.phase in {"before_start_workflow", "before_start", "step"}:
            if item.step != first.step:
                if not spans_steps or not (
                    shares_item_span(first, item, worker=worker)
                ):
                    break
                lineage.update((item.step, *item.ancestors))
            if worker is None and item.owner == "agent":
                worker = item
            stop += 1
            continue
        # A hook the manager performs, such as ww's own debug assessment,
        # starts an assignment of its own, as a ``role: manager`` step does.
        if item.step not in lineage or (item.role == "manager") != (
            worker is not None and worker.role == "manager"
        ):
            break
        stop += 1
    return Assignment(first.id, cursor, stop)


def shares_item_span(
    first: PlanItem, item: PlanItem, *, worker: PlanItem | None
) -> bool:
    """Whether ``item`` continues the per-item assignment begun at ``first``."""
    if first.item_id is None or item.item_id is None:
        return False
    if first.item_pass != item.item_pass:
        return False
    if first.item_assignment != item.item_assignment:
        return False
    if item.item_assignment == "per_item" and first.item_id != item.item_id:
        return False
    if item.item_assignment == "per_step":
        return False
    return _same_worker(item, worker)


def _same_worker(item: PlanItem, worker: PlanItem | None) -> bool:
    """Whether ``item`` can be performed by the assignment's current worker.

    ``worker`` is the first agent-owned item of the assignment so far. A step
    that needs a different agent, model, reasoning, or profile, or that is
    reserved for the manager with ``role: manager``, starts a new
    assignment, because one running worker cannot change any of them.
    """
    if item.owner != "agent":
        # Automatic work inside the span drains within the assignment.
        return True
    if item.role == "manager" or (worker is not None and worker.role == "manager"):
        return False
    return worker is None or _worker_settings(worker) == _worker_settings(item)


def _worker_settings(item: PlanItem) -> tuple[str | None, ...]:
    return (
        item.requested_agent,
        item.requested_model,
        item.requested_reasoning,
        item.profile,
    )


@dataclass(frozen=True)
class ItemSpan:
    """Several per-item stages that one worker performs in one assignment."""

    item_assignment: str
    stages: tuple[PlanItem, ...]

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(str(stage.item_id) for stage in self.stages))

    @property
    def stage_names(self) -> tuple[str, ...]:
        """The stage names of one item, in order."""
        first = self.stages[0].item_id
        return tuple(
            dict.fromkeys(stage.name for stage in self.stages if stage.item_id == first)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "item_assignment": self.item_assignment,
            "item_ids": list(self.item_ids),
            "stages": list(self.stage_names),
        }


def item_span(plan: WorkflowPlan, assignment: Assignment) -> ItemSpan | None:
    """Return the per-item stages an assignment spans, when it spans several."""
    stages = tuple(
        item
        for item in plan.items[assignment.start : assignment.stop]
        if item.phase == "step" and item.owner == "agent" and item.item_id is not None
    )
    if len(stages) <= 1:
        return None
    return ItemSpan(stages[0].item_assignment, stages)


def input_only(plan: WorkflowPlan, assignment: Assignment) -> bool:
    """Whether an assignment holds no agent work, only values for handlers.

    Such a span, for example a commit hook that needs its message after a
    group, is not worth a worker: the manager has just read the
    outcome it would summarize and supplies the values itself.
    """
    span = plan.items[assignment.start : assignment.stop]
    # The built-in workflow summary trails the final assignment; it does not
    # make a value-only span worth a worker.
    return not any(item.owner == "agent" and not item.summary for item in span) and any(
        item.requires_agent_input for item in span
    )


def active_assignment(
    plan: WorkflowPlan, first_item_id: str | None, *, runtime: str
) -> Assignment | None:
    """Rebuild a persisted assignment after reload or plan materialization."""
    if first_item_id is None:
        return None
    for index, item in enumerate(plan.items):
        if item.id == first_item_id:
            return assignment_at(plan, index, runtime=runtime)
    return None


def completion_window_items(
    plan: WorkflowPlan, cursor: int, stop: int | None = None
) -> tuple[PlanItem, ...]:
    """Return the current item and the automatic items its completion feeds."""
    items = [plan.items[cursor]]
    for item in plan.items[cursor + 1 : stop]:
        if item.owner == "agent" or workflow_transition(item) is not None:
            break
        items.append(item)
    return tuple(items)


def completion_window(
    plan: WorkflowPlan, cursor: int, stop: int | None = None
) -> tuple[tuple[ProvidedVariable, ...], tuple[str, ...]]:
    """Return inputs required to complete the current assignment boundary."""
    items = completion_window_items(plan, cursor, stop)
    required: dict[str, ProvidedVariable] = {}
    sources: dict[str, str] = {}
    for item in items:
        for value in item.provide:
            source = f"{item.name} ({item.source}, {item.id})"
            if value.name in required and required[value.name] != value:
                raise StateError(
                    f"completion window has conflicting provided variable "
                    f"{value.name!r}: {sources[value.name]} and {source}"
                )
            required.setdefault(value.name, value)
            sources.setdefault(value.name, source)
    context = [item.name for item in items[1:]]
    return tuple(required.values()), tuple(context)


def _coordinator(item: PlanItem) -> bool:
    return is_coordinator(item)
