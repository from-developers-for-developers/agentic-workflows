# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the compact authoritative task-state document codec."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from ww.actions import Extension, PlannedAction
from ww.children import ChildTask
from ww.execution_models import (
    PLAN_SCHEMA_VERSION,
    CommandExecution,
    PlanSnapshot,
    TaskRunAggregate,
    initial_state,
)
from ww.plan import PlanItem, WorkflowPlan
from ww.storage_adapters.task_document import decode_task_document, encode_task_document


def _run(
    run_id: str = "01-task",
    *,
    settings: dict[str, object] | None = None,
    completed: bool = False,
    artifact: bool = True,
) -> TaskRunAggregate:
    item = PlanItem(
        id="task:work",
        position=1,
        name="work",
        description="Work",
        operation=PlannedAction(
            "extension",
            Extension(
                "ext/ww/git/handlers:commit",
                "1.2.3",
                1,
                "bundled:ww/git",
                "sha256:fingerprint",
                settings,
            ),
        ),
        owner="ww",
        execution="automatic",
        requires_agent_input=False,
        workflow="task",
        step="work",
        parent=None,
        phase="step",
        source="step",
        registered_handler=None,
        artifact=artifact,
    )
    plan = WorkflowPlan(
        workflow="task",
        workflow_description="",
        agent="codex",
        task_id="TASK-1",
        modes=(),
        handoff=False,
        items=(item,),
    )
    snapshot = PlanSnapshot(
        schema_version=PLAN_SCHEMA_VERSION,
        compiler_version="plan-v1",
        configuration_digest="configuration",
        compiled_at="2026-01-01T00:00:00Z",
        plan=plan,
    )
    state = initial_state(snapshot, (), "2026-01-01T00:00:00Z", run_id=run_id)
    if completed:
        state = replace(state, status="completed", cursor=1)
    return TaskRunAggregate(run_id, "task", snapshot, state)


def test_codec_round_trip_is_deterministic_compact_and_non_mutating() -> None:
    first = _run(completed=True, settings={"empty": {}, "false": False})
    second = _run("02-task", settings={"empty": {}, "false": False})
    runs = (first, second)
    dense_before = copy.deepcopy([run.to_dict() for run in runs])
    ledger = {
        "01-task": [{"workflow": "task", "status": "completed", "summary": "done"}]
    }

    encoded = encode_task_document("TASK-1", runs, "choose=task", 7, ledger)
    encoded_again = encode_task_document("TASK-1", runs, "choose=task", 7, ledger)
    decoded = decode_task_document(encoded, "TASK-1")

    assert encoded == encoded_again
    assert [run.to_dict() for run in runs] == dense_before
    assert decoded == (runs, "choose=task", 7, ledger)
    assert encoded["active_run"] == "02-task"
    assert len(encoded["extension_snapshots"]) == 1
    for raw_run in encoded["runs"]:
        assert "template_plan" not in raw_run["snapshot"]
        item = raw_run["snapshot"]["plan"]["items"][0]
        assert item["operation"]["payload"]["snapshot"].startswith("sha256:")
        assert "settings" not in item["operation"]["payload"]
        assert "commands" not in item
        assert "artifact" not in item
    assert len(json.dumps(encoded)) < len(json.dumps(dense_before))


def test_saved_empty_settings_and_false_values_survive_without_aliasing() -> None:
    first = _run(completed=True, settings={})
    second = _run("02-task", settings={})
    encoded = encode_task_document("TASK-1", (first, second), None, 1, {})
    decoded, _, _, _ = decode_task_document(encoded, "TASK-1")

    first_settings = decoded[0].snapshot.plan.items[0].payload_as(Extension).settings
    second_settings = decoded[1].snapshot.plan.items[0].payload_as(Extension).settings
    assert first_settings == second_settings == {}
    assert first_settings is not second_settings

    false_artifact = _run(artifact=False)
    false_state = replace(
        false_artifact.state,
        item_executions=(
            replace(
                false_artifact.state.item_executions[0],
                supplied_values=(("empty", ""),),
                operation_id_known=False,
                commands=(
                    CommandExecution(
                        index=1,
                        exit_code=0,
                        operation_id=(
                            false_artifact.state.item_executions[0].operation_id
                            + ":command:1"
                        ),
                    ),
                ),
            ),
        ),
    )
    false_artifact = replace(false_artifact, state=false_state)
    sparse = encode_task_document("TASK-1", (false_artifact,), None, 1, {})
    raw_item = sparse["runs"][0]["snapshot"]["plan"]["items"][0]
    raw_execution = sparse["runs"][0]["state"]["item_executions"][0]
    assert raw_item["artifact"] is False
    assert raw_execution["operation_id_known"] is False
    assert raw_execution["supplied_values"] == {"empty": ""}
    assert raw_execution["commands"][0]["exit_code"] == 0
    assert sparse["runs"][0]["state"]["cursor"] == 0
    decoded, _, _, _ = decode_task_document(sparse, "TASK-1")
    assert decoded == (false_artifact,)


