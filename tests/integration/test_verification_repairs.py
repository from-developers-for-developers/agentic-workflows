# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent rule verifiers delegate ordinary configurable repair work."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from tests.integration.test_rule_verification_flow import (
    PASS,
    TWO_HINTS,
    WORKFLOWS,
    _developed,
    _project,
    _report,
    _started,
)
from tests.workflow_helpers import assignment_token
from ww.errors import ConfigurationError, StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"
FAIL = {
    "id": "develop/1",
    "status": "judged",
    "verdict": "fail",
    "failures": [{"file": "app.py", "line": 1, "what": "Unexpected public output."}],
}


def _workflow(**verification: object) -> str:
    data = yaml.safe_load(WORKFLOWS)
    data["workflows"][0]["steps"][0]["verification"] = verification
    return yaml.safe_dump(data, sort_keys=False)


def _active(service: WorkflowService, name: str):
    page = service.instruction(TASK)
    for _ in range(12):
        if page.item_name == name and page.item_status == "in_progress":
            return page
        page = service.next(TASK)
    raise AssertionError(f"did not reach {name}: {page.item_name}")


def test_default_repairs_retain_the_verifier_and_all_round_evidence(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service, first = _developed(root)
    assert first.repeat_communication is not None
    session = first.repeat_communication["session"]
    first_operation = (
        service.load(TASK)[0].item_executions[service.load(TASK)[0].cursor].operation_id
    )
    failed = _report(service, FAIL)
    assert failed.item_name == "fix-rule-violations"
    assert failed.repeat_communication is not None
    assert failed.repeat_communication["session"] == session
    assert failed.repeat_communication["rule_results"] == [json.dumps(FAIL)]
    assert "Do not repeat the original full action" in failed.action_text
    state, snapshot = service.load(TASK)
    source = next(
        record
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.name == "develop"
    )
    assert source.held_completion is not None
    assert source.status == "pending"
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    result = "  Fixed public output.\n\nPreserve this exact result.\n"
    again = service.complete(TASK, artifact=result, summary_for_next="Fixed.")
    assert again.item_name == first.item_name
    assert again.repeat_communication is not None
    assert again.repeat_communication["session"] == session
    assert again.repeat_communication["round"] == 2
    assert [entry["text"] for entry in again.repeat_communication["results"]] == [
        result
    ]
    state, snapshot = service.load(TASK)
    assert state.item_executions[state.cursor].operation_id != first_operation
    original = next(
        record
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.name == "develop"
    )
    assert original.attempts == 1
    prior = next(
        record for record in state.execution_history if record.verification_results
    )
    assert prior.artifact is not None
    assert (root / prior.artifact).exists()
    assert prior.artifact != state.item_executions[state.cursor].artifact
    service = WorkflowService(Storage(root))
    finished = _report(service, PASS)
    assert finished.item_name == "check"
    artifact = root / ".ww/tasks/TASK-1/runs/01-task/steps/02-develop.md"
    assert "verified pass" in artifact.read_text()
    assert "Completions rejected before this one: 1." in artifact.read_text()


@pytest.mark.parametrize("manager_source", [False, True])
def test_auto_repairs_are_separate_workers_and_relay_exact_findings(
    tmp_path: Path,
    manager_source: bool,
) -> None:
    data = yaml.safe_load(WORKFLOWS)
    step = data["workflows"][0]["steps"][0]
    step.update({"subagents": False})
    if manager_source:
        step["role"] = "manager"
    else:
        step.update({"model": "dev-model", "reasoning": "high"})
    root = _project(tmp_path, yaml.safe_dump(data, sort_keys=False))
    service = _started(root, runtime="auto")
    service.next(
        TASK, caller_role="manager", model="actual-dev-model", reasoning="high"
    )
    (root / "app.py").write_text("print(1)\n")
    service.complete(
        TASK,
        artifact="Built.",
        summary_for_next="Built.",
        caller_role="manager" if manager_source else "worker",
        assignment=None if manager_source else assignment_token(service, TASK),
    )
    first = service.next(TASK, caller_role="manager")
    assert first.repeat_communication is not None
    verifier_session = first.repeat_communication["session"]
    old_token = assignment_token(service, TASK)
    findings = "  Exact finding α.\n  Preserve whitespace.\n"
    raw = json.dumps(FAIL, ensure_ascii=False, indent=2)
    handed = service.complete(
        TASK,
        artifact=findings,
        rule_results=(raw,),
        caller_role="worker",
        assignment=old_token,
    )
    assert handed.handoff_block is not None
    assert handed.handoff_block.repeat_communication["rule_results"] == [raw]
    fixer = service.next(TASK, caller_role="manager")
    assert fixer.item_name == "fix-rule-violations"
    assert fixer.role == "worker"
    assert fixer.repeat_communication["findings"] == findings
    assert fixer.repeat_communication["rule_results"] == [raw]
    assert fixer.requested_model == "actual-dev-model"
    text = MarkdownOutputAdapter().render_instruction(fixer)
    assert "manager to launch" in text
    assert "unchanged" in text
    with pytest.raises(StateError, match="separate worker"):
        service.complete(
            TASK,
            artifact="Shortcut.",
            summary_for_next="Shortcut.",
            caller_role="manager",
        )
    with pytest.raises(StateError, match="assignment"):
        service.complete(
            TASK,
            artifact="Old worker.",
            summary_for_next="Old worker.",
            caller_role="worker",
            assignment=old_token,
        )
    service.complete(
        TASK,
        artifact="Exact repair result.\n",
        summary_for_next="Fixed.",
        caller_role="worker",
        assignment=assignment_token(service, TASK),
    )
    resumed = service.next(TASK, caller_role="manager")
    assert resumed.item_name == first.item_name
    assert resumed.repeat_communication["session"] == verifier_session
    assert (
        resumed.repeat_communication["results"][0]["text"] == "Exact repair result.\n"
    )


def _event(name: str) -> dict:
    code = (
        "from pathlib import Path; p=Path('events.txt'); "
        f"p.write_text((p.read_text() if p.exists() else '') + {name!r} + '\\n')"
    )
    return {"name": name, "argv": [sys.executable, "-c", code]}


def test_custom_settings_and_both_lifecycles_run_without_replaying_source_hooks(
    tmp_path: Path,
) -> None:
    data = yaml.safe_load(WORKFLOWS)
    source = data["workflows"][0]["steps"][0]
    source.update(
        {
            "model": "dev-model",
            "reasoning": "high",
            "hooks": {
                "before_start": [_event("source-before")],
                "after_complete": [_event("source-after")],
            },
            "verification": {
                "model": "verifier-model",
                "reasoning": "medium",
                "hooks": {
                    "before_start": [_event("verify-before")],
                    "after_complete": [_event("verify-after")],
                },
                "on_failure": {
                    "handler": "fix-rule-violations",
                    "agent": "claudecode",
                    "model": "fix-model",
                    "hooks": {
                        "before_start": [_event("fix-before")],
                        "before_complete": [_event("fix-after")],
                    },
                },
            },
        }
    )
    root = _project(tmp_path, yaml.safe_dump(data, sort_keys=False))
    service, _ = _developed(root)
    verifier = _active(service, "develop-verify-1")
    assert verifier.requested_model == "verifier-model"
    assert verifier.requested_reasoning == "medium"
    _report(service, FAIL)
    fixer = _active(service, "fix-rule-violations")
    assert fixer.requested_agent == "claudecode"
    assert fixer.requested_model == "fix-model"
    assert fixer.requested_reasoning == "auto"
    service.complete(TASK, artifact="Fixed.", summary_for_next="Fixed.")
    _active(service, "develop-verify-1")
    _report(service, PASS)
    assert (root / "events.txt").read_text().splitlines() == [
        "source-before",
        "verify-before",
        "verify-after",
        "fix-before",
        "fix-after",
        "verify-before",
        "verify-after",
        "source-after",
    ]


def test_verification_uses_existing_fix_limits_without_an_assessment_limit(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    (root / "ww.json").write_text(
        json.dumps({"limits": {"fixes": 2, "assessments": 1}})
    )
    service, _ = _developed(root)
    assert _report(service, FAIL).item_name == "fix-rule-violations"
    again = service.complete(TASK, artifact="Tried.", summary_for_next="Tried.")
    assert again.item_name == "develop-verify-1"
    stopped = _report(service, FAIL)
    assert stopped.operator_reason == "fix_limit"
    state, snapshot = service.load(TASK)
    assert state.item_executions[state.cursor].held_completion is not None
    assert snapshot.plan.items[state.cursor].name == "develop"
    retried = service.next(TASK, retry=True)
    if retried.item_status != "in_progress":
        retried = _active(service, "fix-rule-violations")
    assert retried.item_name == "fix-rule-violations"


def test_a_failure_goto_leaves_the_hold_unaccepted_and_preserves_findings(
    tmp_path: Path,
) -> None:
    data = yaml.safe_load(_workflow(on_failure={"goto": "redesign"}))
    data["workflows"][0]["steps"].insert(0, {"redesign": "Reconsider the design."})
    root = _project(tmp_path, yaml.safe_dump(data, sort_keys=False))
    service = _started(root)
    assert service.next(TASK).item_name == "redesign"
    assert (
        service.complete(TASK, artifact="Design.", summary_for_next="Design.").item_name
        == "develop"
    )
    (root / "app.py").write_text("print(1)\n")
    service.complete(TASK, artifact="Built.", summary_for_next="Built.")
    returned = _report(service, FAIL)
    assert returned.item_name == "redesign"
    assert returned.goto_notice is not None
    state, _ = service.load(TASK)
    assert len(state.gotos) == 1
    assert any(
        record.status == "failed" and record.held_completion is not None
        for record in state.execution_history
    )
    assert any(record.verification_results for record in state.execution_history)
    assert not (root / ".ww/tasks/TASK-1/runs/01-task/steps/03-develop.md").exists()


def test_operator_waiver_replays_the_hold_without_another_repair(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    (root / "ww.json").write_text(json.dumps({"limits": {"fixes": 1}}))
    service, _ = _developed(root)
    assert _report(service, FAIL).operator_reason == "fix_limit"
    page = service.next(TASK, force=True, force_reason="Accept this exception.")
    assert page.item_name == "check"
    state, snapshot = service.load(TASK)
    assert not any(item.repair_of for item in snapshot.plan.items)
    source = next(
        record
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.name == "develop"
    )
    assert source.status == "completed"
    assert source.checks_waived == (("develop/1", "Accept this exception."),)


@pytest.mark.parametrize(
    "settings",
    [
        {"on_failure": {"goto": "develop"}},
        {"on_failure": {"goto": "missing"}},
        {"on_failure": {"goto": "check", "handler": "fix-rule-violations"}},
        {"on_failure": {"handler": "fix-rule-violations", "role": "manager"}},
        {"on_failure": {}},
    ],
)
def test_invalid_verification_overrides_fail_before_work_starts(
    tmp_path: Path,
    settings: dict,
) -> None:
    root = _project(tmp_path, _workflow(**settings))
    with pytest.raises(ConfigurationError):
        _started(root)


def test_multiple_custom_repair_steps_and_automatic_handlers_are_ordinary_work(
    tmp_path: Path,
) -> None:
    workflow = _workflow(
        on_failure={
            "steps": [
                {"patch": "Patch the reported violations."},
                {
                    "name": "record",
                    "argv": [sys.executable, "-c", "print('repair checked')"],
                },
            ]
        }
    )
    root = _project(tmp_path, workflow)
    service, _ = _developed(root)
    patch = _report(service, FAIL)
    assert patch.item_name == "patch"
    again = service.complete(TASK, artifact="Patched.", summary_for_next="Patched.")
    assert again.item_name == "develop-verify-1"
    assert again.repeat_communication is not None
    assert any(
        result["text"] == "Patched." for result in again.repeat_communication["results"]
    )
    assert _report(service, PASS).item_name == "check"


def test_settings_only_override_keeps_the_focused_builtin_action(
    tmp_path: Path,
) -> None:
    data = yaml.safe_load(_workflow(on_failure={"model": "fix-model"}))
    data["workflows"][0]["steps"][0].update(model="dev-model", reasoning="high")
    root = _project(tmp_path, yaml.safe_dump(data, sort_keys=False))
    service, _ = _developed(root)
    repair = _report(service, FAIL)
    assert repair.item_name == "fix-rule-violations"
    assert repair.requested_model == "fix-model"
    assert repair.requested_reasoning == "auto"
    assert "Do not repeat the original full action" in repair.action_text


@pytest.mark.parametrize("waive", [False, True])
def test_source_rule_dispute_from_repair_retries_or_waives_the_held_attempt(
    tmp_path: Path, waive: bool
) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)
    repair = _report(service, FAIL)
    assert repair.fix_required.failures[0].judged
    stopped = service.dispute(TASK, ("develop/1",), "The output is required.")
    assert stopped.operator_reason == "check_disputed"
    assert stopped.disputes[0].failure.judged
    page = service.next(
        TASK,
        retry=not waive,
        force=waive,
        force_reason="Accept the output." if waive else None,
    )
    if waive:
        assert page.item_name == "check"
    else:
        assert (
            _active(service, "fix-rule-violations").item_name == "fix-rule-violations"
        )
        service.complete(TASK, artifact="Repaired.", summary_for_next="Repaired.")
        assert _report(service, PASS).item_name == "check"


def test_backward_goto_after_repair_runs_source_before_any_new_repair(
    tmp_path: Path,
) -> None:
    data = yaml.safe_load(WORKFLOWS)
    data["workflows"][0]["allowed_gotos"] = [["check", "develop"]]
    root = _project(tmp_path, yaml.safe_dump(data, sort_keys=False))
    service, _ = _developed(root)
    _report(service, FAIL)
    service.complete(TASK, artifact="Repaired.", summary_for_next="Repaired.")
    assert _report(service, PASS).item_name == "check"
    page = service.complete(
        TASK, artifact="Another iteration.", summary_for_next="Again.", goto="develop"
    )
    assert page.item_name == "develop"
    state, snapshot = service.load(TASK)
    assert all(
        record.status == "completed" and record.attempts == 0
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.repair_of
    )
    service.complete(TASK, artifact="Rebuilt.", summary_for_next="Rebuilt.")
    assert _report(service, PASS).item_name == "check"


def test_all_verifier_groups_must_pass_after_a_repair(tmp_path: Path) -> None:
    root = _project(tmp_path, TWO_HINTS)
    service, first = _developed(root)
    first_session = first.repeat_communication["session"]
    _report(service, FAIL)
    again = service.complete(TASK, artifact="Repaired.", summary_for_next="Repaired.")
    assert again.repeat_communication["session"] == first_session
    second = _report(service, PASS)
    assert second.item_name == "develop-verify-2"
    state, snapshot = service.load(TASK)
    source = next(
        record
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.name == "develop"
    )
    assert source.held_completion is not None and source.status != "completed"
    assert _report(service, {**PASS, "id": "develop/2"}).item_name == "check"


def test_failure_goto_obeys_the_existing_goto_limit(tmp_path: Path) -> None:
    data = yaml.safe_load(_workflow(on_failure={"goto": "redesign"}))
    data["workflows"][0]["steps"].insert(0, {"redesign": "Design it."})
    root = _project(tmp_path, yaml.safe_dump(data, sort_keys=False))
    (root / "ww.json").write_text(json.dumps({"limits": {"gotos": 1}}))
    service = _started(root)
    service.next(TASK)
    for round_number in range(2):
        service.complete(TASK, artifact="Design.", summary_for_next="Design.")
        (root / "app.py").write_text(f"print({round_number})\n")
        service.complete(TASK, artifact="Built.", summary_for_next="Built.")
        page = _report(service, FAIL)
    assert page.operator_reason == "goto_limit"
    state, snapshot = service.load(TASK)
    assert len(state.gotos) == 1
    assert not any(item.repair_of for item in snapshot.plan.items)
    assert any(record.verification_results for record in state.item_executions)
    assert all(
        record.result.startswith("skipped: verification.on_failure goto")
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.name == "develop"
    )
    assert any(
        record.held_completion is not None and record.status == "failed"
        for record in state.execution_history
    )


def test_auto_verifier_hooks_finish_before_the_verdict_is_settled(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path,
        _workflow(
            model="verify-model",
            hooks={
                "before_start": [_event("before")],
                "after_complete": [_event("after")],
            },
        ),
    )
    service = _started(root, runtime="auto")
    service.next(TASK, caller_role="manager")
    (root / "app.py").write_text("print(1)\n")
    service.complete(
        TASK,
        artifact="Built.",
        summary_for_next="Built.",
        caller_role="worker",
        assignment=assignment_token(service, TASK),
    )
    verifier = service.next(TASK, caller_role="manager")
    assert verifier.item_name == "develop-verify-1"
    assert verifier.requested_model == "verify-model"
    assert (root / "events.txt").read_text().splitlines() == ["before"]
    service.complete(
        TASK,
        artifact="Findings.",
        rule_results=(json.dumps(FAIL),),
        caller_role="worker",
        assignment=assignment_token(service, TASK),
    )
    assert (root / "events.txt").read_text().splitlines() == ["before", "after"]
    assert service.next(TASK, caller_role="manager").item_name == "fix-rule-violations"
