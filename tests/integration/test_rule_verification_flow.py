# SPDX-License-Identifier: GPL-3.0-or-later
"""Verifying rules without a command, end to end: rounds and verdicts."""

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
    "shell": "grep -L foo $WW_STEP_CHANGED_FILES || true",
    "assert": ["empty"],
}
PASS = {"id": "develop/1", "status": "judged", "verdict": "pass"}


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _project(root: Path, workflows: str = WORKFLOWS, store: dict | None = None) -> Path:
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


def _report(service: WorkflowService, *results: dict) -> Instruction:
    return service.complete(
        "TASK-1",
        artifact="Findings.",
        rule_results=tuple(json.dumps(result) for result in results),
    )


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
    assert [rule.id for rule in held.verification.rules] == ["develop/1"]
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
    assert "a verdict `pass` or `fail`" in rendered
    assert "#### Where checks run" not in rendered
    assert "#### Existing checks" not in rendered
    assert "--rule-result='<JSON result for develop/1>'" in rendered
    assert "--check-result" not in rendered
    state, snapshot = service.load("TASK-1")
    items = [item.id for item in snapshot.plan.items]
    assert items.index("task:develop:verify:1") < items.index(
        "task:develop:step:step:1"
    )
    assert snapshot.plan_revision == 2
    rendered_json = json.loads(JsonOutputAdapter().render_instruction(held))
    assert rendered_json["verification"]["rules"][0]["id"] == "develop/1"
    assert "directory" not in rendered_json["verification"]
    assert "checks" not in rendered_json["verification"]


