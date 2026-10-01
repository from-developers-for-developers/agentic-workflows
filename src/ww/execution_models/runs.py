# SPDX-License-Identifier: GPL-3.0-or-later
"""Run aggregates, plan snapshots, and the invariants that tie them together."""

from __future__ import annotations

import hashlib
import json
import re
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
from ww.variables import renamed_template_name
from ww.workflow_config import TASK_ID_TOKEN

from .decoding import _from_path, _renamed_assertion
from .plan_codec import _plan_from_dict
from .records import ExecutionState

PLAN_SCHEMA_VERSION = 18
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
        data = _migrate_snapshot(data)
        schema_version = data["schema_version"]
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
        )


def _plan_15_to_16(data: dict[str, Any]) -> dict[str, Any]:
    """Schema 16 gives agent items their ``modes``; a 15 plan's items have none.

    A missing item ``modes`` already reads as no modes, so only the version
    changes: a run started before modes reached the page shows none.
    """
    return {**data, "schema_version": 16}


def _plan_16_to_17(data: dict[str, Any]) -> dict[str, Any]:
    """Schema 17 adds per-child stages (``child_stage``, ``child_number``).

    Both are written only where they apply, and a 16 plan has none, so only
    the version changes.
    """
    return {**data, "schema_version": 17}


_ASSIGNMENTS_17 = {"all_items": "together", "per_iteration": "per_round"}
_PHASES_17 = {"before_in_progress": "before_start"}
_TEMPLATE_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")


def _renamed_templates(value: Any) -> Any:
    """``value`` with every old template name replaced by its ``ww.`` name.

    Extension settings are left as they are: they are the extension's own,
    and its ``upgrade_settings`` reads them.
    """
    if isinstance(value, str):
        return _TEMPLATE_TOKEN.sub(
            lambda match: "{{"
            + (renamed_template_name(match.group(1)) or match.group(1))
            + "}}",
            value,
        )
    if isinstance(value, list):
        return [_renamed_templates(item) for item in value]
    if isinstance(value, dict):
        return {
            key: item if key == "settings" else _renamed_templates(item)
            for key, item in value.items()
        }
    return value


def _plan_item_17(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    upgraded = dict(item)
    for key in ("item_assignment", "loop_assignment"):
        if upgraded.get(key) in _ASSIGNMENTS_17:
            upgraded[key] = _ASSIGNMENTS_17[upgraded[key]]
    if upgraded.get("phase") in _PHASES_17:
        upgraded["phase"] = _PHASES_17[upgraded["phase"]]
    # The template names an item reads, as the compiler listed them from its
    # text: renamed like the tokens, so a value they name is still found.
    if isinstance(upgraded.get("dependencies"), list):
        upgraded["dependencies"] = [
            renamed_template_name(name) or name if isinstance(name, str) else name
            for name in upgraded["dependencies"]
        ]
    operation = upgraded.get("operation")
    if (
        isinstance(operation, dict)
        and operation.get("identifier") == "cli"
        and isinstance(operation.get("payload"), dict)
        and "assert" in operation["payload"]
    ):
        payload = dict(operation["payload"])
        payload["assert"] = _renamed_assertion(payload["assert"])
        upgraded["operation"] = {**operation, "payload": payload}
    if isinstance(upgraded.get("checks"), list):
        upgraded["checks"] = [_check_17(check) for check in upgraded["checks"]]
    return upgraded


def _check_17(check: Any) -> Any:
    """A frozen check whose command's ``assert`` is a version 17 assertion."""
    if not isinstance(check, dict):
        return check
    command = check.get("command")
    if not isinstance(command, dict) or "assert" not in command:
        return check
    return {
        **check,
        "command": {**command, "assert": _renamed_assertion(command["assert"])},
    }


def _plan_17(plan: Any) -> Any:
    if not isinstance(plan, dict):
        return plan
    upgraded = _renamed_templates(plan)
    if isinstance(upgraded.get("items"), list):
        upgraded["items"] = [_plan_item_17(item) for item in upgraded["items"]]
    documents = upgraded.get("documents")
    if isinstance(documents, list):
        upgraded["documents"] = [
            {**document, "path": document["path"].replace("{task_id}", TASK_ID_TOKEN)}
            if isinstance(document, dict) and isinstance(document.get("path"), str)
            else document
            for document in documents
        ]
    return upgraded


def _plan_17_to_18(data: dict[str, Any]) -> dict[str, Any]:
    """Schema 18 applies the v1 renames to what a plan froze.

    Assignment values (``all_items`` is ``together``, ``per_iteration`` is
    ``per_round``), the ``before_in_progress`` phase (``before_start``),
    template names (every ww value under ``ww.``, in the text and in each
    item's ``dependencies``), a document path's
    ``{task_id}`` (``{{ww.task.id}}``), and a command's ``assert`` (a list of
    conditions).  Plan item IDs keep their old spelling: they are opaque, and
    the run's records refer to them.
    """
    upgraded = {**data, "schema_version": 18, "plan": _plan_17(data["plan"])}
    if "template_plan" in data:
        upgraded["template_plan"] = _plan_17(data["template_plan"])
    return upgraded


PLAN_MIGRATIONS = {15: _plan_15_to_16, 16: _plan_16_to_17, 17: _plan_17_to_18}


def _migrate_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a plan snapshot to the current schema, one version at a time."""
    while data["schema_version"] != PLAN_SCHEMA_VERSION:
        version = data["schema_version"]
        migrate = PLAN_MIGRATIONS.get(version) if is_strict_int(version) else None
        if migrate is None:
            raise ValueError(f"unsupported plan snapshot schema: {version!r}")
        data = migrate(data)
    return data


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
        raise StateError(
            f"task {task_id!r} aggregate has mismatched workflow identity"
        )
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
            command.index != index
            for index, command in enumerate(record.commands, 1)
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