@pytest.mark.parametrize("active_run", [None, "missing", 1, False])
def test_active_run_must_match_the_noncompleted_run(active_run: object) -> None:
    encoded = encode_task_document("TASK-1", (_run(),), None, 1, {})
    encoded["active_run"] = active_run

    with pytest.raises(ValueError, match="active_run"):
        decode_task_document(encoded, "TASK-1")


def test_completed_document_requires_explicit_null_active_run() -> None:
    encoded = encode_task_document("TASK-1", (_run(completed=True),), None, 1, {})
    assert encoded["active_run"] is None
    del encoded["active_run"]

    with pytest.raises(ValueError, match="missing field: active_run"):
        decode_task_document(encoded, "TASK-1")


def test_extension_snapshot_references_are_content_addressed_and_strict() -> None:
    encoded = encode_task_document("TASK-1", (_run(),), None, 1, {})
    snapshot_id = next(iter(encoded["extension_snapshots"]))
    corrupted = copy.deepcopy(encoded)
    corrupted["extension_snapshots"][snapshot_id]["settings"] = {"changed": True}
    with pytest.raises(ValueError, match="content hash"):
        decode_task_document(corrupted, "TASK-1")

    conflicting = copy.deepcopy(encoded)
    item = conflicting["runs"][0]["snapshot"]["plan"]["items"][0]
    item["operation"]["payload"]["settings"] = {}
    with pytest.raises(ValueError, match="conflicting inline"):
        decode_task_document(conflicting, "TASK-1")

    dangling = copy.deepcopy(encoded)
    dangling["runs"][0]["snapshot"]["plan"]["items"][0]["operation"]["payload"][
        "snapshot"
    ] = "sha256:missing"
    with pytest.raises(ValueError, match="unknown extension snapshot"):
        decode_task_document(dangling, "TASK-1")


def test_distinct_template_plan_is_retained_and_uses_shared_snapshots() -> None:
    run = _run(settings={"branch": "main"})
    template_item = replace(run.snapshot.plan.items[0], name="template-work")
    template = replace(run.snapshot.plan, items=(template_item,))
    run = replace(run, snapshot=replace(run.snapshot, template_plan=template))

    encoded = encode_task_document("TASK-1", (run,), None, 1, {})
    raw_snapshot = encoded["runs"][0]["snapshot"]

    assert "template_plan" in raw_snapshot
    assert len(encoded["extension_snapshots"]) == 1
    current_ref = raw_snapshot["plan"]["items"][0]["operation"]["payload"]["snapshot"]
    template_ref = raw_snapshot["template_plan"]["items"][0]["operation"]["payload"][
        "snapshot"
    ]
    assert current_ref == template_ref
    decoded, _, _, _ = decode_task_document(encoded, "TASK-1")
    assert decoded == (run,)


def test_state_without_omitted_required_fields_decodes() -> None:
    """The writer omits an empty mode list and a missing active item."""
    runs = (_run(),)
    encoded = encode_task_document("TASK-1", runs, None, 1, {})
    state = encoded["runs"][0]["state"]
    assert "modes" not in state
    assert "active_item_id" not in state

    decoded = decode_task_document(encoded, "TASK-1")

    assert decoded[0] == runs
    assert decoded[0][0].state.modes == ()
    assert decoded[0][0].state.active_item_id is None


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_schema_version_must_be_a_strict_integer(version: object) -> None:
    encoded = encode_task_document("TASK-1", (_run(),), None, 1, {})
    encoded["schema_version"] = version

    with pytest.raises(ValueError, match="unsupported task state schema"):
        decode_task_document(encoded, "TASK-1")


def test_children_round_trip_with_fields_and_skipped_status() -> None:
    children = (
        ChildTask("A", "Slice A", "child", "TASK-1/A", fields=(("area", "parser"),)),
        ChildTask("B", "Slice B", "", "TASK-1/B", status="skipped"),
    )
    run = replace(_run(), children=children)

    encoded = encode_task_document("TASK-1", (run,), None, 1, {})
    decoded, _, _, _ = decode_task_document(encoded, "TASK-1")

    assert encoded["schema_version"] == 2
    assert "fields" not in encoded["runs"][0]["children"][1]
    assert decoded[0].children == children


def test_a_schema_1_document_loads_its_children_without_fields() -> None:
    run = replace(_run(), children=(ChildTask("A", "Slice A", "child", "TASK-1/A"),))
    encoded = encode_task_document("TASK-1", (run,), None, 1, {})
    encoded["schema_version"] = 1

    decoded, _, _, _ = decode_task_document(encoded, "TASK-1")

    assert decoded[0].children[0].fields == ()
    assert decoded[0].children[0].status == "pending"
