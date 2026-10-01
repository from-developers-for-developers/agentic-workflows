# SPDX-License-Identifier: GPL-3.0-or-later
"""Verifying rules without a command, end to end: rounds, the store, the gate."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.cli import main
from ww.config.rules import rule_text_hash
from ww.errors import StateError
from ww.execution_models import PlanSnapshot, TaskRunAggregate
from ww.instructions import Instruction
from ww.output_adapters.json_adapter import JsonOutputAdapter
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.rule_store import STORE_FILE, RuleStore
from ww.rule_verification import SKIPPED_ROUND
from ww.service import WorkflowService
from ww.storage import Storage

CLI = "Keep the public CLI unchanged."
NAMES = "Name things clearly."
WORKFLOWS = f"""workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        rules:
          - {CLI}
      - check: Check it.
"""
TWO_HINTS = f"""workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        rules:
          - {CLI}
          - text: {NAMES}
            model: opus
      - check: Check it.
"""
FOO_CHECK = {
    "name": "cli-surface",
    "shell": "grep -L foo $WW_STEP_CHANGED_FILES || true",
    "assert": ["empty"],
    "config": [],
    "covers": ["develop/1"],
    "proven": True,
}


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _project(
    root: Path, workflows: str = WORKFLOWS, store: dict | None = None
) -> Path:
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    if store is not None:
        (root / STORE_FILE).write_text(
            json.dumps({"schema_version": 1, **store}), encoding="utf-8"
        )
    _git("init", "-q", "-b", "main", ".", cwd=root)
    _git("config", "user.email", "t@e.st", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)
    return root


def _started(root: Path, runtime: str = "single") -> WorkflowService:
    service = WorkflowService(Storage(root))
    service.start(
        "task", "TASK-1", agent="codex", workflow_runtime=runtime, init_artifact="Do."
    )
    return service


def _developed(
    root: Path, content: str = "print(1)\n"
) -> tuple[WorkflowService, Instruction]:
    """Start, work on ``develop``, and complete it in the single runtime."""
    service = _started(root)
    service.next("TASK-1")
    (root / "app.py").write_text(content, encoding="utf-8")
    held = service.complete("TASK-1", artifact="Built.", summary_for_next="Built.")
    return service, held


def _report(
    service: WorkflowService, *results: dict, checks: tuple = ()
) -> Instruction:
    return service.complete(
        "TASK-1",
        artifact="Findings.",
        rule_results=tuple(json.dumps(result) for result in results),
        check_results=tuple(json.dumps(check) for check in checks),
    )


def _approach(rule: str = "develop/1") -> dict:
    return {
        "id": rule,
        "interpretation": "No command-line flag changes.",
        "status": "approach",
        "check": "cli-surface",
        "approach": "diff the parser's help output",
    }


def _store(root: Path) -> dict:
    return json.loads((root / STORE_FILE).read_text(encoding="utf-8"))


def _artifact(root: Path) -> str:
    return (root / ".ww/tasks/TASK-1/runs/01-task/steps/02-develop.md").read_text(
        encoding="utf-8"
    )


def _markdown(instruction: Instruction) -> str:
    return MarkdownOutputAdapter().render_instruction(instruction)


def _converted(*rules: str) -> dict:
    """A store where every rule in ``rules`` is checked by ``cli-surface``."""
    hashes = [rule_text_hash(text) for text in rules]
    return {
        "rules": {
            text_hash: {"text": text, "status": "converted", "check": "cli-surface"}
            for text_hash, text in zip(hashes, rules, strict=True)
        },
        "checks": {
            "cli-surface": {
                "shell": FOO_CHECK["shell"],
                "assert": FOO_CHECK["assert"],
                "config": [],
                "covers": hashes,
                "proven": True,
                "status": "converted",
            }
        },
    }


def test_completing_holds_the_step_and_opens_a_verification(tmp_path: Path) -> None:
    root = _project(tmp_path)

    service, held = _developed(root)

    assert held.item_name == "develop-verify-1"
    assert held.item_status == "in_progress"
    assert held.completion_held is True
    assert held.verification is not None
    assert [(rule.id, rule.state) for rule in held.verification.rules] == [
        ("develop/1", "unresolved")
    ]
    assert held.verification.files == ("app.py",)
    assert held.verification.diff_command is not None
    assert held.verification.diff_command.startswith("git diff ")
    draft = held.verification.draft_artifact
    assert draft is not None and Path(draft).read_text(encoding="utf-8") == "Built."
    assert not (root / ".ww/tasks/TASK-1/runs/01-task/steps/02-develop.md").exists()
    rendered = _markdown(held)
    assert "Completion accepted by `ww` and held" in rendered
    assert "### Verification" in rendered
    assert "This session also did that step's work" in rendered
    assert "Prefer the ecosystem's own tools" in rendered
    assert "--rule-result='<JSON result for develop/1>'" in rendered
    state, snapshot = service.load("TASK-1")
    items = [item.id for item in snapshot.plan.items]
    assert items.index("task:develop:verify:1") < items.index(
        "task:develop:step:step:1"
    )
    assert snapshot.plan_revision == 2
    rendered_json = json.loads(JsonOutputAdapter().render_instruction(held))
    assert rendered_json["verification"]["rules"][0]["state"] == "unresolved"


def test_results_are_refused_on_an_ordinary_step(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _started(root)
    service.next("TASK-1")

    with pytest.raises(StateError, match="report a verification"):
        service.complete(
            "TASK-1",
            artifact="Built.",
            summary_for_next="Built.",
            rule_results=("{}",),
        )


def test_the_two_stages_and_their_approvals_end_in_a_derived_check(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root, "print(1)  # foo\n")
    text_hash = rule_text_hash(CLI)

    stop = _report(service, _approach())

    assert stop.status == "failed"
    assert stop.operator_reason == "rules_proposed"
    assert [(p.kind, p.key) for p in stop.proposals] == [("approach", text_hash[:12])]
    rendered = _markdown(stop)
    assert "verifiers proposed checks for the step's rules" in rendered
    assert "Approach: diff the parser's help output" in rendered
    assert f"--approve {text_hash[:12]}" in rendered
    assert _store(root)["rules"][text_hash]["status"] == "approach_proposed"
    with pytest.raises(StateError, match="nothing to retry"):
        service.next("TASK-1", retry=True)

    prepare = service.next("TASK-1", approve=(text_hash[:12],))

    assert prepare.item_name == "develop-verify-1"
    assert prepare.verification is not None
    assert prepare.verification.rules[0].state == "approach_approved"
    prepare_page = _markdown(prepare)
    assert "--check-result='<JSON check cli-surface>'" in prepare_page
    assert '`"assert": ["empty"]` when it must print nothing' in prepare_page
    assert '`"assert": [{"equals": "<value>"}]`' in prepare_page
    assert '"operator"' not in prepare_page
    stop = _report(
        service,
        {"id": "develop/1", "status": "approach", "check": "cli-surface"},
        checks=(FOO_CHECK,),
    )
    assert stop.operator_reason == "rules_proposed"
    assert [(p.kind, p.key, p.command) for p in stop.proposals] == [
        ("check", "cli-surface", FOO_CHECK["shell"])
    ]
    assert _store(root)["checks"]["cli-surface"]["status"] == "proposed"
    assert _store(root)["rules"][text_hash]["status"] == "proposed"

    recorded = service.next("TASK-1", approve=("cli-surface",))

    assert recorded.item_name == "check"
    assert "- `develop/1`: passed (check `cli-surface`)" in _artifact(root)
    assert _store(root)["checks"]["cli-surface"]["status"] == "converted"
    assert _store(root)["rules"][text_hash]["status"] == "converted"


def test_an_approved_check_that_fails_on_the_held_completion_sends_it_back(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)
    _report(service, _approach())
    service.next("TASK-1", approve=(rule_text_hash(CLI),))
    _report(
        service,
        {"id": "develop/1", "status": "approach", "check": "cli-surface"},
        checks=(FOO_CHECK,),
    )

    back = service.next("TASK-1", approve=("cli-surface",))

    assert back.item_name == "develop"
    assert back.fix_required is not None
    (failure,) = back.fix_required.failures
    assert (failure.id, failure.output, failure.covers) == (
        "cli-surface",
        "app.py",
        ("develop/1",),
    )
    assert "(check covering `develop/1`)" in _markdown(back)
    assert [rule.check for rule in back.rules] == ["cli-surface"]
    (root / "app.py").write_text("print(1)  # foo\n", encoding="utf-8")
    accepted = service.complete("TASK-1", artifact="Fixed.", summary_for_next="Fixed.")
    assert accepted.item_name == "check"
    assert "Completions rejected before this one: 1." in _artifact(root)


def test_a_failing_verdict_is_a_fix_round_counted_with_the_checks(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)
    failing = {
        "id": "develop/1",
        "status": "not_convertible",
        "reason": "It needs a reviewer.",
        "verdict": "fail",
        "failures": [{"file": "app.py", "line": 1, "what": "prints to stdout"}],
    }

    back = _report(service, failing)

    assert back.item_name == "develop"
    assert back.item_status == "in_progress"
    assert back.fix_required is not None
    assert back.fix_required.attempt == 1
    assert back.fix_required.failures[0].judged is True
    rendered = _markdown(back)
    assert "### `develop/1` (verifier's verdict)" in rendered
    assert "    app.py:1 — prints to stdout" in rendered
    assert _store(root)["rules"][rule_text_hash(CLI)]["status"] == "not_convertible"
    again = service.complete("TASK-1", artifact="Fixed.", summary_for_next="Fixed.")
    assert again.verification is not None
    assert [rule.state for rule in again.verification.rules] == ["judged"]
    recorded = _report(
        service, {"id": "develop/1", "status": "judged", "verdict": "pass"}
    )
    assert recorded.item_name == "check"
    artifact = _artifact(root)
    assert "- `develop/1`: verified pass (by `task:develop:verify:1`)" in artifact
    assert "Completions rejected before this one: 1." in artifact


def test_judged_failures_reach_the_fix_limit(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)
    failing = {
        "id": "develop/1",
        "status": "not_convertible",
        "reason": "Review.",
        "verdict": "fail",
        "failures": [{"file": "app.py", "what": "bad"}],
    }
    _report(service, failing)
    for _ in range(2):
        service.complete("TASK-1", artifact="Again.", summary_for_next="Again.")
        stopped = _report(
            service,
            {
                "id": "develop/1",
                "status": "judged",
                "verdict": "fail",
                "failures": [{"file": "app.py", "what": "bad"}],
            },
        )

    assert stopped.operator_reason == "fix_limit"
    assert stopped.error == "check limit reached: develop/1"


def test_an_ambiguous_rule_is_picked_then_proposed_again(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)
    text_hash = rule_text_hash(CLI)

    stop = _report(
        service,
        {
            "id": "develop/1",
            "status": "ambiguous",
            "candidates": ["no flag changes", "no output changes"],
        },
    )
    assert stop.proposals[0].kind == "ambiguous"
    assert "1. no flag changes" in _markdown(stop)
    assert f"--pick {text_hash[:12]}=<number>" in _markdown(stop)

    again = service.next("TASK-1", picks=((text_hash[:12], 2),))

    assert again.item_name == "develop-verify-1"
    assert again.verification is not None
    rule = again.verification.rules[0]
    assert (rule.state, rule.interpretation) == ("unresolved", "no output changes")
    assert _store(root)["rules"][text_hash]["status"] == "interpreted"


def test_the_operators_approach_replaces_the_verifiers(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)
    _report(service, _approach())

    prepare = service.next(
        "TASK-1", approaches=((rule_text_hash(CLI), "Snapshot `--help` in a test."),)
    )

    assert prepare.verification is not None
    assert prepare.verification.rules[0].approach == "Snapshot `--help` in a test."
    assert "Approved approach: Snapshot `--help` in a test." in _markdown(prepare)


def test_force_rejects_every_pending_proposal_and_the_rule_is_judged(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)
    _report(service, _approach())

    assert "reject every undecided proposal" in service.force_target("TASK-1")
    judged = service.next("TASK-1", force=True, force_reason="Not worth a tool.")

    assert judged.verification is not None
    assert [rule.state for rule in judged.verification.rules] == ["judged"]
    entry = _store(root)["rules"][rule_text_hash(CLI)]
    assert (entry["status"], entry["reason"]) == (
        "rejected",
        "operator: Not worth a tool.",
    )
    recorded = _report(
        service, {"id": "develop/1", "status": "judged", "verdict": "pass"}
    )
    assert recorded.item_name == "check"


def test_a_converted_rule_is_checked_at_once_and_reported_under_its_check(
    tmp_path: Path,
) -> None:
    same_hints = TWO_HINTS.replace("\n            model: opus", "")
    root = _project(tmp_path, same_hints, store=_converted(CLI, NAMES))
    service = _started(root)
    page = service.next("TASK-1")
    assert [(rule.id, rule.has_command, rule.check) for rule in page.rules] == [
        ("develop/1", True, "cli-surface"),
        ("develop/2", True, "cli-surface"),
    ]
    assert "Checked automatically when you complete: `develop/1`, `develop/2`." in (
        _markdown(page)
    )
    (root / "app.py").write_text("print(1)\n", encoding="utf-8")

    back = service.complete("TASK-1", artifact="Built.", summary_for_next="Built.")

    assert back.fix_required is not None
    (failure,) = back.fix_required.failures
    assert failure.id == "cli-surface"
    assert failure.covers == ("develop/1", "develop/2")
    assert f"- `develop/1`: {CLI}" in (failure.text or "")
    assert f"- `develop/2`: {NAMES}" in (failure.text or "")


def test_a_pending_proposal_elsewhere_is_judged_for_now(tmp_path: Path) -> None:
    text_hash = rule_text_hash(CLI)
    root = _project(
        tmp_path,
        store={
            "rules": {
                text_hash: {
                    "text": CLI,
                    "status": "approach_proposed",
                    "approach": "a",
                    "check": "x",
                    "interpretation": "No flag changes.",
                }
            },
            "checks": {},
        },
    )
    service = _started(root)

    page = service.next("TASK-1")

    assert page.rules[0].pending_operator is True
    rendered = _markdown(page)
    assert "  No flag changes." in rendered
    assert "This rule has a pending proposal; the operator has not yet decided." in (
        rendered
    )
    held = service.complete("TASK-1", artifact="Built.", summary_for_next="Built.")
    assert held.verification is not None
    assert held.verification.rules[0].state == "judged"
    assert held.verification.rules[0].pending_operator is True


def test_each_hint_set_gets_its_own_verifier_in_the_auto_runtime(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, TWO_HINTS)
    service = _started(root, runtime="auto")
    service.next("TASK-1", caller_role="manager")

    returned = service.complete(
        "TASK-1",
        artifact="Built.",
        summary_for_next="Built.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )

    assert returned.control == "handoff_manager"
    assert returned.completion_held is True
    state, snapshot = service.load("TASK-1")
    verifiers = [item for item in snapshot.plan.items if item.verifies is not None]
    assert [(item.id, item.requested_model) for item in verifiers] == [
        ("task:develop:verify:1", "auto"),
        ("task:develop:verify:2", "opus"),
    ]
    first = service.next("TASK-1", caller_role="manager")
    assert first.item_name == "develop-verify-1"
    assert first.assignment_items == ("develop-verify-1",)
    page = service.instruction(
        "TASK-1", caller_role="worker", assignment=assignment_token(service, "TASK-1")
    )
    assert page.verification is not None
    assert [rule.id for rule in page.verification.rules] == ["develop/1"]
    assert "this session also did" not in _markdown(page)
    done = service.complete(
        "TASK-1",
        artifact="Findings.",
        rule_results=(
            json.dumps(
                {
                    "id": "develop/1",
                    "status": "not_convertible",
                    "reason": "Review.",
                    "verdict": "pass",
                }
            ),
        ),
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    assert done.control == "handoff_manager"
    second = service.next("TASK-1", caller_role="manager")
    assert second.item_name == "develop-verify-2"
    assert second.requested_model == "opus"
    service.complete(
        "TASK-1",
        artifact="Findings.",
        rule_results=(
            json.dumps(
                {
                    "id": "develop/2",
                    "status": "not_convertible",
                    "reason": "Taste.",
                    "verdict": "pass",
                }
            ),
        ),
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    after = service.next("TASK-1", caller_role="manager")
    assert after.item_name == "check"
    artifact = _artifact(root)
    assert "- `develop/1`: verified pass (by `task:develop:verify:1`)" in artifact
    assert "- `develop/2`: verified pass (by `task:develop:verify:2`)" in artifact


def _stage_a_store(root: Path, runtime: str) -> dict:
    service = _started(root, runtime=runtime)
    role = "manager" if runtime == "auto" else None
    worker = "worker" if runtime == "auto" else None
    service.next("TASK-1", caller_role=role)
    service.complete(
        "TASK-1",
        artifact="Built.",
        summary_for_next="Built.",
        caller_role=worker,
        assignment=assignment_token(service, "TASK-1"),
    )
    if runtime == "auto":
        service.next("TASK-1", caller_role="manager")
    service.complete(
        "TASK-1",
        artifact="Findings.",
        rule_results=(json.dumps(_approach()),),
        caller_role=worker,
        assignment=assignment_token(service, "TASK-1"),
    )
    return _store(root)


def test_single_and_auto_write_the_same_store_entries(tmp_path: Path) -> None:
    (tmp_path / "single").mkdir()
    (tmp_path / "auto").mkdir()
    single = _stage_a_store(_project(tmp_path / "single"), "single")
    auto = _stage_a_store(_project(tmp_path / "auto"), "auto")

    assert single == auto


def test_the_state_with_a_held_completion_survives_a_round_trip(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)
    _report(service, _approach())

    reloaded = WorkflowService(Storage(root))
    state, snapshot = reloaded.load("TASK-1")
    record = state.item_executions[state.cursor]

    assert state.failure_kind == "rules_proposed"
    assert record.held_completion is not None
    assert record.open_proposals == (rule_text_hash(CLI),)
    assert record.rule_resolutions[0].status == "unresolved"
    run = reloaded.tasks.read_task_record("TASK-1")[0][0]
    assert TaskRunAggregate.from_dict(run.to_dict()) == run


def test_lint_reports_orphan_and_pending_store_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _converted(CLI)
    store["rules"]["f" * 64] = {"text": "A rule nobody declares.", "status": "rejected"}
    store["rules"]["e" * 64] = {"text": "Undecided.", "status": "ambiguous"}
    root = _project(tmp_path, store=store)

    assert main(["--root", str(root), "lint"]) == 0

    out = capsys.readouterr().out
    assert "Rule store: 3 rules, 1 check\n" in out
    assert f"Orphan rule {'f' * 12}: A rule nobody declares.\n" in out
    assert f"Orphan rule {'e' * 12}: Undecided.\n" in out
    assert f"Pending rule {'e' * 12} (ambiguous): Undecided.\n" in out
    assert "Orphan rule " + rule_text_hash(CLI)[:12] not in out


def test_the_cli_shows_an_approval_in_full_and_asks_first(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)
    _report(service, _approach())
    service.next("TASK-1", approve=(rule_text_hash(CLI),))
    _report(
        service,
        {"id": "develop/1", "status": "approach", "check": "cli-surface"},
        checks=(FOO_CHECK,),
    )
    answers = iter(["n"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    code = main(["--root", str(root), "next", "TASK-1", "--approve", "cli-surface"])

    assert code == 1
    error = capsys.readouterr().err
    assert f"check cli-surface: {FOO_CHECK['shell']}" in error
    assert "Approval cancelled" in error
    assert RuleStore(root).load().checks["cli-surface"].status == "proposed"


def test_the_cli_refuses_a_malformed_rule_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    _developed(root)

    code = main(
        [
            "--root",
            str(root),
            "complete",
            "TASK-1",
            "--artifact",
            "Findings.",
            "--rule-result",
            "{not json",
        ]
    )

    assert code == 1
    assert "--rule-result is not valid JSON" in capsys.readouterr().err


PASS = {
    "id": "develop/1",
    "status": "not_convertible",
    "reason": "A matter of review.",
    "verdict": "pass",
}


def _index(snapshot: PlanSnapshot, name: str) -> int:
    return next(
        index
        for index, item in enumerate(snapshot.plan.items)
        if item.name == name and item.phase == "step"
    )


def test_an_interruption_before_the_held_completion_is_recorded_recovers_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)

    def interrupted(*arguments: object, **options: object) -> Instruction:
        raise RuntimeError("the process died")

    # The last verifier's results are committed; the process dies before the
    # held completion is recorded.
    monkeypatch.setattr(service, "_replay_held", interrupted)
    with pytest.raises(RuntimeError, match="the process died"):
        _report(service, PASS)
    monkeypatch.undo()
    state, snapshot = service.load("TASK-1")
    develop = state.item_executions[_index(snapshot, "develop")]
    assert develop.held_completion is not None
    assert develop.status != "completed"
    assert not (root / ".ww/tasks/TASK-1/runs/01-task/steps/02-develop.md").exists()

    resumed = service.next("TASK-1")

    assert resumed.item_name == "check"
    assert "- `develop/1`: verified pass (by `task:develop:verify:1`)" in (
        _artifact(root)
    )
    state, snapshot = service.load("TASK-1")
    develop = state.item_executions[_index(snapshot, "develop")]
    assert develop.status == "completed"
    assert develop.held_completion is None
    assert snapshot.plan_revision == 2
    assert [item.id for item in snapshot.plan.items if item.verifies] == [
        "task:develop:verify:1"
    ]
    again = service.next("TASK-1")
    assert again.item_name == "check"


LOOPED = f"""workflows:
  - name: task
    steps:
      - rounds: ~
        loop:
          - name: develop
            description: Develop it.
            rules:
              - {CLI}
          - review: Review it.
            break: Nothing is left to do.
