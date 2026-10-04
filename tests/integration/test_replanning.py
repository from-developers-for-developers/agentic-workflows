# SPDX-License-Identifier: GPL-3.0-or-later
"""A running task takes a changed workflow definition, once the operator agrees."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.cli import main
from ww.errors import StateError
from ww.execution_models import PLAN_SCHEMA_VERSION, PlanSnapshot
from ww.items import WorkItem
from ww.output_adapters.json_adapter import JsonOutputAdapter
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

HOOKED = """handlers:
  - name: reject
    argv: ["{command}"]
workflows:
  - name: task
    steps:
      - name: work
        hooks:
          before_complete:
            - name: reject
      - name: later
"""
TWO_STEPS = """workflows:
  - name: task
    steps:
      - first: {first}
      - second: {second}
"""
ITEMS = """workflows:
  - name: task
    steps:
      - review: Review.
        items:
          analyze: {guidance}
"""


def _write(root: Path, config: str) -> WorkflowService:
    """Write the configuration; a fresh service reads it, as each command does."""
    (root / "ww.yaml").write_text(config, encoding="utf-8")
    return WorkflowService(Storage(root))


def _started(root: Path, config: str) -> WorkflowService:
    service = _write(root, config)
    service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime="single",
        caller_role="manager",
    )
    service.next("TASK-1", caller_role="manager")
    return service


def _complete(service: WorkflowService) -> None:
    service.complete(
        "TASK-1",
        artifact="Done.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )


def _progress(service: WorkflowService) -> list[tuple[str, str]]:
    state, snapshot = service.load("TASK-1")
    return [
        (item.name, record.status)
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
    ]


def _markdown(page: object) -> str:
    return MarkdownOutputAdapter().render_instruction(page)  # type: ignore[arg-type]


def test_a_failed_hook_fixed_in_the_configuration_runs_as_fixed(
    tmp_path: Path,
) -> None:
    service = _started(tmp_path, HOOKED.format(command="false"))
    _complete(service)
    assert service.load("TASK-1")[0].status == "failed"
    service = _write(tmp_path, HOOKED.format(command="true"))

    stop = service.next("TASK-1", caller_role="manager")

    assert (stop.operator_reason, stop.control, stop.next_role) == (
        "plan_changed",
        "awaiting_operator",
        "operator",
    )
    assert stop.plan_change is not None
    (change,) = stop.plan_change.changes
    assert (change.kind, change.label) == (
        "changed",
        "`reject` (before complete of `work`)",
    )
    assert stop.plan_change.reruns == ()
    page = _markdown(stop)
    assert "## Operator: the workflow changed" in page
    assert '["false"]' in page and '["true"]' in page
    assert "nothing that already finished runs again" in page
    assert "./ww next TASK-1 --replan --role manager" in page
    assert "./ww next TASK-1 --keep-plan --role manager" in page

    service.next("TASK-1", caller_role="manager", replan=True)

    assert ("reject", "completed") in _progress(service)
    state, snapshot = service.load("TASK-1")
    assert snapshot.plan.items[state.cursor].name == "later"
    assert snapshot.plan_revision == 2
    assert any(record.status == "failed" for record in state.execution_history), (
        "the failed attempt stays readable"
    )


def test_a_retry_stops_at_the_change_instead_of_rerunning_the_old_command(
    tmp_path: Path,
) -> None:
    service = _started(tmp_path, HOOKED.format(command="false"))
    _complete(service)
    service = _write(tmp_path, HOOKED.format(command="true"))

    stop = service.next("TASK-1", caller_role="manager", retry=True)

    assert stop.operator_reason == "plan_changed"
    assert ("reject", "failed") in _progress(service)


def test_a_change_ahead_of_the_cursor_reruns_nothing(tmp_path: Path) -> None:
    service = _started(tmp_path, TWO_STEPS.format(first="One.", second="Two."))
    service = _write(tmp_path, TWO_STEPS.format(first="One.", second="Two, done well."))

    stop = service.next("TASK-1", caller_role="manager")

    assert stop.plan_change is not None
    assert [(c.kind, c.label) for c in stop.plan_change.changes] == [
        ("changed", "step `second`")
    ]
    (field,) = stop.plan_change.changes[0].fields
    assert (field.name, field.before, field.after) == (
        "description",
        '"Two."',
        '"Two, done well."',
    )
    service.next("TASK-1", caller_role="manager", replan=True)
    _complete(service)
    page = service.next("TASK-1", caller_role="manager")
    assert page.item_name == "second"
    assert page.action_text is not None and "Two, done well." in page.action_text


def test_a_change_to_a_finished_step_reruns_it_from_there(tmp_path: Path) -> None:
    service = _started(tmp_path, TWO_STEPS.format(first="One.", second="Two."))
    _complete(service)
    service.next("TASK-1", caller_role="manager")
    service = _write(tmp_path, TWO_STEPS.format(first="One, again.", second="Two."))

    stop = service.instruction("TASK-1", caller_role="manager")

    assert stop.plan_change is not None
    assert stop.plan_change.reruns == ("step `first`",)
    page = _markdown(stop)
    assert "This reruns finished steps" in page
    assert "./ww next TASK-1 --replan --yes --role manager" in page

    resumed = service.next("TASK-1", caller_role="manager", replan=True)

    assert resumed.item_name == "first"
    assert resumed.action_text is not None and "One, again." in resumed.action_text
    assert _progress(service)[1:3] == [("first", "in_progress"), ("second", "pending")]
    state, _ = service.load("TASK-1")
    assert {record.plan_item_id for record in state.execution_history} >= {
        "task:first:step:step:1",
        "task:second:step:step:1",
    }


def test_keeping_the_plan_does_not_ask_again(tmp_path: Path) -> None:
    service = _started(tmp_path, TWO_STEPS.format(first="One.", second="Two."))
    service = _write(tmp_path, TWO_STEPS.format(first="One.", second="Other."))
    assert service.next("TASK-1", caller_role="manager").plan_change is not None

    kept = service.next("TASK-1", caller_role="manager", keep_plan=True)

    assert kept.plan_change is None
    assert service.next("TASK-1", caller_role="manager").plan_change is None
    _, snapshot = service.load("TASK-1")
    assert snapshot.plan_revision == 1
    assert "Two." in [item.description for item in snapshot.plan.items]


def test_an_unrelated_change_is_adopted_silently(tmp_path: Path) -> None:
    config = TWO_STEPS.format(first="One.", second="Two.")
    service = _started(tmp_path, config)
    _, before = service.load("TASK-1")
    service = _write(tmp_path, config + "  - name: other\n    steps:\n      - x: X.\n")

    page = service.next("TASK-1", caller_role="manager")

    assert page.plan_change is None
    _, after = service.load("TASK-1")
    assert after.configuration_digest != before.configuration_digest
    assert after.plan == before.plan


def test_replanning_an_unchanged_workflow_is_refused(tmp_path: Path) -> None:
    service = _started(tmp_path, TWO_STEPS.format(first="One.", second="Two."))

    with pytest.raises(StateError, match="nothing to replan"):
        service.next("TASK-1", caller_role="manager", replan=True)


def test_replan_takes_no_other_decision(tmp_path: Path) -> None:
    service = _started(tmp_path, TWO_STEPS.format(first="One.", second="Two."))

    with pytest.raises(StateError, match="choose one of next --replan"):
        service.next("TASK-1", caller_role="manager", replan=True, keep_plan=True)
    with pytest.raises(StateError, match="choose one of next --replan"):
        service.next("TASK-1", caller_role="manager", replan=True, retry=True)


def test_a_change_to_expanded_item_stages_is_refused(tmp_path: Path) -> None:
    service = _started(tmp_path, ITEMS.format(guidance="Look closely."))
    service.add_item("TASK-1", WorkItem(id="a", item="Fix a."))
    _complete(service)
    service = _write(tmp_path, ITEMS.format(guidance="Look harder."))

    stop = service.next("TASK-1", caller_role="manager")

    assert stop.plan_change is not None
    assert stop.plan_change.refusal is not None
    assert "already expanded" in stop.plan_change.refusal
    page = _markdown(stop)
    assert "This change cannot be applied to this run" in page
    assert "--replan" not in page
    with pytest.raises(StateError, match="cannot replan"):
        service.next("TASK-1", caller_role="manager", replan=True)
    assert service.next("TASK-1", caller_role="manager", keep_plan=True)


def test_a_schema_1_items_run_replans_only_the_real_change(tmp_path: Path) -> None:
    service = _started(tmp_path, ITEMS.format(guidance="Look closely."))
    state, snapshot = service.load("TASK-1")
    old = replace(snapshot, schema_version=1)
    service.commit(replace(state, plan_digest=old.plan_digest), old)
    legacy = service.load("TASK-1")[1]
    assert legacy.schema_version == 1
    assert legacy.plan == snapshot.plan

    unrelated = (
        ITEMS.format(guidance="Look closely.")
        + "  - name: x\n    steps:\n      - y: Y.\n"
    )
    assert (
        _write(tmp_path, unrelated).next("TASK-1", caller_role="manager").plan_change
        is None
    )

    service = _write(tmp_path, ITEMS.format(guidance="Look harder."))
    stop = service.next("TASK-1", caller_role="manager")

    assert stop.plan_change is not None
    assert {
        field.name for change in stop.plan_change.changes for field in change.fields
    } == {"description"}
    service.next("TASK-1", caller_role="manager", replan=True)
    assert service.load("TASK-1")[1].schema_version == PLAN_SCHEMA_VERSION


def test_a_worker_page_is_not_stopped(tmp_path: Path) -> None:
    service = _started(tmp_path, TWO_STEPS.format(first="One.", second="Two."))
    service = _write(tmp_path, TWO_STEPS.format(first="One.", second="Other."))

    page = service.instruction(
        "TASK-1",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )

    assert page.plan_change is None


def test_the_json_page_carries_the_change(tmp_path: Path) -> None:
    service = _started(tmp_path, TWO_STEPS.format(first="One.", second="Two."))
    service = _write(tmp_path, TWO_STEPS.format(first="One.", second="Other."))

    data = json.loads(
        JsonOutputAdapter().render_instruction(
            service.next("TASK-1", caller_role="manager")
        )
    )

    assert data["operator_reason"] == "plan_changed"
    assert data["plan_change"]["changes"][0]["label"] == "step `second`"
    assert data["plan_change"]["reruns"] == []
    assert data["plan_change"]["refusal"] is None


def test_the_cli_asks_before_rerunning_finished_steps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _started(tmp_path, TWO_STEPS.format(first="One.", second="Two."))
    _complete(service)
    _write(tmp_path, TWO_STEPS.format(first="One, again.", second="Two."))
    command = ["--root", str(tmp_path), "next", "TASK-1", "--role", "manager"]

    assert main([*command, "--replan"]) == 1

    error = capsys.readouterr().err
    assert "run these finished steps again under the new definition" in error
    assert "step `first`" in error
    assert main([*command, "--replan", "--yes"]) == 0
    assert "Confirmed with --yes." in capsys.readouterr().err


def test_the_snapshot_keeps_the_bootstrap_step() -> None:
    snapshot = PlanSnapshot.from_dict(
        {
            "schema_version": PLAN_SCHEMA_VERSION,
            "compiler_version": "x",
            "configuration_digest": "d",
            "compiled_at": "now",
            "plan": {
                "workflow": "task",
                "workflow_description": "",
                "agent": "codex",
                "task_id": "TASK-1",
                "modes": [],
                "handoff": False,
                "items": [],
            },
            "bootstrap_step": "bind",
        }
    )

    assert snapshot.bootstrap_step == "bind"
    assert snapshot.to_dict()["bootstrap_step"] == "bind"
    assert (
        "bootstrap_step"
        not in PlanSnapshot.from_dict(
            {**snapshot.to_dict(), "bootstrap_step": None}
        ).to_dict()
    )
