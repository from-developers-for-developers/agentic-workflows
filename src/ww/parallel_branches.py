# SPDX-License-Identifier: GPL-3.0-or-later
"""The branches of a ``parallel`` step: their artifacts and the step's gate.

A parallel step is one plan item; its performer runs every branch at once,
one subagent each.  A branch's worker records the branch's artifact against
the step in progress, and the step completes only once every branch has,
so the run keeps a single current item throughout.
"""

from __future__ import annotations

from dataclasses import replace

from ww.artifacts import render_step_artifact
from ww.errors import StateError
from ww.execution_models import ExecutionState, PlanItemExecution, PlanSnapshot
from ww.plan import ParallelBranch, PlanItem, step_label
from ww.storage_adapters.base import ArtifactAddress, TaskArtifactStorage


def active_group(
    state: ExecutionState, snapshot: PlanSnapshot
) -> tuple[PlanItem, PlanItemExecution]:
    """The parallel step in progress, or a refusal saying there is none."""
    plan = snapshot.plan
    item = plan.items[state.cursor] if state.cursor < len(plan.items) else None
    record = (
        state.item_executions[state.cursor]
        if state.cursor < len(state.item_executions)
        else None
    )
    if (
        item is None
        or record is None
        or not item.parallel_branches
        or state.active_item_id != item.id
        or record.status != "in_progress"
    ):
        raise StateError(
            f"no parallel step of task {state.task_id!r} is in progress; "
            "--branch addresses a branch of the parallel step being worked on"
        )
    return item, record


def find_branch(item: PlanItem, name: str) -> tuple[int, ParallelBranch]:
    """The branch ``name`` of ``item`` and its one-based declaration ordinal."""
    for ordinal, branch in enumerate(item.parallel_branches, start=1):
        if branch.name == name:
            return ordinal, branch
    raise StateError(
        f"parallel step {item.step!r} has no branch {name!r}; its branches are "
        + ", ".join(repr(branch.name) for branch in item.parallel_branches)
    )


def missing_branches(item: PlanItem, record: PlanItemExecution) -> tuple[str, ...]:
    """The branches of ``item`` that have not recorded their artifact yet."""
    recorded = dict(record.branch_artifacts)
    return tuple(
        branch.name for branch in item.parallel_branches if branch.name not in recorded
    )


def require_branches(item: PlanItem, record: PlanItemExecution) -> None:
    """Refuse to complete a parallel step before every branch recorded."""
    missing = missing_branches(item, record)
    if missing:
        raise StateError(
            f"{item.name!r} is parallel and waits for the artifact of "
            + ", ".join(f"branch {name!r}" for name in missing)
            + "; launch a subagent for each with its branch page, "
            "`instruction --role worker --branch <name>`, and complete once "
            "every branch has recorded its artifact"
        )


def write_branch_artifact(
    storage: TaskArtifactStorage,
    state: ExecutionState,
    snapshot: PlanSnapshot,
    item: PlanItem,
    name: str,
    artifact: str,
) -> str:
    """Write branch ``name``'s artifact beside its step's; return the reference.

    It lives in the step's directory, numbered by the branch's declaration
    ordinal, and keeps the step's round, so a later round's stays apart.
    """
    ordinal, branch = find_branch(item, name)
    record = state.item_executions[state.cursor]
    ordinals = (*item.step_ordinals, ordinal)
    number, total = step_label(ordinals, snapshot.plan.items)
    return storage.write_execution_artifact(
        ArtifactAddress(
            state.task_id,
            state.workflow,
            f"{item.step}/{branch.name}",
            item.position,
            branch.name,
            "step",
            run_id=state.run_id,
            step_ordinals=ordinals,
            goto_round=record.goto_round,
        ),
        render_step_artifact(
            task_id=state.task_id,
            workflow=state.workflow,
            step=f"{item.step}/{branch.name}",
            step_number=number,
            step_total=total,
            skill="parallel branch",
            result=artifact,
        ),
    )


def record_branch(state: ExecutionState, name: str, reference: str) -> ExecutionState:
    """``state`` with branch ``name``'s artifact recorded on the current item."""
    records = list(state.item_executions)
    record = records[state.cursor]
    branches = dict(record.branch_artifacts)
    branches[name] = reference
    records[state.cursor] = replace(record, branch_artifacts=tuple(branches.items()))
    return replace(state, item_executions=tuple(records))
