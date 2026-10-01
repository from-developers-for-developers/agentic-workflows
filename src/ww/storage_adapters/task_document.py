# SPDX-License-Identifier: GPL-3.0-or-later
"""Compact on-disk codec for the authoritative task state document."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import MISSING, fields
from typing import Any

from ww.children import ChildTask
from ww.contracts import run_is_open
from ww.errors import ConfigurationError
from ww.execution_models import (
    CommandExecution,
    ExecutionState,
    PlanItemExecution,
    StepProgress,
    TaskRunAggregate,
)
from ww.extensions import parse_reference
from ww.items import WorkItem
from ww.plan import PlanItem
from ww.validation import is_strict_int

TASK_STATE_FORMAT = "ww.task-state"
TASK_STATE_SCHEMA_VERSION = 1


def _serialized_defaults(cls: type, **overrides: object) -> dict[str, object]:
    """Return the serialized default of every defaulted field of a record.

    The compact document omits a field whose value equals this default and
    restores it on read, so both directions derive from the same table and
    cannot drift from the dataclass.  Tuples serialize as lists; fields whose
    serialized form differs from their Python default are named explicitly.
    """
    defaults: dict[str, object] = {}
    for field in fields(cls):
        if field.default is not MISSING:
            value: object = field.default
        elif field.default_factory is not MISSING:
            value = field.default_factory()
        else:
            continue
        defaults[field.name] = list(value) if isinstance(value, tuple) else value
    defaults.update(overrides)
    return defaults


_RUN_DEFAULTS = _serialized_defaults(TaskRunAggregate)
_PLAN_ITEM_DEFAULTS = _serialized_defaults(PlanItem)
_ITEM_EXECUTION_DEFAULTS = _serialized_defaults(
    PlanItemExecution, supplied_values={}, output_values={}
)
_COMMAND_DEFAULTS = _serialized_defaults(CommandExecution)
_STEP_DEFAULTS = _serialized_defaults(StepProgress)
# ``modes`` and ``active_item_id`` have no dataclass default, yet the writer
# has always omitted an empty mode list and a missing active item.
_STATE_DEFAULTS = _serialized_defaults(
    ExecutionState,
    modes=[],
    active_item_id=None,
    workflow_values={},
    loop_iterations={},
    pending_task_metadata={},
)
_WORK_ITEM_DEFAULTS = _serialized_defaults(WorkItem)
_CHILD_DEFAULTS = _serialized_defaults(ChildTask, fields={})
_LEDGER_EVENT_DEFAULTS: dict[str, object] = {"summary": None}

_EXTENSION_SNAPSHOT_FIELDS = (
    "version",
    "api_version",
    "source",
    "fingerprint",
    "settings",
)


def encode_task_document(
    task_id: str,
    runs: tuple[TaskRunAggregate, ...],
    handoff: str | None,
    revision: int,
    ledger: dict[str, list[dict[str, object]]],
) -> dict[str, object]:
    """Encode expanded domain records without mutating them."""
    snapshots: dict[str, dict[str, object]] = {}
    encoded_runs = [
        _compact_run(copy.deepcopy(run.to_dict()), snapshots) for run in runs
    ]
    active = [run.run_id for run in runs if run_is_open(run.state.status)]
    if len(active) > 1:
        raise ValueError("task state has multiple active runs")
    result: dict[str, object] = {
        "format": TASK_STATE_FORMAT,
        "schema_version": TASK_STATE_SCHEMA_VERSION,
        "task_id": task_id,
        "revision": revision,
        "active_run": active[0] if active else None,
        "runs": encoded_runs,
    }
    if snapshots:
        result["extension_snapshots"] = snapshots
    if handoff is not None:
        result["handoff"] = handoff
    if ledger:
        result["ledger"] = _compact_ledger(copy.deepcopy(ledger))
    return result


def decode_task_document(
    data: object, task_id: str
) -> tuple[
    tuple[TaskRunAggregate, ...],
    str | None,
    int,
    dict[str, list[dict[str, object]]],
]:
    """Expand a compact task document and invoke the strict domain decoders."""
    if not isinstance(data, dict):
        raise ValueError("task state must be a mapping")
    if data.get("format") != TASK_STATE_FORMAT:
        raise ValueError(f"unsupported task state format: {data.get('format')!r}")
    version = data.get("schema_version")
    if not is_strict_int(version) or version != TASK_STATE_SCHEMA_VERSION:
        raise ValueError("unsupported task state schema")
    if data.get("task_id") != task_id:
        raise ValueError("task state task ID does not match its path")
    revision = data.get("revision")
    if not is_strict_int(revision) or revision < 0:
        raise ValueError("task state revision must be non-negative")
    if "active_run" not in data:
        raise ValueError("task state missing field: active_run")
    active_run = data["active_run"]
    if active_run is not None and not isinstance(active_run, str):
        raise ValueError("task state active_run must be a string or null")
    raw_runs = data.get("runs")
    if not isinstance(raw_runs, list):
        raise ValueError("task state runs must be a list")
    raw_snapshots = data.get("extension_snapshots", {})
    snapshots = _validate_extension_snapshots(raw_snapshots)
    runs = tuple(
        TaskRunAggregate.from_dict(_expand_run(copy.deepcopy(run), snapshots))
        for run in raw_runs
    )
    actual_active = [run.run_id for run in runs if run_is_open(run.state.status)]
    if len(actual_active) > 1:
        raise ValueError("task state has multiple active runs")
    expected_active = actual_active[0] if actual_active else None
    if active_run != expected_active:
        raise ValueError("task state active_run does not match the non-completed run")
    handoff = data.get("handoff")
    if handoff is not None and not isinstance(handoff, str):
        raise ValueError("task state handoff must be a string or null")
    ledger = data.get("ledger", {})
    if not isinstance(ledger, dict):
        raise ValueError("task state ledger must be a mapping")
    return runs, handoff, revision, _expand_ledger(copy.deepcopy(ledger))


def _compact_run(
    run: dict[str, Any], snapshots: dict[str, dict[str, object]]
) -> dict[str, Any]:
    snapshot = run["snapshot"]
    _compact_snapshot(snapshot, snapshots)
    _compact_state(run["state"])
    for item in run.get("items", []):
        _omit_defaults(item, _WORK_ITEM_DEFAULTS)
    for child in run.get("children", []):
        _omit_defaults(child, _CHILD_DEFAULTS)
    _omit_defaults(run, _RUN_DEFAULTS)
    return run


def _compact_snapshot(
    snapshot: dict[str, Any], snapshots: dict[str, dict[str, object]]
) -> None:
    plan = snapshot["plan"]
    template = snapshot.get("template_plan")
    if template == plan:
        snapshot.pop("template_plan", None)
    _compact_plan(plan, snapshots)
    if "template_plan" in snapshot:
        _compact_plan(snapshot["template_plan"], snapshots)


def _compact_plan(
    plan: dict[str, Any], snapshots: dict[str, dict[str, object]]
) -> None:
    for item in plan["items"]:
        operation = item["operation"]
        if operation["type"] == "action" and operation["identifier"] == "extension":
            payload = operation["payload"]
            reference = payload.get("reference")
            if not isinstance(reference, str):
                raise ValueError("extension plan item requires a reference")
            identifier = _extension_identifier(reference)
            entry: dict[str, object] = {"identifier": identifier}
            for field in _EXTENSION_SNAPSHOT_FIELDS:
                entry[field] = copy.deepcopy(payload.pop(field))
            snapshot_id = _snapshot_id(entry)
            existing = snapshots.get(snapshot_id)
            if existing is not None and existing != entry:
                raise ValueError("extension snapshot hash collision")
            snapshots[snapshot_id] = entry
            payload["snapshot"] = snapshot_id
        _omit_defaults(item, _PLAN_ITEM_DEFAULTS)


def _compact_state(state: dict[str, Any]) -> None:
    for item in [*state["item_executions"], *state["execution_history"]]:
        for command in item["commands"]:
            _omit_defaults(command, _COMMAND_DEFAULTS)
        _omit_defaults(item, _ITEM_EXECUTION_DEFAULTS)
    for step in state["steps"]:
        _compact_step(step)
    _omit_defaults(state, _STATE_DEFAULTS)


def _compact_step(step: dict[str, Any]) -> None:
    for child in step["children"]:
        _compact_step(child)
    _omit_defaults(step, _STEP_DEFAULTS)


def _expand_run(run: object, snapshots: dict[str, dict[str, object]]) -> dict[str, Any]:
    if not isinstance(run, dict):
        raise ValueError("run must be a mapping")
    _add_defaults(run, _RUN_DEFAULTS)
    snapshot = run.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("run.snapshot must be a mapping")
    _expand_snapshot(snapshot, snapshots)
    state = run.get("state")
    if not isinstance(state, dict):
        raise ValueError("run.state must be a mapping")
    _expand_state(state)
    for item in _list(run["items"], "run.items"):
        if not isinstance(item, dict):
            raise ValueError("run.items entries must be mappings")
        _add_defaults(item, _WORK_ITEM_DEFAULTS)
    for child in _list(run["children"], "run.children"):
        if not isinstance(child, dict):
            raise ValueError("run.children entries must be mappings")
        _add_defaults(child, _CHILD_DEFAULTS)
    return run


def _expand_snapshot(
    snapshot: dict[str, Any], snapshots: dict[str, dict[str, object]]
) -> None:
    plan = snapshot.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("plan snapshot.plan must be a mapping")
    _expand_plan(plan, snapshots)
    if "template_plan" not in snapshot:
        snapshot["template_plan"] = copy.deepcopy(plan)
    else:
        template = snapshot["template_plan"]
        if not isinstance(template, dict):
            raise ValueError("plan snapshot.template_plan must be a mapping")
        _expand_plan(template, snapshots)


def _expand_plan(plan: dict[str, Any], snapshots: dict[str, dict[str, object]]) -> None:
    for item in _list(plan.get("items"), "plan.items"):
        if not isinstance(item, dict):
            raise ValueError("plan.items entries must be mappings")
        _add_defaults(item, _PLAN_ITEM_DEFAULTS)
        operation = item.get("operation")
        if not isinstance(operation, dict):
            raise ValueError("plan item operation must be a mapping")
        if operation.get("type") != "action":
            continue
        payload = operation.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("plan action payload must be a mapping")
        # Snapshot indirection belongs exclusively to extension payloads.
        # Other registered actions may freely use a field named ``snapshot``.
        if operation.get("identifier") != "extension":
            continue
        inline = set(payload) & set(_EXTENSION_SNAPSHOT_FIELDS)
        has_snapshot = "snapshot" in payload
        snapshot_id = payload.pop("snapshot", None)
        if inline and has_snapshot:
            raise ValueError("extension item has conflicting inline snapshot fields")
        if not has_snapshot:
            raise ValueError("extension plan item missing extension_snapshot")
        if not isinstance(snapshot_id, str):
            raise ValueError("extension_snapshot must be a string")
        entry = snapshots.get(snapshot_id)
        if entry is None:
            raise ValueError(f"unknown extension snapshot: {snapshot_id!r}")
        reference = payload.get("reference")
        if not isinstance(reference, str):
            raise ValueError("extension plan item requires a reference")
        if _extension_identifier(reference) != entry["identifier"]:
            raise ValueError("extension snapshot identifier does not match reference")
        for field in _EXTENSION_SNAPSHOT_FIELDS:
            payload[field] = copy.deepcopy(entry[field])


def _expand_state(state: dict[str, Any]) -> None:
    _add_defaults(state, _STATE_DEFAULTS)
    for field in ("item_executions", "execution_history"):
        for item in _list(state.get(field), f"state.{field}"):
            if not isinstance(item, dict):
                raise ValueError(f"state.{field} entries must be mappings")
            _add_defaults(item, _ITEM_EXECUTION_DEFAULTS)
            for command in _list(item["commands"], f"{field}.commands"):
                if not isinstance(command, dict):
                    raise ValueError("command execution must be a mapping")
                _add_defaults(command, _COMMAND_DEFAULTS)
    for step in _list(state.get("steps"), "state.steps"):
        _expand_step(step)


def _expand_step(step: object) -> None:
    if not isinstance(step, dict):
        raise ValueError("step progress must be a mapping")
    _add_defaults(step, _STEP_DEFAULTS)
    for child in _list(step["children"], "step.children"):
        _expand_step(child)


def _validate_extension_snapshots(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict):
        raise ValueError("extension_snapshots must be a mapping")
    result: dict[str, dict[str, object]] = {}
    required = {
        "identifier",
        "version",
        "api_version",
        "source",
        "fingerprint",
        "settings",
    }
    for snapshot_id, raw in value.items():
        if not isinstance(snapshot_id, str) or not isinstance(raw, dict):
            raise ValueError("extension snapshot entries are invalid")
        if not required <= set(raw):
            raise ValueError("extension snapshot has invalid fields")
        if not isinstance(raw["identifier"], str):
            raise ValueError("extension snapshot identifier must be a string")
        for name in ("version", "source", "fingerprint"):
            if raw[name] is not None and not isinstance(raw[name], str):
                raise ValueError(f"extension snapshot {name} must be a string or null")
        if raw["api_version"] is not None and not is_strict_int(raw["api_version"]):
            raise ValueError(
                "extension snapshot api_version must be an integer or null"
            )
        if raw["settings"] is not None and not isinstance(raw["settings"], dict):
            raise ValueError("extension snapshot settings must be a mapping or null")
        entry = copy.deepcopy(raw)
        if _snapshot_id(entry) != snapshot_id:
            raise ValueError("extension snapshot content hash does not match its ID")
        result[snapshot_id] = entry
    return result


def _compact_ledger(
    ledger: dict[str, list[dict[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    for events in ledger.values():
        for event in events:
            _omit_defaults(event, _LEDGER_EVENT_DEFAULTS)
    return ledger


def _expand_ledger(value: dict[object, object]) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for run_id, events in value.items():
        if not isinstance(run_id, str) or not isinstance(events, list):
            raise ValueError("task state ledger entries are invalid")
        expanded: list[dict[str, object]] = []
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("task state ledger events must be mappings")
            _add_defaults(event, _LEDGER_EVENT_DEFAULTS)
            expanded.append(event)
        result[run_id] = expanded
    return result


def _snapshot_id(entry: dict[str, object]) -> str:
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extension_identifier(reference: str) -> str:
    try:
        return parse_reference(reference).identifier
    except ConfigurationError as error:
        raise ValueError(str(error)) from error


def _omit_defaults(mapping: dict[str, Any], defaults: dict[str, object]) -> None:
    for name, default in defaults.items():
        if name in mapping and mapping[name] == default:
            mapping.pop(name)


def _add_defaults(mapping: dict[str, Any], defaults: dict[str, object]) -> None:
    for name, default in defaults.items():
        if name not in mapping:
            mapping[name] = copy.deepcopy(default)


def _list(value: object, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list")
    return value