def test_results_are_refused_on_an_ordinary_step(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _started(root)
    service.next("TASK-1")

    with pytest.raises(StateError, match="reports a verification"):
        service.complete(
            "TASK-1",
            artifact="Built.",
            summary_for_next="Built.",
            rule_results=("{}",),
        )


def test_a_failing_verdict_is_a_fix_round_counted_with_the_checks(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)
    failing = {
        "id": "develop/1",
        "status": "judged",
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
    again = service.complete("TASK-1", artifact="Fixed.", summary_for_next="Fixed.")
    assert again.verification is not None
    assert [rule.id for rule in again.verification.rules] == ["develop/1"]
    recorded = _report(service, PASS)
    assert recorded.item_name == "check"
    artifact = _artifact(root)
    assert "- `develop/1`: verified pass (by `task:develop:verify:1`)" in artifact
    assert "Completions rejected before this one: 1." in artifact


def test_judged_failures_reach_the_fix_limit(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)
    failing = {
        "id": "develop/1",
        "status": "judged",
        "verdict": "fail",
        "failures": [{"file": "app.py", "what": "bad"}],
    }
    _report(service, failing)
    for _ in range(2):
        service.complete("TASK-1", artifact="Again.", summary_for_next="Again.")
        stopped = _report(service, failing)

    assert stopped.operator_reason == "fix_limit"
    assert stopped.error == "check limit reached: develop/1"


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


@pytest.mark.parametrize(
    "entry",
    [
        {
            "status": "approach_approved",
            "approach": "diff the parser's help output",
            "check": "cli-surface",
        },
        {"status": "approach_proposed", "approach": "a", "check": "x"},
        {"status": "ambiguous", "candidates": ["one reading", "another"]},
    ],
    ids=["approach_approved", "approach_proposed", "ambiguous"],
)
def test_an_old_stores_interim_entry_is_only_judged(
    tmp_path: Path, entry: dict
) -> None:
    text_hash = rule_text_hash(CLI)
    store = {
        "rules": {
            text_hash: {"text": CLI, "interpretation": "No flag changes.", **entry}
        },
        "checks": {},
    }
    root = _project(tmp_path, store=store)
    service = _started(root)

    page = service.next("TASK-1")

    assert [(rule.has_command, rule.check) for rule in page.rules] == [(False, None)]
    assert "  No flag changes." in _markdown(page)
    (root / "app.py").write_text("print(1)\n", encoding="utf-8")
    held = service.complete("TASK-1", artifact="Built.", summary_for_next="Built.")
    assert held.verification is not None
    (rule,) = held.verification.rules
    assert (rule.id, rule.interpretation) == ("develop/1", "No flag changes.")
    page_text = _markdown(held)
    assert "prepare and prove its check" not in page_text
    assert "--check-result" not in page_text
    recorded = _report(service, PASS)
    assert recorded.item_name == "check"
    assert RuleStore(root).load().rules[text_hash].status == entry["status"]


def test_a_verdict_other_than_judged_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)

    with pytest.raises(StateError, match="status must be judged"):
        _report(
            service,
            {
                "id": "develop/1",
                "status": "approach",
                "check": "cli-surface",
                "approach": "diff the parser's help output",
            },
        )
    with pytest.raises(StateError, match="unknown keys: approach, check"):
        _report(
            service,
            {**PASS, "check": "cli-surface", "approach": "diff the help output"},
        )
    state, _ = service.load("TASK-1")
    assert state.failure_kind is None


def test_a_verifier_never_writes_the_store(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service, _ = _developed(root)

    recorded = _report(service, PASS)

    assert recorded.item_name == "check"
    assert not (root / STORE_FILE).exists()


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
        rule_results=(json.dumps(PASS),),
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
        rule_results=(json.dumps({**PASS, "id": "develop/2"}),),
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    after = service.next("TASK-1", caller_role="manager")
    assert after.item_name == "check"
    artifact = _artifact(root)
    assert "- `develop/1`: verified pass (by `task:develop:verify:1`)" in artifact
    assert "- `develop/2`: verified pass (by `task:develop:verify:2`)" in artifact


def test_the_state_with_a_held_completion_survives_a_round_trip(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _developed(root)

    reloaded = WorkflowService(Storage(root))
    state, snapshot = reloaded.load("TASK-1")
    record = state.item_executions[_index(snapshot, "develop")]

    assert record.held_completion is not None
    assert record.rule_resolutions[0].status == "judged"
    verifier = state.item_executions[state.cursor]
    assert [rule.id for rule in verifier.verification] == ["develop/1"]
    run = reloaded.tasks.read_task_record("TASK-1")[0][0]
    assert TaskRunAggregate.from_dict(run.to_dict()) == run


@pytest.mark.parametrize(
    ("resolution", "asked"),
    [("unresolved", "unresolved"), ("pending_operator", "approach_approved")],
)
def test_a_state_the_previous_build_wrote_loads_as_judged(
    tmp_path: Path, resolution: str, asked: str
) -> None:
    # The previous build let verifiers propose checks: a rule could be
    # unresolved or wait on the operator, and records kept the proposals.
    root = _project(tmp_path)
    _developed(root)
    path = root / ".ww/tasks/TASK-1/state.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for item in document["runs"][0]["state"]["item_executions"]:
        for entry in item.get("rule_resolutions", []):
            entry["status"] = resolution
            item["open_proposals"] = [rule_text_hash(CLI)]
        for rule in item.get("verification", []):
            rule.update(state=asked, approach="Grep the CLI.", pending_operator=True)
    path.write_text(json.dumps(document), encoding="utf-8")

    service = WorkflowService(Storage(root))
    state, snapshot = service.load("TASK-1")
    record = state.item_executions[_index(snapshot, "develop")]
    assert [entry.status for entry in record.rule_resolutions] == ["judged"]
    verifier = state.item_executions[state.cursor]
    assert [rule.id for rule in verifier.verification] == ["develop/1"]
    # The held step goes on as a judged one.
    service.next("TASK-1")
    _report(service, PASS)
    assert service.next("TASK-1").item_name == "check"


def test_lint_reports_orphan_store_entries_and_unscriptized_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _converted(CLI)
    store["rules"]["f" * 64] = {"text": "A rule nobody declares.", "status": "rejected"}
    store["rules"]["e" * 64] = {"text": "Undecided.", "status": "ambiguous"}
    store["rules"][rule_text_hash(NAMES)] = {"text": NAMES, "status": "ambiguous"}
    root = _project(tmp_path, TWO_HINTS, store=store)

    assert main(["--root", str(root), "lint"]) == 0

    out = capsys.readouterr().out
    assert "Rule store: 4 rules, 1 check\n" in out
    assert f"Orphan rule {'f' * 12}: A rule nobody declares.\n" in out
    assert f"Orphan rule {'e' * 12}: Undecided.\n" in out
    assert "Pending rule" not in out
    assert "Orphan rule " + rule_text_hash(CLI)[:12] not in out
    # Without the shipped built-ins there is no ``ww-scriptize-rules`` to
    # suggest.
    assert "Warning: 1 rule has no check yet (develop/2).\n" in out


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


def test_next_no_longer_takes_rule_decisions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    _developed(root)

    for flags in (["--approve", "cli-surface"], ["--pick", "abc=1"]):
        with pytest.raises(SystemExit):
            main(["--root", str(root), "next", "TASK-1", *flags])
        assert "unrecognized arguments" in capsys.readouterr().err
