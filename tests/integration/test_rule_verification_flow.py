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
from ww.rule_stats import RuleFailure
from ww.rule_store import STORE_FILE, RuleStore
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
# A group of judged rules scoped by globs and strings, on ``develop`` alone.
SCOPED = """rules:
  docs:
    rules: [rules/docs/]
    steps: [develop]
workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
      - check: Check it.
"""
# The scoped group and the unscoped CLI rule together.
MIXED = SCOPED.replace(
    "        description: Develop it.\n",
    f"        description: Develop it.\n        rules:\n          - {CLI}\n",
)
MARKDOWN_RULE = '---\npaths: ["*.md"]\n---\nStart every Markdown file with a heading.\n'
PRINTING_RULE = (
    '---\npaths: ["*.py"]\ncontains_in_file: [print]\n---\n'
    "Print nothing in library code.\n"
)
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


def _scoped_project(root: Path, workflows: str = SCOPED, *rules: str) -> Path:
    """A project with the ``docs`` group holding ``rules``, Markdown first."""
    group = root / "rules/docs"
    group.mkdir(parents=True)
    (group / "markdown.md").write_text(MARKDOWN_RULE, encoding="utf-8")
    if PRINTING_RULE in rules:
        (group / "printing.md").write_text(PRINTING_RULE, encoding="utf-8")
    return _project(root, workflows)


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


def test_a_scoped_rule_outside_the_change_set_needs_no_verifier(
    tmp_path: Path,
) -> None:
    root = _scoped_project(tmp_path)

    service, page = _developed(root)

    # Nothing is held: the completion is recorded at once, as when every
    # rule passed, and the artifact says why the rule was not judged.
    assert page.item_name == "check"
    assert not page.completion_held
    assert "- `docs/markdown`: not applicable (no changed file in scope)" in (
        _artifact(root)
    )
    state, snapshot = service.load("TASK-1")
    assert all(item.verifies is None for item in snapshot.plan.items)
    record = state.item_executions[_index(snapshot, "develop")]
    assert record.rules_not_applicable == ("docs/markdown",)
    assert record.held_completion is None
    markdown = service.rule_stats.load()["docs/markdown"]
    assert (markdown.applied, markdown.judged, markdown.not_applicable) == (0, 0, 1)


def test_a_step_that_changed_nothing_skips_every_scoped_rule(
    tmp_path: Path,
) -> None:
    root = _scoped_project(tmp_path, SCOPED, PRINTING_RULE)
    service = _started(root)
    service.next("TASK-1")

    page = service.complete("TASK-1", artifact="Nothing.", summary_for_next="None.")

    assert page.item_name == "check"
    artifact = _artifact(root)
    assert "- `docs/markdown`: not applicable (no changed file in scope)" in artifact
    assert "- `docs/printing`: not applicable (no changed file in scope)" in artifact


