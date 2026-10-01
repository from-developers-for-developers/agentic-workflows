# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the compact authoritative task-state document codec."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ww.actions import Extension, PlannedAction
from ww.children import ChildTask
from ww.config import load_configuration
from ww.execution_models import (
    PLAN_SCHEMA_VERSION,
    CommandExecution,
    PlanSnapshot,
    TaskRunAggregate,
    initial_state,
)
from ww.extensions import ExtensionRegistry
from ww.items import WorkItem
from ww.plan import PlanItem, WorkflowPlan, compile_workflow_plan
from ww.storage_adapters.task_document import decode_task_document, encode_task_document
from ww.transitions import materialize_child_plan, materialize_item_plan


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
    assert raw_snapshot["plan"]["items"] == [
        {"id": "task:work", "template": "task:work", "name": "work"}
    ]
    template_ref = raw_snapshot["template_plan"]["items"][0]["operation"]["payload"][
        "snapshot"
    ]
    assert template_ref in encoded["extension_snapshots"]
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


@pytest.mark.parametrize("version", [1, 3, True, 2.0, "2", None])
def test_schema_version_must_be_the_current_strict_integer(version: object) -> None:
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


_EXPANDED_WORKFLOW = """workflows:
  - name: parent
    steps:
      - collect: Split the requirements into stories.
        items:
          steps:
            - resolve: Resolve {{ww.item.id}} with tests and a minimal change.
              item_phase: resolve
            - review: Review the change.
              max_rounds: 2
              loop:
                - fix: Fix what the review found.
                  break: The review found nothing.
            - name: ext/ww/git/handlers:git-commit
      - slices: Split the stories into child tasks.
        children:
          steps:
            - implement:
                workflow: child
            - land: Land {{ww.child.id}}.
  - name: child
    steps:
      - work: Work.
"""


def _clock() -> str:
    return "2026-01-01T00:00:00Z"


def _expanded_run(tmp_path: Path) -> TaskRunAggregate:
    """A run whose per-item and per-child stages are expanded."""
    (tmp_path / "ww.json").write_text(
        '{"extensions": {"ww/git": {}}}', encoding="utf-8"
    )
    (tmp_path / "ww.yaml").write_text(_EXPANDED_WORKFLOW, encoding="utf-8")
    plan = compile_workflow_plan(
        load_configuration(tmp_path / "ww.yaml"),
        tmp_path,
        "parent",
        "codex",
        extensions=ExtensionRegistry.discover(tmp_path),
    )
    snapshot = PlanSnapshot(
        schema_version=PLAN_SCHEMA_VERSION,
        compiler_version="plan-v1",
        configuration_digest="configuration",
        compiled_at="2026-01-01T00:00:00Z",
        plan=plan,
    )
    state = initial_state(snapshot, (), "2026-01-01T00:00:00Z", run_id="01-parent")
    items = tuple(WorkItem(f"story-{n}", f"Story {n}.") for n in range(1, 4))
    children = (
        ChildTask("A", "Slice A", "child", "TASK-1/A"),
        ChildTask("B", "Slice B", "child", "TASK-1/B"),
    )
    state, snapshot = materialize_item_plan(state, snapshot, items, _clock)
    state, snapshot = materialize_child_plan(state, snapshot, children, _clock)
    return TaskRunAggregate(
        "01-parent", "parent", snapshot, state, items=items, children=children
    )


def test_expanded_plan_items_are_stored_as_a_diff_against_their_template(
    tmp_path: Path,
) -> None:
    run = _expanded_run(tmp_path)
    plan_items = run.snapshot.plan.items
    assert any(item.item_id == "story-3" for item in plan_items)
    assert any(item.child_number == 2 for item in plan_items)
    assert any(item.kind == "loop" and item.item_id for item in plan_items)
    assert sum(item.kind == "extension" for item in plan_items) == 3

    encoded = encode_task_document("TASK-1", (run,), None, 1, {})
    decoded, _, _, _ = decode_task_document(encoded, "TASK-1")

    assert decoded == (run,)
    assert encode_task_document("TASK-1", decoded, None, 1, {}) == encoded
    assert len(encoded["extension_snapshots"]) == 1
    raw = encoded["runs"][0]["snapshot"]
    template_ids = {item["id"] for item in raw["template_plan"]["items"]}
    assert all("template" not in item for item in raw["template_plan"]["items"])
    for item in raw["plan"]["items"]:
        assert item["template"] in template_ids
        assert "description" not in item
        assert "operation" not in item or item["operation"]["type"] != "action"
    resolve = next(
        item for item in raw["plan"]["items"] if item["id"].endswith(":item:story-2")
    )
    assert resolve["item_template"] is False
    assert resolve["item_id"] == "story-2"


def test_a_plan_without_a_template_plan_is_stored_in_full() -> None:
    run = _run()
    encoded = encode_task_document("TASK-1", (run,), None, 1, {})
    item = encoded["runs"][0]["snapshot"]["plan"]["items"][0]

    assert "template_plan" not in encoded["runs"][0]["snapshot"]
    assert "template" not in item
    assert item["description"] == "Work"
    assert decode_task_document(encoded, "TASK-1")[0] == (run,)


@pytest.mark.parametrize("template", ["task:missing", 1, None, ["task:work"]])
def test_an_unknown_template_reference_is_rejected(
    tmp_path: Path, template: object
) -> None:
    encoded = encode_task_document("TASK-1", (_expanded_run(tmp_path),), None, 1, {})
    encoded["runs"][0]["snapshot"]["plan"]["items"][3]["template"] = template

    with pytest.raises(ValueError, match="unknown template"):
        decode_task_document(encoded, "TASK-1")


def test_a_template_reference_without_a_template_plan_is_rejected() -> None:
    encoded = encode_task_document("TASK-1", (_run(),), None, 1, {})
    encoded["runs"][0]["snapshot"]["plan"]["items"][0]["template"] = "task:work"

    with pytest.raises(ValueError, match="unknown template"):
        decode_task_document(encoded, "TASK-1")
