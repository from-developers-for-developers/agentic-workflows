# SPDX-License-Identifier: GPL-3.0-or-later
"""What an expanded ``items`` pass requires of its items when it ends.

A workflow has one item collection and any number of sequential ``items``
passes over it.  Each pass declares what its stages do with an item through
``item_phase``, and only that is required when the run leaves the pass: an
analyze stage needs the item's analysis, a resolve stage its actual solution
and ``resolved``, a report stage ``reported``, and a phase stage's declared
item saves their values.  The built-in ``handle-item`` stage keeps its whole
lifecycle: ``resolved`` and ``reported``.  A stage without ``item_phase``
has only its ordinary completion contract.  A stage that never ran, because
an assessment, a break, or a stop skipped it, requires nothing.  When the
pass ends with an assessment, it is left only once the outcome is chosen.

A linked item (``reference_to_id``) shares its canonical item's analysis and
solution, so a duplicate comment needs no duplicate fix, but it is reported,
and resolved, for its own source.
"""

from __future__ import annotations

from ww.execution_models import PlanItemExecution
from ww.items import WorkItem
from ww.plan import PlanItem, WorkflowPlan


def item_collection(plan: WorkflowPlan) -> PlanItem | None:
    """The first ``items`` declaration, which holds the collection's settings.

    Every pass works on the same collection; its ``persistent``,
    ``identity``, and ``unique`` settings are the first declaration's.  The
    shared validator rejects a later declaration that sets them differently.
    """
    return next(
        (
            item
            for item in plan.items
            if item.item_operation == "collect" and item.child_operation is None
        ),
        None,
    )


def is_pass_stage(item: PlanItem) -> bool:
    """Whether ``item`` is a concrete stage, hook, or verifier of an item pass."""
    return (
        item.item_pass is not None
        and item.item_id is not None
        and item.child_stage is None
        and not item.item_template
    )


def reports_item_on_completion(plan: WorkflowPlan, cursor: int) -> bool:
    """Whether completing ``plan.items[cursor]`` finishes an automatic report.

    A report stage's lifecycle is its step, its handler-group members and its
    completion hooks: the plan items of one item and pass with the report
    operation.  When ww runs any of them, the item is reported by the last one
    to complete, whoever owns that last item, so no agent bookkeeping is
    needed.  A report stage that ww runs none of is reported by its agent
    with ``update-item --reported=true``, as before.
    """
    current = plan.items[cursor]
    if current.item_operation != "report_item" or current.item_id is None:
        return False
    lifecycle = [
        other
        for other in plan.items
        if other.item_id == current.item_id
        and other.item_pass == current.item_pass
        and other.item_operation == "report_item"
    ]
    return lifecycle[-1].id == current.id and any(
        other.owner == "ww" for other in lifecycle
    )


def leaving_pass(plan: WorkflowPlan, cursor: int) -> str | None:
    """The pass whose expanded stages end right before ``cursor``, if any.

    Assessment outcomes inside a per-item stage carry the stage's item and
    pass, so they count as stages of the pass.  Every workflow without a
    handoff ends with its built-in summary, so the run never leaves a pass by
    reaching the end of the plan; a stopping outcome completes the run.
    """
    if not 0 < cursor < len(plan.items):
        return None
    previous, following = plan.items[cursor - 1], plan.items[cursor]
    if not is_pass_stage(previous) or (
        is_pass_stage(following) and following.item_pass == previous.item_pass
    ):
        return None
    return previous.item_pass


def pass_gate_failures(
    plan: WorkflowPlan,
    records: tuple[PlanItemExecution, ...],
    pass_id: str,
    items: tuple[WorkItem, ...],
) -> tuple[str, ...]:
    """What each item of ``pass_id`` still lacks for the stages that ran."""
    by_id = {item.id: item for item in items}
    failures: list[str] = []
    for stage, record in zip(plan.items, records, strict=True):
        if (
            not is_pass_stage(stage)
            or stage.item_pass != pass_id
            or stage.phase != "step"
            or stage.verifies is not None
            or stage.item_operation is None
            or record.status != "completed"
            or record.started_at is None
        ):
            continue
        item = by_id.get(str(stage.item_id))
        if item is None:
            failures.append(f"{stage.item_id} ({stage.name}): the item is gone")
            continue
        missing = _missing(stage, item, by_id)
        if missing:
            failures.append(f"{item.id} ({stage.name}): " + ", ".join(missing))
    return tuple(dict.fromkeys(failures))


def _missing(
    stage: PlanItem, item: WorkItem, items: dict[str, WorkItem]
) -> tuple[str, ...]:
    canonical = _canonical(item, items)
    missing: list[str] = []
    operation = stage.item_operation
    if operation == "process_item" and not (
        item.processed_item or canonical.processed_item
    ):
        missing.append("processed_item")
    if operation == "resolve_item":
        if not (item.actual_solution or canonical.actual_solution):
            missing.append("actual_solution")
        if not (item.resolved or canonical.resolved):
            missing.append("resolved=true")
    if operation == "report_item" and not item.reported:
        missing.append("reported=true")
    if operation == "handle_item":
        if not item.resolved:
            missing.append("resolved=true")
        if not item.reported:
            missing.append("reported=true")
    missing.extend(
        f"field {field.name}"
        for field in stage.update_item
        if not item.field(field.name)
    )
    return tuple(missing)


def _canonical(item: WorkItem, items: dict[str, WorkItem]) -> WorkItem:
    """The item a linked item refers to, following links; itself otherwise."""
    seen = {item.id}
    current = item
    while current.reference_to_id is not None:
        target = items.get(current.reference_to_id)
        if target is None or target.id in seen:
            break
        seen.add(target.id)
        current = target
    return current
