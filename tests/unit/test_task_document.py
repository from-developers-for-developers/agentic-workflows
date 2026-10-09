# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the compact authoritative task index and run document codec."""

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
from ww.storage_adapters.task_document import (
    TASK_STATE_SCHEMA_VERSION,
    RunRevisionMismatchError,
    check_active_run,
    decode_run_document,
    decode_task_index,
    encode_run_document,
    encode_task_index,
)
from ww.transitions import materialize_child_plan


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


def _encode(
    runs: tuple[TaskRunAggregate, ...],
    handoff: str | None = None,
    revision: int = 1,
    ledger: dict[str, list[dict[str, object]]] | None = None,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """Encode a task index and its run documents, all written at ``revision``."""
    documents = {
        run.run_id: encode_run_document("TASK-1", run, revision) for run in runs
    }
    revisions = {run.run_id: revision for run in runs}
    index = encode_task_index(
        "TASK-1", runs, revisions, handoff, revision, ledger or {}
    )
    return index, documents


def _decode(
    index: dict[str, object], documents: dict[str, dict[str, object]]
) -> tuple[
    tuple[TaskRunAggregate, ...],
    str | None,
    int,
    dict[str, list[dict[str, object]]],
]:
    decoded = decode_task_index(index, "TASK-1")
    runs = tuple(
        decode_run_document(documents[run_id], "TASK-1", run_id, revision)
        for run_id, revision in decoded.run_revisions
    )
    check_active_run(decoded, runs)
    return runs, decoded.handoff, decoded.revision, decoded.ledger


def test_codec_round_trip_is_deterministic_compact_and_non_mutating() -> None:
    first = _run(completed=True, settings={"empty": {}, "false": False})
    second = _run("02-task", settings={"empty": {}, "false": False})
    runs = (first, second)
    dense_before = copy.deepcopy([run.to_dict() for run in runs])
    ledger = {
        "01-task": [{"workflow": "task", "status": "completed", "summary": "done"}]
    }

    encoded = _encode(runs, "choose=task", 7, ledger)
    encoded_again = _encode(runs, "choose=task", 7, ledger)
    index, documents = encoded
    decoded = _decode(index, documents)

    assert encoded == encoded_again
    assert [run.to_dict() for run in runs] == dense_before
    assert decoded == (runs, "choose=task", 7, ledger)
    assert index["active_run"] == "02-task"
    assert index["runs"] == [
        {"id": "01-task", "revision": 7},
        {"id": "02-task", "revision": 7},
    ]
    for document in documents.values():
        assert len(document["extension_snapshots"]) == 1
        raw_run = document["run"]
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
    decoded, _, _, _ = _decode(*_encode((first, second)))

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
    sparse = _encode((false_artifact,))
    raw_run = sparse[1]["01-task"]["run"]
    raw_item = raw_run["snapshot"]["plan"]["items"][0]
    raw_execution = raw_run["state"]["item_executions"][0]
    assert raw_item["artifact"] is False
    assert raw_execution["supplied_values"] == {"empty": ""}
    assert raw_execution["commands"][0]["exit_code"] == 0
    assert raw_run["state"]["cursor"] == 0
    decoded, _, _, _ = _decode(*sparse)
    assert decoded == (false_artifact,)


@pytest.mark.parametrize("active_run", [None, "missing", 1, False])
def test_active_run_must_match_the_noncompleted_run(active_run: object) -> None:
    index, documents = _encode((_run(),))
    index["active_run"] = active_run

    with pytest.raises(ValueError, match="active_run"):
        _decode(index, documents)


def test_completed_document_requires_explicit_null_active_run() -> None:
    index, documents = _encode((_run(completed=True),))
    assert index["active_run"] is None
    del index["active_run"]

    with pytest.raises(ValueError, match="missing field: active_run"):
        _decode(index, documents)


def test_extension_snapshot_references_are_content_addressed_and_strict() -> None:
    encoded = encode_run_document("TASK-1", _run(), 1)
    snapshot_id = next(iter(encoded["extension_snapshots"]))
    corrupted = copy.deepcopy(encoded)
    corrupted["extension_snapshots"][snapshot_id]["settings"] = {"changed": True}
    with pytest.raises(ValueError, match="content hash"):
        decode_run_document(corrupted, "TASK-1", "01-task", 1)

    conflicting = copy.deepcopy(encoded)
    item = conflicting["run"]["snapshot"]["plan"]["items"][0]
    item["operation"]["payload"]["settings"] = {}
    with pytest.raises(ValueError, match="conflicting inline"):
        decode_run_document(conflicting, "TASK-1", "01-task", 1)

    dangling = copy.deepcopy(encoded)
    dangling["run"]["snapshot"]["plan"]["items"][0]["operation"]["payload"][
        "snapshot"
    ] = "sha256:missing"
    with pytest.raises(ValueError, match="unknown extension snapshot"):
        decode_run_document(dangling, "TASK-1", "01-task", 1)


def test_distinct_template_plan_is_retained_and_uses_shared_snapshots() -> None:
    run = _run(settings={"branch": "main"})
    template_item = replace(run.snapshot.plan.items[0], name="template-work")
    template = replace(run.snapshot.plan, items=(template_item,))
    run = replace(run, snapshot=replace(run.snapshot, template_plan=template))

    encoded = encode_run_document("TASK-1", run, 1)
    raw_snapshot = encoded["run"]["snapshot"]

    assert "template_plan" in raw_snapshot
    assert len(encoded["extension_snapshots"]) == 1
    assert raw_snapshot["plan"]["items"] == [
        {"id": "task:work", "template": "task:work", "name": "work"}
    ]
    template_ref = raw_snapshot["template_plan"]["items"][0]["operation"]["payload"][
        "snapshot"
    ]
    assert template_ref in encoded["extension_snapshots"]
    assert decode_run_document(encoded, "TASK-1", "01-task", 1) == run


def test_state_without_omitted_required_fields_decodes() -> None:
    """The writer omits an empty mode list and a missing active item."""
    runs = (_run(),)
    encoded = _encode(runs)
    state = encoded[1]["01-task"]["run"]["state"]
    assert "modes" not in state
    assert "active_item_id" not in state

    decoded = _decode(*encoded)

    assert decoded[0] == runs
    assert decoded[0][0].state.modes == ()
    assert decoded[0][0].state.active_item_id is None


@pytest.mark.parametrize("version", [0, 1, 2, 4, True, 3.0, "3", None])
@pytest.mark.parametrize("document", ["index", "run"])
def test_schema_version_must_be_the_current_strict_integer(
    version: object, document: str
) -> None:
    index, documents = _encode((_run(),))
    target = index if document == "index" else documents["01-task"]
    target["schema_version"] = version

    with pytest.raises(ValueError, match="unsupported task state schema"):
        _decode(index, documents)


def test_the_previous_single_document_layout_is_rejected() -> None:
    """Schema 2 kept every run inline; it is not read, not even partly."""
    index, documents = _encode((_run(),))
    legacy = {**index, "schema_version": 2, "runs": [documents["01-task"]["run"]]}

    with pytest.raises(ValueError, match="unsupported task state schema"):
        decode_task_index(legacy, "TASK-1")


def test_a_run_written_at_another_revision_than_the_index_names_is_detected() -> None:
    first = _run("01-task", completed=True)
    second = _run("02-task")
    index = encode_task_index(
        "TASK-1", (first, second), {"01-task": 2, "02-task": 3}, None, 3, {}
    )
    documents = {
        "01-task": encode_run_document("TASK-1", first, 2),
        # Written by a later commit whose index was never published.
        "02-task": encode_run_document("TASK-1", second, 4),
    }

    with pytest.raises(RunRevisionMismatchError, match="written at revision 4"):
        _decode(index, documents)

    documents["02-task"] = encode_run_document("TASK-1", second, 3)
    assert _decode(index, documents)[0] == (first, second)


@pytest.mark.parametrize(
    "runs",
    [
        [{"id": "01-task", "revision": 2}],
        [{"id": "01-task", "revision": 0}],
        [{"id": "01-task"}],
        [{"id": 1, "revision": 1}],
        [{"id": "01-task", "revision": 1}, {"id": "01-task", "revision": 1}],
        ["01-task"],
    ],
)
def test_index_run_entries_are_strict(runs: object) -> None:
    index, _ = _encode((_run(),))
    index["runs"] = runs

    with pytest.raises(ValueError, match="task state runs"):
        decode_task_index(index, "TASK-1")


def test_a_run_document_must_belong_to_its_task_and_path() -> None:
    document = encode_run_document("TASK-1", _run(), 1)

    with pytest.raises(ValueError, match="task ID does not match"):
        decode_run_document(document, "TASK-2", "01-task", 1)
    with pytest.raises(ValueError, match="run ID does not match"):
        decode_run_document(document, "TASK-1", "02-task", 1)
    with pytest.raises(ValueError, match="unsupported task state format"):
        decode_task_index(document, "TASK-1")


def test_children_round_trip_with_fields_and_failed_status() -> None:
    children = (
        ChildTask("A", "Slice A", "child", "TASK-1/A", fields=(("area", "parser"),)),
        ChildTask("B", "Slice B", "", "TASK-1/B", status="failed"),
    )
    run = replace(_run(), children=children)

    encoded = encode_run_document("TASK-1", run, 1)
    decoded = decode_run_document(encoded, "TASK-1", "01-task", 1)

    assert encoded["schema_version"] == TASK_STATE_SCHEMA_VERSION
    assert "fields" not in encoded["run"]["children"][1]
    assert decoded.children == children


_EXPANDED_WORKFLOW = """workflows:
  - name: parent
    steps:
      - collect: Split the requirements into stories.
        items:
          steps:
            - resolve: Resolve the stories with tests and a minimal change.
            - review: Review the change.

              steps:
                - fix: Fix what the review found.

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
    """A run whose per-child stages are expanded; items never expand."""
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
    state, snapshot = materialize_child_plan(state, snapshot, children, _clock)
    return TaskRunAggregate(
        "01-parent", "parent", snapshot, state, items=items, children=children
    )


def test_expanded_plan_items_are_stored_as_a_diff_against_their_template(
    tmp_path: Path,
) -> None:
    run = _expanded_run(tmp_path)
    plan_items = run.snapshot.plan.items
    assert any(item.child_number == 2 for item in plan_items)
    assert sum(item.kind == "extension" for item in plan_items) == 1
    assert sum(item.item_context == "collect" for item in plan_items) == 5

    encoded = encode_run_document("TASK-1", run, 1)
    decoded = decode_run_document(encoded, "TASK-1", "01-parent", 1)

    assert decoded == run
    assert encode_run_document("TASK-1", decoded, 1) == encoded
    assert len(encoded["extension_snapshots"]) == 1
    raw = encoded["run"]["snapshot"]
    template_ids = {item["id"] for item in raw["template_plan"]["items"]}
    assert all("template" not in item for item in raw["template_plan"]["items"])
    for item in raw["plan"]["items"]:
        assert item["template"] in template_ids
        assert "description" not in item
        assert "operation" not in item or item["operation"]["type"] != "action"
    land = next(
        item for item in raw["plan"]["items"] if item["id"].endswith(":child:2")
    )
    assert land["child_template"] is False


def test_a_plan_without_a_template_plan_is_stored_in_full() -> None:
    run = _run()
    encoded = encode_run_document("TASK-1", run, 1)
    item = encoded["run"]["snapshot"]["plan"]["items"][0]

    assert "template_plan" not in encoded["run"]["snapshot"]
    assert "template" not in item
    assert item["description"] == "Work"
    assert decode_run_document(encoded, "TASK-1", "01-task", 1) == run


@pytest.mark.parametrize("template", ["task:missing", 1, None, ["task:work"]])
def test_an_unknown_template_reference_is_rejected(
    tmp_path: Path, template: object
) -> None:
    encoded = encode_run_document("TASK-1", _expanded_run(tmp_path), 1)
    encoded["run"]["snapshot"]["plan"]["items"][3]["template"] = template

    with pytest.raises(ValueError, match="unknown template"):
        decode_run_document(encoded, "TASK-1", "01-parent", 1)


def test_a_template_reference_without_a_template_plan_is_rejected() -> None:
    encoded = encode_run_document("TASK-1", _run(), 1)
    encoded["run"]["snapshot"]["plan"]["items"][0]["template"] = "task:work"

    with pytest.raises(ValueError, match="unknown template"):
        decode_run_document(encoded, "TASK-1", "01-task", 1)