def test_an_unscoped_rule_is_verified_even_when_nothing_changed(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service = _started(root)
    service.next("TASK-1")

    held = service.complete("TASK-1", artifact="Nothing.", summary_for_next="None.")

    assert held.item_name == "develop-verify-1"
    assert held.completion_held is True
    assert "The step changed no files." in _markdown(held)


def test_one_verifier_judges_only_the_applicable_rules(tmp_path: Path) -> None:
    root = _scoped_project(tmp_path, MIXED, PRINTING_RULE)

    service, held = _developed(root)

    assert held.item_name == "develop-verify-1"
    assert held.verification is not None
    assert {rule.id for rule in held.verification.rules} == {
        "develop/1",
        "docs/printing",
    }
    rendered = _markdown(held)
    assert '  Scope: *.py; in file "print"\n' in rendered
    assert "Changed files in its scope" not in rendered
    assert "- `docs/markdown`" not in rendered
    assert "--rule-result='<JSON result for develop/1>'" in rendered
    assert "--rule-result='<JSON result for docs/printing>'" in rendered
    state, snapshot = service.load("TASK-1")
    assert len([item for item in snapshot.plan.items if item.verifies]) == 1
    record = state.item_executions[_index(snapshot, "develop")]
    assert record.rules_not_applicable == ("docs/markdown",)

    accepted = _report(
        service, PASS, {"id": "docs/printing", "status": "judged", "verdict": "pass"}
    )

    assert accepted.item_name == "check"
    artifact = _artifact(root)
    assert "- `develop/1`: verified pass (by `task:develop:verify:1`)" in artifact
    assert "- `docs/markdown`: not applicable (no changed file in scope)" in artifact
    assert "- `docs/printing`: verified pass (by `task:develop:verify:1`)" in artifact


def test_the_verifier_page_shows_each_rules_scope_and_its_files(
    tmp_path: Path,
) -> None:
    root = _scoped_project(tmp_path, MIXED, PRINTING_RULE)
    service = _started(root)
    service.next("TASK-1")
    (root / "app.py").write_text("print(1)\n", encoding="utf-8")
    (root / "README.md").write_text("# Seed\n", encoding="utf-8")

    held = service.complete("TASK-1", artifact="Built.", summary_for_next="Built.")

    assert held.verification is not None
    assert held.verification.files == ("README.md", "app.py")
    by_id = {rule.id: rule for rule in held.verification.rules}
    assert by_id["docs/markdown"].files == ("README.md",)
    assert by_id["docs/printing"].files == ("app.py",)
    assert by_id["develop/1"].files == ()
    rendered = _markdown(held)
    assert rendered.count("Files the step changed (2):") == 1
    assert "  Scope: *.md\n  Changed files in its scope (1): `README.md`\n" in rendered
    assert (
        '  Scope: *.py; in file "print"\n  Changed files in its scope (1): `app.py`\n'
    ) in rendered
    payload = json.loads(JsonOutputAdapter().render_instruction(held))
    printing = next(
        rule
        for rule in payload["verification"]["rules"]
        if rule["id"] == "docs/printing"
    )
    assert (
        printing["paths"],
        printing["contains_in_file"],
        printing["contains_in_diff"],
        printing["files"],
    ) == (["*.py"], ["print"], [], ["app.py"])


def test_a_rule_left_out_in_one_round_is_judged_when_a_fix_reaches_its_files(
    tmp_path: Path,
) -> None:
    root = _scoped_project(tmp_path, MIXED)
    service, held = _developed(root)
    assert [rule.id for rule in held.verification.rules] == ["develop/1"]  # type: ignore[union-attr]

    _report(
        service,
        {
            "id": "develop/1",
            "status": "judged",
            "verdict": "fail",
            "failures": [{"file": "app.py", "what": "Renames the flag."}],
        },
    )
    service.next("TASK-1")
    (root / "README.md").write_text("# Fixed\n", encoding="utf-8")
    held = service.complete("TASK-1", artifact="Fixed.", summary_for_next="Fixed.")

    assert held.verification is not None
    assert {rule.id for rule in held.verification.rules} == {
        "develop/1",
        "docs/markdown",
    }
    state, snapshot = service.load("TASK-1")
    record = state.item_executions[_index(snapshot, "develop")]
    assert record.rules_not_applicable == ()


def test_lint_hints_at_judged_rules_without_a_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _scoped_project(tmp_path, MIXED, PRINTING_RULE)

    assert main(["--root", str(root), "lint"]) == 0
    out = capsys.readouterr().out

    assert (
        "Hint: 1 judged rule is verified on every step (develop/1); add paths, "
        "contains_in_file or contains_in_diff to narrow it.\n"
    ) in out
    assert "docs/markdown)" not in out and "docs/printing)" not in out

    # A converted rule is checked, not judged: nothing is left to narrow.
    (root / STORE_FILE).write_text(
        json.dumps({"schema_version": 1, **_converted(CLI)}), encoding="utf-8"
    )
    assert main(["--root", str(root), "lint"]) == 0
    assert "Hint:" not in capsys.readouterr().out


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
    judged = service.rule_stats.load()["develop/1"]
    assert (judged.applied, judged.judged, judged.judged_failures) == (2, 2, 1)
    assert judged.last_failure == RuleFailure("TASK-1", "develop", "verdict")


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
    path = root / ".ww/tasks/TASK-1/runs/01-task/state.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for item in document["run"]["state"]["item_executions"]:
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
    assert (
        "Hint: 1 judged rule is verified on every step (develop/2); add paths, "
        "contains_in_file or contains_in_diff to narrow it.\n"
    ) in out


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


def test_the_handoff_shows_the_held_re_check_after_a_rejection(
    tmp_path: Path,
) -> None:
    # ``develop/1`` is checked by ``cli-surface`` at once; ``develop/2`` is judged.
    root = _project(tmp_path, TWO_HINTS, store=_converted(CLI))
    service = _started(root, runtime="auto")
    service.next("TASK-1", caller_role="manager")
    token = assignment_token(service, "TASK-1")
    (root / "app.py").write_text("print(1)\n", encoding="utf-8")
    rejected = service.complete(
        "TASK-1",
        artifact="Built.",
        summary_for_next="Built.",
        caller_role="worker",
        assignment=token,
    )
    assert rejected.fix_required is not None
    assert rejected.handoff_block is None
    (root / "app.py").write_text("foo\n", encoding="utf-8")

    held = service.complete(
        "TASK-1",
        artifact="Fixed.",
        summary_for_next="Fixed.",
        caller_role="worker",
        assignment=token,
    )

    assert held.completion_held is True
    assert held.handoff_block is not None
    (develop,) = held.handoff_block.steps
    assert develop.outcome == "held for verification"
    assert develop.checks == (("cli-surface", "passed"),)
    assert develop.checks_note == "checks passed; rules being verified"
    assert develop.fix_rounds == 1
    assert "checks: cli-surface passed (checks passed; rules being verified)" in (
        _markdown(held)
    )
    assert service.handoff("TASK-1", assignment=token) == held.handoff_block

    # A failing verdict reopens the step; the block now shows that attempt.
    assert service.next("TASK-1", caller_role="manager").item_name == (
        "develop-verify-1"
    )
    failing = {
        "id": "develop/2",
        "status": "judged",
        "verdict": "fail",
        "failures": [{"file": "app.py", "what": "unclear name"}],
    }
    service.complete(
        "TASK-1",
        artifact="Findings.",
        rule_results=(json.dumps(failing),),
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    again = service.handoff("TASK-1", assignment=token)
    (develop,) = again.steps
    assert develop.outcome == "not completed"
    assert dict(develop.checks)["develop/2"] == "failed"
    assert develop.checks_note == (
        "last rejected attempt, re-checked on the next complete"
    )


def test_next_no_longer_takes_rule_decisions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    _developed(root)

    for flags in (["--approve", "cli-surface"], ["--pick", "abc=1"]):
        with pytest.raises(SystemExit):
            main(["--root", str(root), "next", "TASK-1", *flags])
        assert "unrecognized arguments" in capsys.readouterr().err
