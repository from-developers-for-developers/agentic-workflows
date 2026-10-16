# SPDX-License-Identifier: GPL-3.0-or-later
"""Work and assessment share a session; authored branches define goto edges."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from tests.integration.test_assessment_repeats import TASK, _complete, _next, _service
from tests.integration.test_rule_verification_flow import _project, _report, _started
from tests.workflow_helpers import assignment_token
from ww.cli import main
from ww.config import parse_yaml_text
from ww.errors import ConfigurationError, StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

FLOW = """workflows:
  - name: task
    steps:
      - code-review: Review the changes thoroughly.
        assess:
          assess: Are there issues?
          repeat_until: negative
          positive:
            steps:
              - fix: Fix the exact findings.
      - publish: Publish the accepted result.
"""


@pytest.mark.parametrize("runtime", ["single", "auto"])
def test_attached_review_retains_session_and_repeats_only_its_assessment(
    tmp_path: Path,
    runtime: str,
) -> None:
    service = _service(tmp_path, FLOW, runtime=runtime)
    first = _next(service)
    assert first.item_name == "code-review"
    assert first.action_text == "Review the changes thoroughly."
    assert first.assessment_prompt == "Are there issues?"
    worker = service.instruction(
        TASK, caller_role="worker", assignment=assignment_token(service, TASK)
    )
    rendered = MarkdownOutputAdapter().render_instruction(worker)
    assert rendered.index("Review the changes thoroughly.") < rendered.index(
        "Are there issues?"
    )
    session = first.repeat_communication["session"]
    findings = "  Exact finding α.\n\nPreserve this.\n"
    _complete(service, findings)
    fix = _next(service, "positive")
    assert fix.item_name == "fix"
    assert fix.repeat_communication["findings"] == findings
    _complete(service, "Exact fix result.\n")
    service = WorkflowService(Storage(tmp_path))
    again = _next(service)
    assert again.item_name == "code-review"
    assert again.action_text == "Are there issues?"
    assert "Review the changes thoroughly." not in again.action_text
    assert again.repeat_communication["session"] == session
    assert again.repeat_communication["results"][0]["text"] == "Exact fix result.\n"
    _complete(service, "No issues remain.")
    assert _next(service, "negative").item_name == "publish"


def _event(name: str) -> dict:
    return {
        "argv": [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; p=Path('events.txt'); "
                f"p.write_text((p.read_text() if p.exists() else '') + "
                f"{name!r} + '\\n')"
            ),
        ]
    }


def test_source_hooks_run_once_and_completion_hooks_wait_for_terminal_outcome(
    tmp_path: Path,
) -> None:
    data = yaml.safe_load(FLOW)
    data["workflows"][0]["steps"][0]["hooks"] = {
        "before_start": [_event("start")],
        "before_complete": [_event("finish")],
        "after_complete": [_event("after")],
    }
    service = _service(tmp_path, yaml.safe_dump(data), runtime="single")
    _next(service)
    _complete(service, "Issues.")
    assert (tmp_path / "events.txt").read_text().splitlines() == ["start"]
    _next(service, "positive")
    _complete(service, "Fixed.")
    again = _next(service)
    assert again.action_text == "Are there issues?"
    assert (tmp_path / "events.txt").read_text().splitlines() == ["start"]
    _complete(service, "Clear.")
    _next(service, "negative")
    assert (tmp_path / "events.txt").read_text().splitlines() == [
        "start",
        "finish",
        "after",
    ]


@pytest.mark.parametrize("group", [False, True])
def test_commands_and_groups_get_a_final_assessment_after_their_work(
    tmp_path: Path,
    group: bool,
) -> None:
    data = yaml.safe_load(FLOW)
    step = data["workflows"][0]["steps"][0]
    del step["code-review"]
    step["name"] = "inspect"
    if group:
        step["steps"] = [{"gather": "Gather the evidence."}]
    else:
        step["argv"] = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('ran').touch()",
        ]
    service = _service(tmp_path, yaml.safe_dump(data), runtime="single")
    page = _next(service)
    if group:
        assert page.item_name == "gather"
        page = _complete(service, "Evidence.")
    else:
        assert (tmp_path / "ran").exists()
    assert page.item_name == "inspect"
    assert page.assessment_prompt == "Are there issues?"


def test_manager_can_assess_an_automatic_source_action(tmp_path: Path) -> None:
    data = yaml.safe_load(FLOW)
    step = data["workflows"][0]["steps"][0]
    del step["code-review"]
    step.update(name="inspect", argv=[sys.executable, "-c", "pass"], role="manager")
    service = _service(tmp_path, yaml.safe_dump(data), runtime="auto")
    page = _next(service)
    assert page.item_name == "inspect"
    assert page.role == "manager"
    assert page.assessment_prompt == "Are there issues?"


@pytest.mark.parametrize("group", [False, True])
def test_final_assessor_inherits_the_source_execution_settings(
    tmp_path: Path, group: bool
) -> None:
    data = yaml.safe_load(FLOW)
    data["profiles"] = {"reviewer": "Review with care."}
    step = data["workflows"][0]["steps"][0]
    del step["code-review"]
    step.update(
        name="inspect",
        profile="reviewer",
        model="review-model",
        reasoning="high",
        subagents=False,
        workdir="root",
    )
    if group:
        step["steps"] = [{"gather": "Gather evidence."}]
    else:
        step["argv"] = [sys.executable, "-c", "pass"]
    service = _service(tmp_path, yaml.safe_dump(data), runtime="single")
    page = _next(service)
    if group:
        page = _complete(service, "Gathered.")
    assert page.item_name == "inspect"
    state, snapshot = service.load(TASK)
    item = snapshot.plan.items[state.cursor]
    assert item.profile == "reviewer"
    assert item.profile_instruction == "Review with care."
    assert item.model == "review-model"
    assert item.reasoning == "high"
    assert item.workdir == "root"
    assert item.subagents is False


@pytest.mark.parametrize("group", [False, True])
def test_attached_prompt_interpolates_source_outputs(
    tmp_path: Path, group: bool
) -> None:
    data = yaml.safe_load(FLOW)
    step = data["workflows"][0]["steps"][0]
    step["assess"]["assess"] = "Is {{evidence}} sufficient?"
    if group:
        del step["code-review"]
        step.update(
            name="inspect",
            steps=[
                {
                    "gather": "Gather evidence.",
                    "variables": [{"evidence": "The proof."}],
                }
            ],
        )
    else:
        step["variables"] = [{"evidence": "The proof."}]
    service = _service(tmp_path, yaml.safe_dump(data), runtime="single")
    _next(service)
    page = service.complete(
        TASK,
        artifact="Ready.",
        summary_for_next="Ready.",
        variables=(("evidence", "proof"),),
    )
    if group:
        assert page.assessment_prompt == "Is proof sufficient?"
    else:
        page = _next(service, "positive")
        assert page.item_name == "fix"
        if page.item_status == "pending":
            _next(service)
        _complete(service, "Fixed.")
        again = _next(service)
        assert again.assessment_prompt == "Is proof sufficient?"
        assert again.action_text == "Is proof sufficient?"


def test_attached_branch_derives_its_goto_and_rejects_undeclared_destinations(
    tmp_path: Path,
) -> None:
    flow = """workflows:
  - name: task
    steps:
      - develop: Develop it.
      - review: Review it thoroughly.
        assess:
          assess: Are there issues?
          positive:
            goto: [develop, "Fix what {{ww.goto.from}} found."]
      - publish: Publish it.
