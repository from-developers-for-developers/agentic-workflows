# SPDX-License-Identifier: GPL-3.0-or-later
"""Repeating assessments retain context and relay exact step messages."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.errors import ConfigurationError, StateError
from ww.instructions import Instruction
from ww.instructions.handoff import handoff_markdown
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"
WORKFLOW = """workflows:
  - name: task
    steps:
      - develop: Develop it once.
      - assess:
          question: Are there issues?
          repeat_until: negative
          positive:
            steps:
              - fix: Fix the exact findings.
      - publish: Publish it.
"""
md = MarkdownOutputAdapter()


def _service(
    root: Path, workflow: str = WORKFLOW, runtime: str = "auto", limit: int = 5
) -> WorkflowService:
    (root / "ww.yaml").write_text(workflow, encoding="utf-8")
    (root / "ww.json").write_text(
        json.dumps({"limits": {"assessments": limit}}), encoding="utf-8"
    )
    service = WorkflowService(Storage(root))
    service.start(
        "task", TASK, agent="codex", workflow_runtime=runtime, init_artifact="Do it."
    )
    return service


def _next(service: WorkflowService, outcome: str | None = None) -> Instruction:
    return service.next(TASK, caller_role="manager", outcome=outcome)


def _complete(service: WorkflowService, text: str) -> Instruction:
    return service.complete(
        TASK,
        artifact=text,
        summary_for_next="Short summary; the exact text is in the findings.",
        caller_role="worker",
        assignment=assignment_token(service, TASK),
    )


def _assess(service: WorkflowService) -> Instruction:
    assert _next(service).item_name == "develop"
    _complete(service, "Developed once.")
    page = _next(service)
    assert page.item_name == "assess"
    return page


@pytest.mark.parametrize("runtime", ["single", "auto"])
def test_only_the_assessment_and_its_work_repeat_with_exact_messages(
    tmp_path: Path, runtime: str
) -> None:
    service = _service(tmp_path, runtime=runtime)
    initial = _assess(service)
    assert initial.repeat_communication is not None
    session = initial.repeat_communication["session"]
    original_token = assignment_token(service, TASK)
    findings = "  Finding α:\n\n- Keep `literal` text.\n  Preserve indentation.\n"
    result = "Fixed α.\n\n```text\n  exact result\n```\n"

    handoff = _complete(service, findings)
    if runtime == "auto":
        assert handoff.handoff_block is not None
        message = handoff.handoff_block.repeat_communication
        assert message is not None
        assert message["findings"] == findings
        assert "Do not close or replace" in handoff_markdown(handoff.handoff_block)
    branch = _next(service, "positive")
    assert branch.item_name == "fix"
    assert branch.repeat_communication is not None
    assert branch.repeat_communication["findings"] == findings
    assert branch.repeat_communication["session"] == session
    if runtime == "auto":
        assert assignment_token(service, TASK) != original_token
        with pytest.raises(StateError, match="separate worker"):
            service.complete(
                TASK,
                artifact="Manager shortcut.",
                caller_role="manager",
                summary_for_next="Shortcut.",
            )
        with pytest.raises(StateError, match="assignment"):
            service.complete(
                TASK,
                artifact="Stale worker.",
                caller_role="worker",
                assignment=original_token,
                summary_for_next="Stale.",
            )
    branch_token = assignment_token(service, TASK)
    after = _complete(service, result)
    if runtime == "auto":
        assert after.handoff_block is not None
        assert [step.name for step in after.handoff_block.steps] == ["fix"]
        replayed = service.handoff(TASK, assignment=branch_token)
        assert replayed.to_dict() == after.handoff_block.to_dict()

    # Reload before the next round: context and messages are persisted.
    service = WorkflowService(Storage(tmp_path))
    again = _next(service)
    assert again.item_name == "assess"
    message = again.repeat_communication
    assert message is not None
    assert message["session"] == session
    assert message["round"] == 2
    assert message["findings"] == findings
    assert [entry["text"] for entry in message["results"]] == [result]
    assert again.to_dict()["repeat_communication"] == message
    state, plan = service.load(TASK)
    developed = next(
        record
        for item, record in zip(plan.plan.items, state.item_executions, strict=False)
        if item.name == "develop"
    )
    assert developed.attempts == 1
    saved = [record.artifact for record in state.execution_history if record.artifact]
    assert saved
    assert all((tmp_path / path).is_file() for path in saved)

    _complete(service, "No issues remain.")
    terminal = _next(service, "negative")
    assert terminal.item_name == "publish"
    assert terminal.repeat_communication is None
    state, _ = service.load(TASK)
    assessment = next(record for record in state.item_executions if record.repeat_round)
    assert assessment.artifact not in saved
    assert assessment.assessment_outcome == "negative"


def test_a_second_repeat_returns_only_the_latest_rounds_results(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _assess(service)
    for text in ("first result", "second result"):
        _complete(service, "Findings for " + text)
        _next(service, "positive")
        _complete(service, text)
        page = _next(service)
        assert page.repeat_communication is not None
        assert [entry["text"] for entry in page.repeat_communication["results"]] == [
            text
        ]
    assert page.repeat_communication["round"] == 3


def test_the_manager_relay_is_explicit_when_delegation_is_disallowed(
    tmp_path: Path,
) -> None:
    workflow = WORKFLOW.replace(
        "          repeat_until: negative",
        "          repeat_until: negative\n          subagents: false",
    )
    service = _service(tmp_path, workflow)
    page = _assess(service)
    assert page.subagents is False
    text = md.render_instruction(page)
    assert "manager to launch" in text
    assert "exact findings" in text
    assert "same waiting assessment agent" in text
    _complete(service, "Exact findings.\n  With whitespace.\n")
    branch = _next(service, "positive")
    delegated = md.render_instruction(branch)
    assert "separate subagent" in delegated
    assert "forward the findings and result text values" in delegated
    assert "Manager and assessment agent:" in delegated
    assert "Already launched outcome worker: perform your current WW" in delegated
    assert "Do not launch another worker" in delegated


@pytest.mark.parametrize("retry", [False, True])
def test_the_round_limit_keeps_evidence_for_an_operator_decision(
    tmp_path: Path, retry: bool
) -> None:
    service = _service(tmp_path, limit=1)
    _assess(service)
    _complete(service, "Still unresolved.")
    _next(service, "positive")
    stopped = _complete(service, "Tried the fix.")
    assert stopped.operator_reason == "assessment_limit"
    rendered = md.render_instruction(service.instruction(TASK, caller_role="manager"))
    assert "one more round" in rendered
    assert "waive" in rendered
    service = WorkflowService(Storage(tmp_path))
    if retry:
        page = service.next(TASK, retry=True, caller_role="manager")
        if page.item_status == "pending":
            page = _next(service)
        assert page.item_name == "assess"
        assert page.repeat_communication is not None
        assert page.repeat_communication["round"] == 2
    else:
        page = service.next(
            TASK,
            force=True,
            force_reason="Operator accepts the remaining issue.",
            caller_role="manager",
        )
        assert page.item_name == "publish"
        state, _ = service.load(TASK)
        waived = [record for record in state.item_executions if record.repeat_waived]
        assert len(waived) == 1
        assert waived[0].assessment_outcome == "positive"


def test_an_undeclared_nonterminal_outcome_cannot_silently_exit_or_spin(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _assess(service)
    _complete(service, "Uncertain.")
    with pytest.raises(StateError, match="resolve the uncertainty"):
        _next(service, "mixed")
    assert service.instruction(TASK).choosing_outcome_of == "assess"


def test_an_automatic_outcome_runs_normally_before_reassessment(tmp_path: Path) -> None:
    command = (
        "from pathlib import Path; p=Path('fixed.txt'); "
        "p.write_text(p.read_text()+'x' if p.exists() else 'x')"
    )
    workflow = WORKFLOW.replace(
        "            steps:\n              - fix: Fix the exact findings.",
        "            argv: " + json.dumps([sys.executable, "-c", command]),
    )
    service = _service(tmp_path, workflow)
    _assess(service)
    _complete(service, "Run the automatic fix.")
    page = _next(service, "positive")
    if page.item_status == "pending":
        page = _next(service)
    assert page.item_name == "assess"
    assert page.repeat_communication is not None
    assert page.repeat_communication["round"] == 2
    assert (tmp_path / "fixed.txt").read_text() == "x"
    _complete(service, "No more issues.")
    assert _next(service, "negative").item_name == "publish"
    assert (tmp_path / "fixed.txt").read_text() == "x"


@pytest.mark.parametrize(
    ("before", "after", "error"),
    [
        ("repeat_until: negative", "repeat_until: true", "non-empty string"),
        ("repeat_until: negative", "repeat_until: missing", "unknown outcome"),
        (
            "repeat_until: negative",
            "repeat_until: negative\n          artifact: false",
            "artifact",
        ),
        (
            "steps:\n              - fix: Fix the exact findings.",
            "goto: develop",
            "goto or stop_workflow",
        ),
        (
            "steps:\n              - fix: Fix the exact findings.",
            "stop_workflow: true",
            "goto or stop_workflow",
        ),
        (
            "steps:\n              - fix: Fix the exact findings.",
            "steps:\n              - fix: Fix the exact findings.\n"
            "                role: manager",
            "role: worker",
        ),
    ],
)
def test_invalid_repeat_configuration_is_rejected(
    tmp_path: Path, before: str, after: str, error: str
) -> None:
    with pytest.raises(ConfigurationError, match=error):
        _service(tmp_path, WORKFLOW.replace(before, after))


def test_a_custom_terminal_outcome_runs_its_work_then_exits(tmp_path: Path) -> None:
    workflow = WORKFLOW.replace(
        "          repeat_until: negative\n          positive:",
        "          repeat_until: accepted\n          outcomes:\n            positive:",
    ).replace(
        "            steps:\n              - fix: Fix the exact findings.",
        "              steps:\n                - fix: Fix the exact findings.\n"
        "            accepted:\n              steps:\n"
        "                - record: Record acceptance.",
    )
    service = _service(tmp_path, workflow)
    _assess(service)
    _complete(service, "Accepted.")
    assert _next(service, "accepted").item_name == "record"
    _complete(service, "Acceptance recorded.")
    assert _next(service).item_name == "publish"


def test_assessment_hooks_finish_before_reset_and_run_in_each_round(
    tmp_path: Path,
) -> None:
    workflow = WORKFLOW.replace(
        "          repeat_until: negative",
        "          repeat_until: negative\n"
        "          hooks:\n"
        "            before_start:\n"
        "              - shell: echo before >> lifecycle\n"
        "            after_complete:\n"
        "              - shell: echo after >> lifecycle",
    )
    service = _service(tmp_path, workflow)
    page = _assess(service)
    assert page.item_name == "assess"
    assert (tmp_path / "lifecycle").read_text() == "before\n"
    _complete(service, "Issues remain.")
    _next(service, "positive")
    _complete(service, "Fixed them.")
    state, snapshot = service.load(TASK)
    hooks = [
        record
        for record in state.execution_history
        if record.plan_item_id
        in {
            item.id
            for item in snapshot.plan.items
            if item.step == "assess"
            and item.phase in {"before_start", "after_complete"}
        }
    ]
    assert len(hooks) == 2
    assert all(record.status == "completed" for record in hooks)
    page = _next(service)
    assert page.repeat_communication is not None
    assert page.repeat_communication["round"] == 2
    assert (tmp_path / "lifecycle").read_text() == "before\nafter\nbefore\n"
    _complete(service, "No issues remain.")
    assert _next(service, "negative").item_name == "publish"
    assert (tmp_path / "lifecycle").read_text() == "before\nafter\nbefore\nafter\n"


def test_round_local_provided_values_are_cleared_before_reassessment(
    tmp_path: Path,
) -> None:
    workflow = WORKFLOW.replace(
        "              - fix: Fix the exact findings.",
        "              - fix: Fix the exact findings.\n"
        "                variables:\n"
        "                  - round_note: Evidence for this round.",
    )
    service = _service(tmp_path, workflow)
    _assess(service)
    for note in ("first round", "second round"):
        _complete(service, "Issues remain.")
        branch = _next(service, "positive")
        assert "round_note" in [value.name for value in branch.required_values]
        service.complete(
            TASK,
            artifact="Fixed them.",
            variables=(("round_note", note),),
            summary_for_next="Fixed findings.",
            caller_role="worker",
            assignment=assignment_token(service, TASK),
        )
        state, _ = service.load(TASK)
        assert "round_note" not in dict(state.workflow_values)
        page = _next(service)
        assert page.item_name == "assess"
    _complete(service, "No issues remain.")
    assert _next(service, "negative").item_name == "publish"