"""


def test_a_loop_round_skips_the_verifier_its_reset_left_idle(tmp_path: Path) -> None:
    root = _project(tmp_path, LOOPED)
    service, held = _developed(root)
    assert held.item_name == "develop-verify-1"
    review = _report(service, PASS)
    assert review.item_name == "review"
    service.complete("TASK-1", artifact="Another round.", summary_for_next="More.")

    again = service.next("TASK-1")

    assert again.item_name == "develop"
    assert again.item_status == "in_progress"
    state, snapshot = service.load("TASK-1")
    index = next(
        index
        for index, item in enumerate(snapshot.plan.items)
        if item.verifies is not None
    )
    record = state.item_executions[index]
    assert record.verification == ()
    assert (record.status, record.result) == ("completed", SKIPPED_ROUND)


AFTER_HOOK = f"""workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        rules:
          - {CLI}
        hooks:
          after_complete:
            - record-notes: Record what you learned.
      - check: Check it.
"""


def test_a_replayed_completion_ends_the_verifiers_assignment(tmp_path: Path) -> None:
    root = _project(tmp_path, AFTER_HOOK)
    service = _started(root, runtime="auto")
    service.next("TASK-1", caller_role="manager")
    (root / "app.py").write_text("print(1)\n", encoding="utf-8")
    service.complete(
        "TASK-1",
        artifact="Built.",
        summary_for_next="Built.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    verifier = service.next("TASK-1", caller_role="manager")
    assert verifier.item_name == "develop-verify-1"

    done = service.complete(
        "TASK-1",
        artifact="Findings.",
        rule_results=(json.dumps(PASS),),
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )

    assert done.control == "handoff_manager"
    assert done.next_role == "manager"
    assert "This assignment is complete." in _markdown(done)
    state, snapshot = service.load("TASK-1")
    assert state.assignment_item_id is None
    assert state.active_item_id is None
    assert snapshot.plan.items[state.cursor].name == "record-notes"
    assert "- `develop/1`: verified pass" in _artifact(root)
    hook = service.next("TASK-1", caller_role="manager")
    assert hook.item_name == "record-notes"
    assert hook.item_status == "in_progress"
    assert hook.assignment_items == ("record-notes",)


def test_yes_approves_without_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root, "print(1)  # foo\n")
    _report(service, _approach())

    def no_prompt(prompt: str) -> str:
        raise AssertionError("--yes must not ask")

    monkeypatch.setattr("builtins.input", no_prompt)

    code = main(
        [
            "--root",
            str(root),
            "next",
            "TASK-1",
            "--approve",
            rule_text_hash(CLI)[:12],
            "--yes",
        ]
    )

    assert code == 0
    error = capsys.readouterr().err
    assert "approach for rule" in error
    assert "Approved with --yes." in error
    assert _store(root)["rules"][rule_text_hash(CLI)]["status"] == "approach_approved"


# rules.approval ------------------------------------------------------------------

JUDGED_PASS = {"id": "develop/1", "status": "judged", "verdict": "pass"}
STAGE_B = {"id": "develop/1", "status": "approach", "check": "cli-surface"}


def _approval(root: Path, value: str) -> None:
    (root / "ww.json").write_text(
        json.dumps({"rules": {"approval": value}}), encoding="utf-8"
    )


def _active(service: WorkflowService, page: Instruction) -> Instruction:
    """The page of the agent item now in progress, starting it if needed."""
    if page.item_status == "in_progress":
        return page
    return service.next("TASK-1")


def _finish(service: WorkflowService, page: Instruction) -> Instruction:
    """Complete ``check`` and the workflow summary; the completion page."""
    page = _active(service, page)
    assert page.item_name == "check"
    page = _active(
        service,
        service.complete("TASK-1", artifact="Checked.", summary_for_next="Checked."),
    )
    assert page.item_name == "update-workflow-summary"
    service.complete(
        "TASK-1", artifact="Summary.", variables=(("summary", "Did it."),)
    )
    return service.instruction("TASK-1")


def _summary_artifact(root: Path, service: WorkflowService) -> str:
    state, snapshot = service.load("TASK-1")
    index = next(
        index for index, item in enumerate(snapshot.plan.items) if item.summary
    )
    reference = state.item_executions[index].artifact
    assert reference is not None
    return (root / reference).read_text(encoding="utf-8")


def test_the_operator_path_records_the_operator_as_approver(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root, "print(1)  # foo\n")
    text_hash = rule_text_hash(CLI)
    _report(service, _approach())
    service.next("TASK-1", approve=(text_hash[:12],))
    _report(service, STAGE_B, checks=(FOO_CHECK,))

    service.next("TASK-1", approve=("cli-surface",))

    check = _store(root)["checks"]["cli-surface"]
    rule = _store(root)["rules"][text_hash]
    run = "TASK-1/01-task"
    assert (check["approved_by"], check["approved_in"]) == ("operator", run)
    assert (rule["approved_by"], rule["approved_in"]) == ("operator", run)
    assert rule["proposed_run"] == "TASK-1/01-task"


def test_check_approval_approves_the_approach_and_stops_for_the_check(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _approval(root, "check")
    service, _ = _developed(root, "print(1)  # foo\n")
    text_hash = rule_text_hash(CLI)

    prepare = _active(service, _report(service, _approach()))

    assert prepare.item_name == "develop-verify-1"
    assert prepare.verification is not None
    assert prepare.verification.rules[0].state == "approach_approved"
    entry = _store(root)["rules"][text_hash]
    assert (entry["status"], entry["approved_by"]) == ("approach_approved", "auto")
    stop = _report(service, STAGE_B, checks=(FOO_CHECK,))
    assert stop.operator_reason == "rules_proposed"
    assert [(p.kind, p.key) for p in stop.proposals] == [("check", "cli-surface")]

    recorded = service.next("TASK-1", approve=("cli-surface",))

    assert recorded.item_name == "check"
    assert _store(root)["checks"]["cli-surface"]["approved_by"] == "operator"


def test_check_approval_still_stops_for_an_ambiguous_rule(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _approval(root, "check")
    service, _ = _developed(root)

    stop = _report(
        service,
        {"id": "develop/1", "status": "ambiguous", "candidates": ["a", "b"]},
    )

    assert stop.operator_reason == "rules_proposed"
    assert stop.proposals[0].kind == "ambiguous"


def test_auto_approval_converts_a_proven_check_without_a_stop(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _approval(root, "auto")
    service, _ = _developed(root, "print(1)  # foo\n")
    text_hash = rule_text_hash(CLI)

    prepare = _active(service, _report(service, _approach()))
    assert prepare.verification is not None
    assert prepare.verification.rules[0].state == "approach_approved"
    recorded = _report(service, STAGE_B, checks=(FOO_CHECK,))

    assert recorded.status != "failed"
    assert "- `develop/1`: passed (check `cli-surface`)" in _artifact(root)
    check = _store(root)["checks"]["cli-surface"]
    assert (check["status"], check["approved_by"], check["approved_in"]) == (
        "converted",
        "auto",
        "TASK-1/01-task",
    )
    assert _store(root)["rules"][text_hash]["status"] == "converted"
    done = _finish(service, recorded)
    assert done.status == "completed"
    assert [check.name for check in done.rule_conversions.converted] == [
        "cli-surface"
    ]
    converted = done.rule_conversions.converted[0]
    assert converted.rules[0].id == "develop/1"
    assert converted.approved_by == "auto"
    assert converted.undo is not None and converted.undo.endswith(
        "rules revoke cli-surface"
    )
    page = _markdown(done)
    assert "### Rules converted in this run" in page
    assert "- Check `cli-surface` (approved by auto)" in page
    assert f"  - Rule `develop/1`: {CLI}" in page
    assert "  - Proven: yes" in page
    assert "rules revoke cli-surface`" in page
    summary = _summary_artifact(root, service)
    assert summary.startswith("Summary.")
    assert "## Rules converted in this run" in summary
    assert "rules revoke cli-surface" in summary
    rendered_json = json.loads(JsonOutputAdapter().render_instruction(done))
    assert rendered_json["rule_conversions"]["converted"][0]["name"] == "cli-surface"


def test_auto_approval_leaves_an_unproven_check_undecided_and_judges(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _approval(root, "auto")
    service, _ = _developed(root)
    _active(service, _report(service, _approach()))

    judged = _active(
        service,
        _report(service, STAGE_B, checks=({**FOO_CHECK, "proven": False},)),
    )

    assert judged.status != "failed"
    assert judged.verification is not None
    rule = judged.verification.rules[0]
    assert (rule.state, rule.pending_operator) == ("judged", True)
    assert _store(root)["checks"]["cli-surface"]["status"] == "proposed"
    recorded = _report(service, JUDGED_PASS)
    assert recorded.status != "failed"
    done = _finish(service, recorded)
    assert done.rule_conversions.converted == ()
    assert [(p.kind, p.key, p.proven) for p in done.rule_conversions.undecided] == [
        ("check", "cli-surface", False)
    ]
    page = _markdown(done)
    assert "No check was approved in this run." in page
    assert "- Check `cli-surface` (proposed)" in page
    assert "## Rules converted in this run" in _summary_artifact(root, service)


def test_auto_approval_judges_an_ambiguous_rule_without_a_stop(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _approval(root, "auto")
    service, _ = _developed(root)

    judged = _active(
        service,
        _report(
            service,
            {"id": "develop/1", "status": "ambiguous", "candidates": ["a", "b"]},
        ),
    )

    assert judged.status != "failed"
    assert judged.verification is not None
    assert judged.verification.rules[0].state == "judged"
    assert _store(root)["rules"][rule_text_hash(CLI)]["status"] == "ambiguous"
    recorded = _report(service, JUDGED_PASS)
    assert recorded.status != "failed"
    done = _finish(service, recorded)
    assert [(p.kind, p.status) for p in done.rule_conversions.undecided] == [
        ("rule", "ambiguous")
    ]


def test_nothing_converted_leaves_no_section(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)

    recorded = _report(service, PASS)

    done = _finish(service, recorded)
    assert not done.rule_conversions
    assert "Rules converted in this run" not in _markdown(done)
    assert "Rules converted" not in _summary_artifact(root, service)