"""
    service = _service(tmp_path, flow, runtime="single")
    _next(service)
    review = _complete(service, "Developed.")
    assert review.goto_targets == ("develop",)
    with pytest.raises(StateError, match="may not go"):
        service.complete(
            TASK, artifact="Issues.", summary_for_next="Fix it.", goto="publish"
        )
    _complete(service, "Issues.")
    returned = _next(service, "positive")
    assert returned.item_name == "develop"
    assert returned.goto_notice.text.endswith("Fix what review found.")
    assert returned.goto_notice.text.startswith("Short summary;")


def test_deprecated_question_is_accepted_and_reported_by_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy = FLOW.replace(
        "          assess: Are there issues?", "          question: Are there issues?"
    )
    assert (
        parse_yaml_text(legacy).workflows[0].steps[0].assessment_question
        == "Are there issues?"
    )
    (tmp_path / "ww.yaml").write_text(legacy)
    assert main(["--root", str(tmp_path), "lint"]) == 0
    output = capsys.readouterr()
    assert "Deprecated:" in output.out + output.err
    assert "deprecations.md" in output.out + output.err


def test_conflicting_prompt_spellings_are_rejected() -> None:
    conflicting = FLOW.replace(
        "          assess: Are there issues?",
        "          assess: Are there issues?\n          question: Another question?",
    )
    with pytest.raises(ConfigurationError, match="cannot combine"):
        parse_yaml_text(conflicting)


@pytest.mark.parametrize("fail_again", [False, True])
def test_attached_rounds_and_judged_rule_repairs_preserve_their_own_sessions(
    tmp_path: Path,
    fail_again: bool,
) -> None:
    data = yaml.safe_load(FLOW)
    step = data["workflows"][0]["steps"][0]
    step["rules"] = ["Preserve the public interface."]
    step["hooks"] = {
        "before_start": [_event("start")],
        "after_complete": [_event("finish")],
    }
    root = _project(tmp_path, yaml.safe_dump(data))
    (root / "ww.json").write_text(json.dumps({"limits": {"fixes": 2}}))
    service = _started(root)
    first = _next(service)
    session = first.repeat_communication["session"]
    state, _ = service.load(TASK)
    original_mark = state.item_executions[state.cursor].change_mark
    (root / "app.py").write_text("value = 1\n")
    verify = _complete(service, "Issues found.")
    assert verify.verification is not None
    assert verify.assessment_prompt is None
    assert "Verify the rules" in verify.action_text
    fail = {
        "id": "code-review/1",
        "status": "judged",
        "verdict": "fail",
        "failures": [{"file": "app.py", "line": 1, "what": "Fix this."}],
    }
    repair = _report(service, fail)
    assert repair.item_name == "fix-rule-violations"
    assert repair.assessment_prompt is None
    again = _complete(service, "Rule fixed.")
    assert again.verification is not None
    assert again.action_text == verify.action_text
    passed = {
        "id": "code-review/1",
        "status": "judged",
        "verdict": "pass",
        "checked": "c",
    }
    _report(service, passed)
    _next(service, "positive")
    _complete(service, "Assessment finding fixed.")
    reassess = _next(service)
    assert reassess.action_text == "Are there issues?"
    assert reassess.repeat_communication["session"] == session
    state, _ = service.load(TASK)
    assert state.item_executions[state.cursor].change_mark == original_mark
    verify2 = _complete(service, "Clear.")
    assert verify2.verification is not None
    if fail_again:
        assert _report(service, fail).operator_reason == "fix_limit"
        assert (root / "events.txt").read_text().splitlines() == ["start"]
        return
    _report(service, passed)
    assert _next(service, "negative").item_name == "publish"
    assert (root / "events.txt").read_text().splitlines() == ["start", "finish"]
    state, snapshot = service.load(TASK)
    source = next(item for item in snapshot.plan.items if item.assessment_attached)
    previous = [
        record for record in state.execution_history if record.plan_item_id == source.id
    ]
    assert previous
    assert previous[0].check_failures("code-review/1") == 1


@pytest.mark.parametrize("explicit", [False, True])
def test_named_handlers_keep_their_work_when_an_assessment_is_attached(
    tmp_path: Path,
    explicit: bool,
) -> None:
    data = yaml.safe_load(FLOW)
    data["handlers"] = [{"code-review": "Review the registered handler's work."}]
    step = data["workflows"][0]["steps"][0]
    step["code-review"] = None
    if explicit:
        del step["code-review"]
        step.update(name="review", handler="code-review")
    service = _service(tmp_path, yaml.safe_dump(data), runtime="single")
    page = _next(service)
    assert page.action_text == "Review the registered handler's work."
    assert page.assessment_prompt == "Are there issues?"


def test_a_manager_owned_attached_step_delegates_repeating_outcome_work(
    tmp_path: Path,
) -> None:
    data = yaml.safe_load(FLOW)
    data["workflows"][0]["steps"][0]["role"] = "manager"
    service = _service(tmp_path, yaml.safe_dump(data), runtime="auto")
    assert _next(service).role == "manager"
    service.complete(
        TASK, artifact="Issues.", summary_for_next="Fix them.", caller_role="manager"
    )
    repair = _next(service, "positive")
    assert repair.item_name == "fix"
    assert repair.role == "worker"


def test_completion_goto_flag_cannot_bypass_the_declared_assessment_answer(
    tmp_path: Path,
) -> None:
    flow = FLOW.replace("          repeat_until: negative\n", "").replace(
        "            steps:\n              - fix: Fix the exact findings.",
        "            goto: publish",
    )
    service = _service(tmp_path, flow, runtime="single")
    _next(service)
    page = service.complete(
        TASK, artifact="Reviewed.", summary_for_next="Clear.", goto="publish"
    )
    assert page.choosing_outcome_of == "code-review"
    _next(service, "negative")
    state, _ = service.load(TASK)
    assert state.gotos == ()
