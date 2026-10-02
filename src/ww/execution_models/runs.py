# SPDX-License-Identifier: GPL-3.0-or-later
"""Run aggregates, plan snapshots, and the invariants that tie them together."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from ww.actions import PlannedAction, actions
from ww.children import ChildTask
from ww.contracts import BOOTSTRAP_REQUEST_PREFIX, RunStatus, run_is_open
from ww.errors import StateError
from ww.items import WorkItem
from ww.plan import PlanItem, WorkflowPlan
from ww.validation import (
    expect_optional_string,
    expect_positive_int,
    expect_string,
    is_strict_int,
    require_keys,
)

from .decoding import _from_path
from .plan_codec import _plan_from_dict
from .records import ExecutionState

PLAN_SCHEMA_VERSION = 1
# Recorded on every snapshot; informational until a reader needs to branch on it.
PLAN_COMPILER_VERSION = "plan-v9"


@dataclass(frozen=True)
class WorkflowRunSummary:
    """Task-level projection of one independently persisted workflow run."""

    run_id: str
    workflow: str
    status: RunStatus
    summary: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class TaskRunAggregate:
    """Authoritative, atomically published state for one workflow run.

    ``plan`` and ``state`` are the source of truth.  Items and children are
    included in the same record because they participate in transitions (and
    are therefore not safe as independently committed side files).
    """

    run_id: str
    workflow: str
    snapshot: PlanSnapshot
    state: ExecutionState
    items: tuple[WorkItem, ...] = ()
    children: tuple[ChildTask, ...] = ()
    bootstrap_request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "snapshot": self.snapshot.to_dict(),
            "state": self.state.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "children": [child.to_dict() for child in self.children],
            "bootstrap_request_id": self.bootstrap_request_id,
        }

    @classmethod
    def from_dict(cls, data: Any) -> TaskRunAggregate:
        """Decode one run without coercing malformed persisted values."""
        if not isinstance(data, dict):
            raise ValueError("run must be a mapping")
        require_keys(data, {"run_id", "workflow", "snapshot", "state"}, "run")
        run_id = expect_string(data["run_id"], "run.run_id")
        raw_items = data.get("items", [])
        raw_children = data.get("children", [])
        if not isinstance(raw_items, list):
            raise ValueError("run.items must be a list")
        if not isinstance(raw_children, list):
            raise ValueError("run.children must be a list")
        snapshot = _from_path(PlanSnapshot.from_dict, data["snapshot"], "run.snapshot")
        state = _from_path(ExecutionState.from_dict, data["state"], "run.state")
        if state.plan_digest is not None:
            # The digest is derived from the plan stored beside it, and how a
            # plan serializes depends on the ww version that wrote it: a
            # field it knew and this one does not, or a default only one of
            # them fills in. Re-derive it from this version's reading of the
            # same plan, so state another version wrote keeps loading.
            state = replace(state, plan_digest=snapshot.plan_digest)
        return cls(
            run_id=run_id,
            workflow=expect_string(data["workflow"], "run.workflow"),
            snapshot=snapshot,
            state=state,
            items=tuple(
                _from_path(WorkItem.from_dict, item, f"run.items[{index}]")
                for index, item in enumerate(raw_items)
            ),
            children=tuple(
                _from_path(ChildTask.from_dict, child, f"run.children[{index}]")
                for index, child in enumerate(raw_children)
            ),
            bootstrap_request_id=expect_optional_string(
                data.get("bootstrap_request_id"), "run.bootstrap_request_id"
            ),
        )


@dataclass(frozen=True)
class PlanSnapshot:
    """A task-local plan revision plus its immutable compiled template."""

    schema_version: int
    compiler_version: str
    configuration_digest: str
    compiled_at: str
    plan: WorkflowPlan
    plan_revision: int = 1
    template_plan: WorkflowPlan | None = None
    # The step a bootstrap request already performed, which the plan omits;
    # kept so a replan compiles the workflow the way the run started.
    bootstrap_step: str | None = None

    def __post_init__(self) -> None:
        if self.template_plan is None:
            object.__setattr__(self, "template_plan", self.plan)

    @property
    def plan_digest(self) -> str:
        canonical = json.dumps(
            self.plan.to_dict(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        template = self.template_plan if self.template_plan is not None else self.plan
        return {
            "schema_version": self.schema_version,
            "compiler_version": self.compiler_version,
            "configuration_digest": self.configuration_digest,
            "compiled_at": self.compiled_at,
            "plan": self.plan.to_dict(),
            "plan_revision": self.plan_revision,
            "template_plan": template.to_dict(),
            **(
                {"bootstrap_step": self.bootstrap_step}
                if self.bootstrap_step is not None
                else {}
            ),
        }

    @classmethod
    def from_dict(cls, data: Any) -> PlanSnapshot:
        if not isinstance(data, dict):
            raise ValueError("plan snapshot must be a mapping")
        required = {
            "schema_version",
            "compiler_version",
            "configuration_digest",
            "compiled_at",
            "plan",
        }
        require_keys(data, required, "plan snapshot")
        schema_version = data["schema_version"]
        if not is_strict_int(schema_version) or schema_version != PLAN_SCHEMA_VERSION:
            raise ValueError(f"unsupported plan snapshot schema: {schema_version!r}")
        plan = _plan_from_dict(data["plan"])
        return cls(
            schema_version=schema_version,
            compiler_version=expect_string(
                data["compiler_version"], "compiler_version"
            ),
            configuration_digest=expect_string(
                data["configuration_digest"], "configuration_digest"
            ),
            compiled_at=expect_string(data["compiled_at"], "compiled_at"),
            plan=plan,
            plan_revision=expect_positive_int(
                data.get("plan_revision", 1), "plan snapshot.plan_revision"
            ),
            template_plan=(
                _plan_from_dict(data["template_plan"])
                if "template_plan" in data
                else plan
            ),
            bootstrap_step=expect_optional_string(
                data.get("bootstrap_step"), "plan snapshot.bootstrap_step"
            ),
        )


def validate_task_runs(task_id: str, runs: tuple[TaskRunAggregate, ...]) -> None:
    """Reject an aggregate whose runs, plans, and records disagree.

    Every storage adapter calls this on read and before commit, so the
    invariants live with the model rather than being copied per backend.
    """
    run_ids = [run.run_id for run in runs]
    if len(run_ids) != len(set(run_ids)):
        raise StateError(f"task {task_id!r} aggregate has duplicate run IDs")
    if sum(run_is_open(run.state.status) for run in runs) > 1:
        raise StateError(f"task {task_id!r} aggregate has multiple active runs")
    for run in runs:
        _validate_run(task_id, run)


def _validate_run(task_id: str, run: TaskRunAggregate) -> None:
    state, plan = run.state, run.snapshot.plan
    if state.task_id != task_id or state.run_id != run.run_id:
        raise StateError(f"task {task_id!r} aggregate has mismatched run identity")
    if plan.task_id != task_id:
        raise StateError(f"task {task_id!r} aggregate has mismatched plan task ID")
    binding = run.bootstrap_request_id
    if binding is not None and not binding.startswith(BOOTSTRAP_REQUEST_PREFIX):
        raise StateError(f"task {task_id!r} aggregate has invalid bootstrap binding")
    if state.workflow != run.workflow or plan.workflow != run.workflow:
        raise StateError(f"task {task_id!r} aggregate has mismatched workflow identity")
    if (
        state.agent != plan.agent
        or state.snapshot_digest != run.snapshot.configuration_digest
    ):
        raise StateError(f"task {task_id!r} aggregate has mismatched plan identity")
    if state.plan_revision != run.snapshot.plan_revision:
        raise StateError(f"task {task_id!r} aggregate has mismatched plan revision")
    if state.plan_digest is not None and state.plan_digest != run.snapshot.plan_digest:
        raise StateError(f"task {task_id!r} aggregate has mismatched plan digest")
    if len(state.item_executions) != len(plan.items):
        raise StateError(f"task {task_id!r} aggregate has mismatched plan/state")
    if not 0 <= state.cursor <= len(plan.items):
        raise StateError(f"task {task_id!r} aggregate has invalid cursor")
    item_ids = [item.id for item in plan.items]
    if len(item_ids) != len(set(item_ids)) or any(
        item.position != position for position, item in enumerate(plan.items, 1)
    ):
        raise StateError(f"task {task_id!r} aggregate has invalid plan identity")
    for item, record in zip(plan.items, state.item_executions, strict=True):
        if item.id != record.plan_item_id or item.position != record.position:
            raise StateError(f"task {task_id!r} aggregate has mismatched item identity")
        expected = _expected_command_count(item)
        if expected is not None and expected != len(record.commands):
            raise StateError(f"task {task_id!r} aggregate has mismatched commands")
        if any(
            command.index != index for index, command in enumerate(record.commands, 1)
        ):
            raise StateError(f"task {task_id!r} aggregate has invalid command index")
    if state.active_item_id is not None and (
        state.cursor >= len(plan.items)
        or plan.items[state.cursor].id != state.active_item_id
    ):
        raise StateError(f"task {task_id!r} aggregate has an invalid active item")


def _expected_command_count(item: PlanItem) -> int | None:
    """Return how many command records the item's action declares, if known."""
    if not isinstance(item.operation, PlannedAction) or not actions.contains(item.kind):
        return None
    implementation = actions.get(item.kind)
    traits = implementation.traits(item.payload_as(implementation.planned_type))
    return len(traits.command_segments or ())
