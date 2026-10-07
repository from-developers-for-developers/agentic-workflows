# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict decoding of work items, child records, and saved-state helpers."""

from __future__ import annotations

from typing import Any

import pytest

from tests.plan_helpers import plan_item
from ww.actions import CommandDefinition, Commands, PlannedAction
from ww.children import ChildTask
from ww.execution_models import (
    PLAN_SCHEMA_VERSION,
    CommandExecution,
    InputRequest,
    PlanSnapshot,
)
from ww.execution_models.decoding import _from_path, _positive_int_mapping, _variables
from ww.items import EDITABLE_WORK_ITEM_FIELDS, WorkItem
from ww.plan import WorkflowPlan
from ww.workflow_config import ProvidedVariable, SavedMetadata


def test_work_item_round_trips_with_every_field() -> None:
    item = WorkItem(
        "c1",
        "The error path is untested.",
        processed_item="missing coverage",
        proposed_solution="add a test",
        actual_solution="added a test",
        resolved=True,
        reported=True,
        reference_to_id="c0",
    )

    assert WorkItem.from_dict(item.to_dict()) == item
    assert WorkItem.from_dict({"id": "c1", "item": "text"}) == WorkItem("c1", "text")


def test_only_progress_fields_are_editable() -> None:
    assert {
        "fields",
        "processed_item",
        "proposed_solution",
        "actual_solution",
        "resolved",
        "reported",
        "reference_to_id",
    } == EDITABLE_WORK_ITEM_FIELDS


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "item must be a mapping"),
        ({"item": "text"}, "item ID must be a non-empty string"),
        ({"id": " ", "item": "text"}, "item ID must be a non-empty string"),
        ({"id": "c1", "item": ""}, "item text must be a non-empty string"),
        (
            {"id": "c1", "item": "t", "actual_solution": 1},
            "text fields must be strings",
        ),
        ({"id": "c1", "item": "t", "resolved": "true"}, "must be booleans"),
        ({"id": "c1", "item": "t", "reported": 1}, "must be booleans"),
        ({"id": "c1", "item": "t", "reference_to_id": ""}, "reference_to_id must be"),
        ({"id": "c1", "item": "t", "reference_to_id": 3}, "reference_to_id must be"),
    ],
)
def test_work_item_rejects_malformed_records(data: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        WorkItem.from_dict(data)


def _child(**changes: object) -> dict[str, object]:
    return {
        **ChildTask(
            "c1",
            "Fix it.",
            "task",
            "P/c1",
            status="completed",
            run_id="01-task",
            summary="done",
            start_operation_id="op",
            parent_task_id="P",
        ).to_dict(),
        **changes,
    }


def test_child_task_round_trips() -> None:
    assert ChildTask.from_dict(_child()).to_dict() == _child()
    minimal = ChildTask("c1", "Fix it.", "", "P/c1")
    assert ChildTask.from_dict(minimal.to_dict()) == minimal


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("c1", "child task must be a mapping"),
        (_child(id=""), "fields must be non-empty strings"),
        (_child(task_id=None), "fields must be non-empty strings"),
        (_child(workflow=None), "workflow must be a string"),
        (_child(status="done"), "child task status must be one of"),
        (_child(run_id=1), "run ID must be a string or null"),
        (_child(summary=["x"]), "summary must be a string or null"),
        (_child(start_operation_id=1), "start_operation_id must be a string"),
        (_child(parent_task_id=False), "parent_task_id must be a string"),
    ],
)
def test_child_task_rejects_malformed_records(data: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ChildTask.from_dict(data)


def test_from_path_prefixes_decoding_errors() -> None:
    def fail(_value: object) -> object:
        raise TypeError("bad value")

    assert _from_path(int, "3", "run.cursor") == 3
    with pytest.raises(ValueError, match="^run.items\\[2\\]: bad value$"):
        _from_path(fail, None, "run.items[2]")


def test_saved_mappings_are_type_checked() -> None:
    assert _variables({"a": "b"}, "values") == (("a", "b"),)
    assert _positive_int_mapping({"loop": 2}, "iterations") == (("loop", 2),)
    for bad in ([], {"a": 1}, {1: "b"}):
        with pytest.raises(ValueError, match="values must be a mapping of strings"):
            _variables(bad, "values")
    for bad in ({"loop": 0}, {"loop": True}, {"loop": "2"}, None):
        with pytest.raises(ValueError, match="mapping of positive integers"):
            _positive_int_mapping(bad, "iterations")


def test_persisted_input_request_keeps_a_conditional_value() -> None:
    request = InputRequest(
        "item", (ProvidedVariable("commit_message", "Why.", conditional=True),)
    )

    assert InputRequest.from_dict(request.to_dict()) == request
    with pytest.raises(ValueError, match="provided conditional must be a boolean"):
        InputRequest.from_dict(
            {
                "item_id": "item",
                "values": [{"name": "commit_message", "conditional": "yes"}],
                "continuation": "complete",
            }
        )


def test_persisted_input_request_rejects_malformed_entries() -> None:
    with pytest.raises(ValueError, match="only objects"):
        InputRequest.from_dict(
            {
                "item_id": "item",
                "values": ["discarded-before"],
                "continuation": "complete",
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("index", "1"),
        ("index", True),
        ("exit_code", "0"),
        ("exit_code", False),
        ("stdout", []),
        ("stderr", {}),
        ("started_at", 1),
    ],
)
def test_persisted_command_execution_rejects_malformed_fields(
    field: str, value: object
) -> None:
    record: dict[str, object] = {
        "index": 1,
        "status": "pending",
        "started_at": None,
        "completed_at": None,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
    }
    record[field] = value

    with pytest.raises(ValueError, match=rf"command execution\.{field}"):
        CommandExecution.from_dict(record)


def _command_snapshot(schema_version: int) -> dict[str, object]:
    plan = WorkflowPlan(
        workflow="workflow",
        workflow_description="",
        agent="codex",
        task_id="TASK-1",
        modes=(),
        handoff=False,
        items=(
            plan_item(
                operation=PlannedAction(
                    "cli", Commands((CommandDefinition(shell="true"),))
                ),
                owner="ww",
                execution="automatic",
            ),
        ),
    )
    return PlanSnapshot(
        schema_version=schema_version,
        compiler_version="test",
        configuration_digest="digest",
        compiled_at="2026-09-09T00:00:00Z",
        plan=plan,
    ).to_dict()


def test_persisted_plan_rejects_malformed_provided_values() -> None:
    plan = WorkflowPlan(
        workflow="workflow",
        workflow_description="",
        agent="codex",
        task_id="TASK-1",
        modes=(),
        handoff=False,
        items=(plan_item(),),
    )
    snapshot = PlanSnapshot(
        schema_version=PLAN_SCHEMA_VERSION,
        compiler_version="test",
        configuration_digest="digest",
        compiled_at="2026-09-09T00:00:00Z",
        plan=plan,
    ).to_dict()
    snapshot["plan"]["items"][0]["provide"] = ["discarded-before"]

    with pytest.raises(ValueError, match="provide entries"):
        PlanSnapshot.from_dict(snapshot)


def test_saved_metadata_round_trips_through_persisted_plan() -> None:
    plan = WorkflowPlan(
        workflow="workflow",
        workflow_description="",
        agent="codex",
        task_id="TASK-1",
        modes=(),
        handoff=False,
        items=(
            plan_item(
                save_metadata=(SavedMetadata("jira_id", "jira.issue_id", "Issue ID."),)
            ),
        ),
    )
    snapshot = PlanSnapshot(
        schema_version=PLAN_SCHEMA_VERSION,
        compiler_version="test",
        configuration_digest="digest",
        compiled_at="2026-09-09T00:00:00Z",
        plan=plan,
    )

    loaded = PlanSnapshot.from_dict(snapshot.to_dict())

    assert loaded.plan.items[0].save_metadata == (
        SavedMetadata("jira_id", "jira.issue_id", "Issue ID."),
    )


def test_project_metadata_scope_round_trips_through_persisted_plan() -> None:
    plan = WorkflowPlan(
        workflow="workflow",
        workflow_description="",
        agent="codex",
        task_id="TASK-1",
        modes=(),
        handoff=False,
        items=(
            plan_item(
                save_metadata=(
                    SavedMetadata(
                        "url", "environments.staging.url", "Staging URL.", "project"
                    ),
                )
            ),
        ),
    )
    snapshot = PlanSnapshot(
        schema_version=PLAN_SCHEMA_VERSION,
        compiler_version="test",
        configuration_digest="digest",
        compiled_at="2026-09-09T00:00:00Z",
        plan=plan,
    )

    loaded = PlanSnapshot.from_dict(snapshot.to_dict())

    assert loaded.plan.items[0].save_metadata[0].scope == "project"


@pytest.mark.parametrize("version", [3, 0, True, "1"])
def test_a_plan_at_another_schema_version_is_not_loaded(version: object) -> None:
    snapshot = _command_snapshot(PLAN_SCHEMA_VERSION)
    snapshot["schema_version"] = version
    with pytest.raises(ValueError, match="unsupported plan snapshot schema"):
        PlanSnapshot.from_dict(snapshot)


def test_current_plan_schema_rejects_bare_shell_command() -> None:
    snapshot = _command_snapshot(PLAN_SCHEMA_VERSION)
    snapshot["plan"]["items"][0]["operation"]["payload"]["commands"] = ["true"]

    with pytest.raises(ValueError, match="command must be a mapping"):
        PlanSnapshot.from_dict(snapshot)


@pytest.mark.parametrize(
    "field", ["requires_agent_input", "summary", "item_template", "artifact"]
)
def test_persisted_plan_rejects_coerced_boolean_fields(field: str) -> None:
    snapshot = _command_snapshot(PLAN_SCHEMA_VERSION)
    snapshot["plan"]["items"][0][field] = "false"

    with pytest.raises(ValueError, match=rf"plan\.items\[0\]\.{field}"):
        PlanSnapshot.from_dict(snapshot)


def test_persisted_plan_rejects_non_list_commands_with_field_path() -> None:
    snapshot = _command_snapshot(PLAN_SCHEMA_VERSION)
    snapshot["plan"]["items"][0]["operation"]["payload"]["commands"] = {}

    with pytest.raises(ValueError, match=r"action\.commands"):
        PlanSnapshot.from_dict(snapshot)
