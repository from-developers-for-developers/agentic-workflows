# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for closed and validated Python model contracts."""

from __future__ import annotations

import pytest

from ww.actions import (
    AssertionDefinition,
    CommandDefinition,
    Commands,
    Extension,
    PlannedAction,
    Prompt,
    Skill,
)
from ww.execution_models import (
    PLAN_SCHEMA_VERSION,
    CommandExecution,
    InputRequest,
    PlanSnapshot,
)
from ww.plan import PlanItem, WorkflowPlan
from ww.workflow_config import ProvidedVariable, SavedMetadata


def _item(**changes: object) -> PlanItem:
    values: dict[str, object] = {
        "id": "workflow:step:step:step:1",
        "position": 1,
        "name": "step",
        "description": "",
        "operation": PlannedAction("prompt", Prompt("Do the work.")),
        "owner": "agent",
        "execution": "agent_instruction",
        "requires_agent_input": False,
        "workflow": "workflow",
        "step": "step",
        "parent": None,
        "phase": "step",
        "source": "step",
        "registered_handler": None,
    }
    values.update(changes)
    return PlanItem(**values)  # type: ignore[arg-type]


def test_plan_item_rejects_contradictory_automatic_action() -> None:
    with pytest.raises(ValueError, match="conflicts with owner/execution"):
        _item(execution="automatic")


@pytest.mark.parametrize(
    "changes, payload",
    [
        ({"operation": PlannedAction("prompt", Skill("unexpected"))}, "payload"),
        (
            {
                "operation": PlannedAction(
                    "prompt", Commands((CommandDefinition(argv=("true",)),))
                )
            },
            "payload",
        ),
        (
            {
                "operation": PlannedAction(
                    "prompt", Extension("ext/acme/example/handlers:run")
                )
            },
            "payload",
        ),
    ],
)
def test_plan_item_rejects_payloads_for_another_action_kind(
    changes: dict[str, object], payload: str
) -> None:
    with pytest.raises(ValueError, match=rf"action {payload}"):
        _item(**changes)


def test_plan_item_rejects_inconsistent_agent_input_flag() -> None:
    with pytest.raises(ValueError, match="requires_agent_input"):
        _item(
            operation=PlannedAction(
                "cli", Commands((CommandDefinition(argv=("true",)),))
            ),
            owner="ww",
            execution="automatic",
            provide=(ProvidedVariable("answer"),),
        )


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"phase": "later"}, "invalid plan item phase"),
        ({"item_operation": "archive"}, "invalid item operation"),
        ({"child_operation": "dispatch"}, "invalid child operation"),
    ],
)
def test_plan_item_rejects_unknown_closed_values(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _item(**changes)


def test_assertion_rejects_unknown_operator() -> None:
    with pytest.raises(ValueError, match="invalid assertion operator"):
        AssertionDefinition(operator="contains", expected="done")  # type: ignore[arg-type]


def test_persisted_input_request_rejects_malformed_entries() -> None:
    with pytest.raises(ValueError, match="only objects"):
        InputRequest.from_dict(
            {
                "item_id": "item",
                "values": ["discarded-before"],
                "continuation": "complete",
            }
        )


def test_persisted_plan_rejects_malformed_provided_values() -> None:
    plan = WorkflowPlan(
        workflow="workflow",
        workflow_description="",
        agent="codex",
        task_id="TASK-1",
        modes=(),
        handoff=False,
        items=(_item(),),
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
            _item(
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


def test_a_plan_schema_without_a_migration_is_not_loaded() -> None:
    snapshot = _command_snapshot(PLAN_SCHEMA_VERSION)
    snapshot["schema_version"] = 14
    with pytest.raises(ValueError, match="unsupported plan snapshot schema: 14"):
        PlanSnapshot.from_dict(snapshot)


def test_a_schema_15_plan_loads_with_no_modes_on_its_items() -> None:
    snapshot = _command_snapshot(PLAN_SCHEMA_VERSION)
    snapshot["schema_version"] = 15
    assert "modes" not in snapshot["plan"]["items"][0]

    loaded = PlanSnapshot.from_dict(snapshot)

    assert loaded.schema_version == PLAN_SCHEMA_VERSION == 17
    assert loaded.plan.items[0].modes == ()
    assert loaded.to_dict()["plan"] == snapshot["plan"]


def test_a_schema_16_plan_loads_without_per_child_stages() -> None:
    snapshot = _command_snapshot(PLAN_SCHEMA_VERSION)
    snapshot["schema_version"] = 16
    item = snapshot["plan"]["items"][0]
    assert "child_stage" not in item and "child_number" not in item

    loaded = PlanSnapshot.from_dict(snapshot)

    assert loaded.schema_version == 17
    assert (loaded.plan.items[0].child_stage, loaded.plan.items[0].child_number) == (
        None,
        None,
    )
    assert loaded.to_dict()["plan"] == snapshot["plan"]


def test_project_metadata_scope_round_trips_through_persisted_plan() -> None:
    plan = WorkflowPlan(
        workflow="workflow",
        workflow_description="",
        agent="codex",
        task_id="TASK-1",
        modes=(),
        handoff=False,
        items=(
            _item(
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


def _command_snapshot(schema_version: int) -> dict[str, object]:
    plan = WorkflowPlan(
        workflow="workflow",
        workflow_description="",
        agent="codex",
        task_id="TASK-1",
        modes=(),
        handoff=False,
        items=(
            _item(
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


def test_current_plan_schema_rejects_bare_shell_command() -> None:
    snapshot = _command_snapshot(PLAN_SCHEMA_VERSION)
    snapshot["plan"]["items"][0]["operation"]["payload"]["commands"] = ["true"]

    with pytest.raises(ValueError, match="command must be a mapping"):
        PlanSnapshot.from_dict(snapshot)


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
