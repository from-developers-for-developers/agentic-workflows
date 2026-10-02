# SPDX-License-Identifier: GPL-3.0-or-later
"""The agent's and the operator's rule commands: check, dispute, rule, rules."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.cli import main
from ww.config.rules import rule_text_hash
from ww.errors import StateError
from ww.execution_models import ExecutionState, PlanItemExecution, TaskRunAggregate
from ww.instructions import Instruction
from ww.output_adapters.json_adapter import JsonOutputAdapter
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.rule_disputes import DISPUTES_FILE, DisputeLog
from ww.rule_store import STORE_FILE, RuleStore
from ww.service import WorkflowService
from ww.storage import Storage

JUDGED = "Keep the public CLI unchanged."
# A judged rule, a rule with a command scoped to Markdown, and a ``fix`` hook.
WORKFLOWS = f"""rules:
  docs: [rules/docs/]
workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        rules:
          - {JUDGED}
        hooks:
          before_complete:
            - argv: [sh, -c, "test ! -e broken"]
              on_failure: fix
      - check: Check it.
"""
HEADER_RULE = """---
paths: ["*.md"]
check:
  shell: grep -L foo $WW_STEP_CHANGED_FILES || true
  assert: [empty]
---
Include "foo" in every Markdown file you change.

A marker keeps the documentation searchable.
"""
PASS = {
    "id": "develop/1",
    "status": "not_convertible",
    "reason": "A matter of review.",
    "verdict": "pass",
}


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _project(root: Path, workflows: str = WORKFLOWS) -> Path:
    rules = root / "rules/docs"
    rules.mkdir(parents=True)
    (rules / "header.md").write_text(HEADER_RULE, encoding="utf-8")
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    (root / "README.md").write_text("foo seed\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", ".", cwd=root)
    _git("config", "user.email", "t@e.st", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)
    return root


def _develop(root: Path, runtime: str = "single") -> WorkflowService:
    service = WorkflowService(Storage(root))
    service.start(
        "task", "TASK-1", agent="codex", workflow_runtime=runtime, init_artifact="Do."
    )
    service.next("TASK-1", caller_role="manager" if runtime == "auto" else None)
    return service


def _violate(root: Path) -> None:
    (root / "notes.md").write_text("no marker here\n", encoding="utf-8")
    (root / "broken").write_text("", encoding="utf-8")


def _complete(service: WorkflowService, artifact: str = "Done.") -> Instruction:
    return service.complete("TASK-1", artifact=artifact, summary_for_next="Done.")


def _record(service: WorkflowService) -> tuple[ExecutionState, PlanItemExecution]:
    state, snapshot = service.load("TASK-1")
    index = next(
        index
        for index, item in enumerate(snapshot.plan.items)
        if item.name == "develop" and item.phase == "step"
    )
    return state, state.item_executions[index]


def _markdown(instruction: Instruction) -> str:
    return MarkdownOutputAdapter().render_instruction(instruction)


def _artifact(root: Path) -> str:
    return (root / ".ww/tasks/TASK-1/runs/01-task/steps/02-develop.md").read_text(
        encoding="utf-8"
    )


def _rejected(root: Path) -> WorkflowService:
    service = _develop(root)
    _violate(root)
    rejected = _complete(service, "First try.")
    assert rejected.fix_required is not None
    return service


# The page names the commands --------------------------------------------------


def test_the_step_page_names_check_and_rule(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = WorkflowService(Storage(root))
    service.start("task", "TASK-1", agent="codex", init_artifact="Do.")

    rendered = _markdown(service.next("TASK-1"))

    assert (
        "ww checks them when you complete. Preview with `./ww check TASK-1`; "
        "read a full rule with `./ww rule TASK-1 <id>`." in rendered
    )


def test_the_fix_page_offers_check_and_dispute(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _develop(root)
    _violate(root)

    rendered = _markdown(_complete(service))

    assert "`./ww check TASK-1` previews the checks" in rendered
    assert (
        "./ww dispute TASK-1 --role worker --rule <id> "
        '--reason "<why the check is wrong here>"' in rendered
    )


# ww check ---------------------------------------------------------------------


def test_check_runs_the_checks_and_records_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _develop(root)
    _violate(root)
    before = _record(service)

    preview = service.check("TASK-1")

    assert [failure.id for failure in preview.failures] == ["docs/header", "develop/sh"]
    assert preview.failures[0].output == "notes.md"
    assert preview.failures[0].text is not None
    assert preview.failures[0].text.startswith('Include "foo"')
    assert preview.checks == 2
    assert preview.judged == ("develop/1",)
    assert _record(service) == before
    assert not (root / ".ww/tasks/TASK-1/runs/01-task/command-output").exists()
    assert not any("check" in entry for entry in service.artifacts("TASK-1"))
    (root / "notes.md").write_text("foo\n", encoding="utf-8")
    (root / "broken").unlink()
    clean = service.check("TASK-1")
    assert clean.failures == ()
    assert clean.passed == ("docs/header", "develop/sh")
    rejected = _complete(service)
    assert rejected.fix_required is None


def test_check_marks_a_check_whose_globs_match_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _develop(root)
    (root / "app.py").write_text("x\n", encoding="utf-8")

    preview = service.check("TASK-1")

    assert preview.not_applicable == ("docs/header",)
    assert preview.passed == ("develop/sh",)


def test_check_on_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    _develop(root)
    _violate(root)

    code = main(["--root", str(root), "check", "TASK-1"])

    out = capsys.readouterr().out
    assert code == 1
    assert "# TASK-1 · check `develop`" in out
    assert "## 2 of 2 checks failed" in out
    assert "### `develop/sh` (hook)" in out
    assert "    notes.md" in out
    assert "Judged at completion: `develop/1`." in out
    assert "Nothing was recorded" in out
    assert main(["--root", str(root), "check", "TASK-1", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["passed"] is False
    assert [failure["id"] for failure in data["failures"]] == [
        "docs/header",
        "develop/sh",
    ]
    assert data["judged_at_completion"] == ["develop/1"]
    (root / "notes.md").unlink()
    (root / "broken").unlink()
    assert main(["--root", str(root), "check", "TASK-1"]) == 0
    assert "All checks pass (2 run)." in capsys.readouterr().out


def test_check_is_not_logged(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _develop(root)
    log = root / ".ww/log.jsonl"
    before = log.read_text(encoding="utf-8") if log.exists() else ""

    main(["--root", str(root), "check", "TASK-1"])

    after = log.read_text(encoding="utf-8") if log.exists() else ""
    assert after == before


def test_check_needs_a_step_in_progress(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = WorkflowService(Storage(root))
    service.start("task", "TASK-1", agent="codex", init_artifact="Do.")

    with pytest.raises(StateError, match="nothing to check: no step"):
        service.check("TASK-1")


def test_check_refuses_a_verification_item(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _develop(root)
    held = _complete(service)
    assert held.item_name == "develop-verify-1"

    with pytest.raises(StateError, match="verifies another step's rules"):
        service.check("TASK-1")


# ww dispute -------------------------------------------------------------------


def test_a_dispute_needs_a_rejection_naming_the_check(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _develop(root)

    with pytest.raises(StateError, match="nothing to dispute.*run check first"):
        service.dispute("TASK-1", "docs/header", "It is fine.")
    _violate(root)
    _complete(service)
    with pytest.raises(StateError, match="nothing to dispute"):
        service.dispute("TASK-1", "develop/1", "It is fine.")
    with pytest.raises(StateError, match="--reason must be non-empty"):
        service.dispute("TASK-1", "docs/header", "  ")


def test_a_dispute_stops_the_task_for_the_operator(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _rejected(root)

    stop = service.dispute(
        "TASK-1", "docs/header", "notes.md is a scratch file, not documentation."
    )

    assert stop.status == "failed"
    assert stop.control == "awaiting_operator"
    assert stop.operator_reason == "check_disputed"
    assert stop.error == "check disputed: docs/header"
    assert stop.dispute is not None
    assert stop.dispute.attempt == 1
    assert stop.dispute.failure.output == "notes.md"
    assert [command.action for command in stop.recovery_commands] == [
        "retry",
        "force",
    ]
    rendered = _markdown(stop)
    assert "the step's worker disputed a check" in rendered
    assert "The step's worker disputes `docs/header`" in rendered
    assert "> notes.md is a scratch file, not documentation." in rendered
    assert 'Include "foo" in every Markdown file you change.' in rendered
    assert "Command: grep -L foo $WW_STEP_CHANGED_FILES || true" in rendered
    assert "To let the check stand" in rendered
    assert "To waive `docs/header` for this step" in rendered
    data = json.loads(JsonOutputAdapter().render_instruction(stop))
    assert data["dispute"]["reason"] == "notes.md is a scratch file, not documentation."
    assert data["dispute"]["check"]["id"] == "docs/header"
    (entry,) = DisputeLog(root).load()
    assert (entry.check, entry.task_id, entry.step, entry.attempt) == (
        "docs/header",
        "TASK-1",
        "develop",
        1,
    )
    assert entry.text_hash is not None
    state, record = _record(service)
    assert record.dispute is not None
    assert state.failure_kind == "check_disputed"
    run = service.tasks.read_task_record("TASK-1")[0][0]
    assert TaskRunAggregate.from_dict(run.to_dict()) == run
    assert "waive the disputed check `docs/header`" in service.force_target("TASK-1")


def test_a_dispute_of_the_judged_rule_records_its_wording(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _develop(root)
    _complete(service)
    back = service.complete(
        "TASK-1",
        artifact="Findings.",
        rule_results=(
            json.dumps(
                {
                    "id": "develop/1",
                    "status": "not_convertible",
                    "reason": "A matter of review.",
                    "verdict": "fail",
                    "failures": [{"file": "README.md", "what": "a flag changed"}],
                }
            ),
        ),
    )
    assert back.fix_required is not None
    service.next("TASK-1")

    stop = service.dispute("TASK-1", "develop/1", "No flag changed.")

    assert stop.dispute is not None
    assert stop.dispute.failure.judged is True
    (entry,) = DisputeLog(root).load()
    assert entry.text_hash == rule_text_hash(JUDGED)


def test_retry_lets_the_disputed_check_stand(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _rejected(root)
    service.dispute("TASK-1", "docs/header", "Scratch file.")

    service.next("TASK-1", retry=True)
    page = service.next("TASK-1")

    assert page.item_name == "develop"
    assert page.item_status == "in_progress"
    assert page.fix_required is not None
    assert page.fix_required.attempt == 1
    state, record = _record(service)
    assert state.failure_kind is None
    assert record.dispute is None
    assert len(record.check_reports) == 1
    again = _complete(service)
    assert again.fix_required is not None
    assert again.fix_required.attempt == 2


def test_force_waives_only_the_disputed_check(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _rejected(root)
    service.dispute("TASK-1", "docs/header", "Scratch file.")

    page = service.next("TASK-1", force=True, force_reason="It is a scratch file.")

    assert page.item_name == "develop"
    assert page.checks_waived == (("docs/header", "It is a scratch file."),)
    assert page.fix_required is not None
    assert [failure.id for failure in page.fix_required.failures] == ["develop/sh"]
    rendered = _markdown(page)
    assert "### `docs/header`" not in rendered
    still = _complete(service)
    assert still.fix_required is not None
    assert [failure.id for failure in still.fix_required.failures] == ["develop/sh"]
    (root / "broken").unlink()
    held = _complete(service, "Fixed.")
    assert held.item_name == "develop-verify-1"
    recorded = service.complete(
        "TASK-1", artifact="Findings.", rule_results=(json.dumps(PASS),)
    )
    assert recorded.item_name == "check"
    artifact = _artifact(root)
    assert "- `docs/header`: failed" in artifact
    assert "- `develop/sh` (hook): passed" in artifact
    assert "- `develop/1`: verified pass" in artifact
    assert (
        "Checks waived by the operator (`docs/header`): It is a scratch file."
        in artifact
    )


def test_lint_lists_disputed_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    service = _rejected(root)
    service.dispute("TASK-1", "docs/header", "Scratch file.")

    assert main(["--root", str(root), "lint"]) == 0

    assert "Disputed docs/header: 1 time, last in TASK-1 develop\n" in (
        capsys.readouterr().out
    )


def test_the_dispute_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    _rejected(root)

    code = main(
        [
            "--root",
            str(root),
            "dispute",
            "TASK-1",
            "--rule",
            "develop/sh",
            "--reason",
            "The file is expected.",
            "--role",
            "worker",
        ]
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "The step's worker disputes `develop/sh`" in out
    assert "### Operator recovery" in out
    assert (root / DISPUTES_FILE).is_file()


def test_the_worker_in_auto_returns_the_dispute(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _develop(root, runtime="auto")
    _violate(root)
    service.complete(
        "TASK-1",
        artifact="Try.",
        summary_for_next="Done.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )

    stop = service.dispute(
        "TASK-1",
        "docs/header",
        "Scratch file.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )

    rendered = _markdown(stop)
    assert 'Stop here. Return the "Handoff to manager" block below' in rendered
    assert "error: check disputed: docs/header" in rendered
    assert "### Operator recovery" not in rendered


# --yes on next ----------------------------------------------------------------


def _no_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(prompt: str) -> str:
        raise AssertionError("--yes must not ask")

    monkeypatch.setattr("builtins.input", refuse)


def test_yes_confirms_a_retry_and_a_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)
    service = _rejected(root)
    service.dispute("TASK-1", "docs/header", "Scratch file.")
    _no_prompt(monkeypatch)

    assert main(["--root", str(root), "next", "TASK-1", "--retry", "--yes"]) == 0
    _, record = _record(service)
    assert record.dispute is None
    service.next("TASK-1")
    service.dispute("TASK-1", "docs/header", "Scratch file, again.")
    code = main(
        [
            "--root",
            str(root),
            "next",
            "TASK-1",
            "--force",
            "--reason",
            "Scratch.",
            "--yes",
        ]
    )

    assert code == 0
    assert "Confirmed with --yes." in capsys.readouterr().err
    _, record = _record(service)
    assert record.checks_waived == (("docs/header", "Scratch."),)


def test_yes_alone_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    _develop(root)

    assert main(["--root", str(root), "next", "TASK-1", "--yes"]) == 1
    assert "--yes confirms next --retry, --force or --approve" in (
        capsys.readouterr().err
    )


# ww rule ----------------------------------------------------------------------


def test_rule_shows_a_rule_file_in_full(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _develop(root)

    view = service.rule("TASK-1", "docs/header")

    assert view.kind == "rule"
    assert view.text.endswith("A marker keeps the documentation searchable.")
    assert view.paths == ("*.md",)
    assert view.source == "rules/docs/header.md"
    assert view.command == "grep -L foo $WW_STEP_CHANGED_FILES || true"
    assert view.assertion == "Command output must be empty."
    # The group applies to every step of the task.
    assert view.steps == ("develop", "check")


def test_rule_shows_a_judged_rule_with_what_the_store_knows(tmp_path: Path) -> None:
    root = _project(tmp_path)
    text_hash = rule_text_hash(JUDGED)
    (root / STORE_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": {
                    text_hash: {
                        "text": JUDGED,
                        "status": "not_convertible",
                        "reason": "Needs a reviewer.",
                        "interpretation": "No flag changes.",
                    }
                },
                "checks": {},
            }
        ),
        encoding="utf-8",
    )
    service = _develop(root)

    view = service.rule("TASK-1", "develop/1")

    assert (view.source, view.command) == (None, None)
    assert view.store_status == "not_convertible"
    assert view.interpretation == "No flag changes."
    assert view.resolution == "judged"
    assert view.text_hash == text_hash


def test_rule_shows_a_fix_hook_and_refuses_an_unknown_id(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _develop(root)

    hook = service.rule("TASK-1", "develop/sh")

    assert hook.kind == "hook"
    assert hook.command == "sh -c test ! -e broken"
    with pytest.raises(StateError, match="has no rule or check 'nope/1'"):
        service.rule("TASK-1", "nope/1")


def test_rule_on_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    _develop(root)

    assert main(["--root", str(root), "rule", "TASK-1", "docs/header"]) == 0
    out = capsys.readouterr().out
    assert "# Rule `docs/header`" in out
    assert "- Rule file: rules/docs/header.md" in out
    assert "- Applies to: *.md" in out
    assert main(["--root", str(root), "rule", "TASK-1", "develop/1"]) == 0
    out = capsys.readouterr().out
    assert "- Written in the step's own `rules` list." in out
    assert "- No command: a verifier judges it when the step completes." in out
    assert main(["--root", str(root), "rule", "TASK-1", "develop/1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == JUDGED


# ww rules and rules prune -----------------------------------------------------


def test_rules_lists_groups_and_step_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert main(["--root", str(root), "rules"]) == 0

    out = capsys.readouterr().out
    assert "### Group `docs`" in out
    assert "Applies to: every step." in out
    assert (
        '- `docs/header` — *.md — Include "foo" in every Markdown file you change. '
        "(checked by its command)" in out
    )
    assert "### Step `develop`" in out
    assert f"- `develop/1` — {JUDGED}" in out
    assert main(["--root", str(root), "rules", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    (group,) = data["groups"]
    assert (group["name"], group["workflows"], group["steps"]) == ("docs", "*", "*")
    assert group["rules"][0]["source"] == "rules/docs/header.md"
    assert data["steps"][0]["rules"][0]["id"] == "develop/1"
    assert (data["scripting"], data["check_guidance"]) == (True, None)


def test_rules_json_carries_the_rules_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    (root / "ww.json").write_text(
        json.dumps({"rules": {"scripting": False, "check_guidance": "Run in docker."}}),
        encoding="utf-8",
    )

    assert main(["--root", str(root), "rules", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert (data["scripting"], data["check_guidance"]) == (False, "Run in docker.")


def test_rules_names_group_filters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(
        tmp_path,
        WORKFLOWS.replace(
            "  docs: [rules/docs/]",
            "  docs:\n    rules: [rules/docs/]\n    workflows: [task]\n"
            "    steps: [develop]\n    model: opus",
        ),
    )

    assert main(["--root", str(root), "rules"]) == 0

    out = capsys.readouterr().out
    assert "Applies to: workflows `task`; steps `develop`." in out
    assert "Verifier hints: model opus." in out


def _orphaned_store(root: Path) -> str:
    orphan = "f" * 64
    live = rule_text_hash(JUDGED)
    (root / STORE_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": {
                    orphan: {
                        "text": "A rule nobody declares.",
                        "status": "converted",
                        "check": "old-tool",
                    },
                    live: {"text": JUDGED, "status": "rejected", "reason": "No."},
                },
                "checks": {
                    "old-tool": {
                        "argv": ["old-tool"],
                        "assert": None,
                        "config": [],
                        "covers": [orphan],
                        "proven": True,
                        "status": "converted",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return live


def test_prune_lists_the_orphans_and_asks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)
    live = _orphaned_store(root)
    answers = iter(["n"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    assert main(["--root", str(root), "rules", "prune"]) == 1

    error = capsys.readouterr().err
    assert f"- rule {'f' * 12} (converted): A rule nobody declares." in error
    assert "- check old-tool (converted)" in error
    assert "Prune cancelled" in error
    assert set(RuleStore(root).load().rules) == {"f" * 64, live}
    answers = iter(["y"])

    assert main(["--root", str(root), "rules", "prune"]) == 0

    assert "Pruned 1 rule and 1 check entries" in capsys.readouterr().out
    automation = RuleStore(root).load()
    assert set(automation.rules) == {live}
    assert automation.checks == {}


def test_prune_with_yes_and_without_orphans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)
    live = _orphaned_store(root)
    _no_prompt(monkeypatch)

    assert main(["--root", str(root), "rules", "prune", "--yes", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data == {"pruned": {"rules": ["f" * 64], "checks": ["old-tool"]}}
    assert set(RuleStore(root).load().rules) == {live}
    assert main(["--root", str(root), "rules", "prune"]) == 0
    assert "has no orphan entries" in capsys.readouterr().out


def test_prune_keeps_a_check_a_live_rule_still_names(tmp_path: Path) -> None:
    root = _project(tmp_path)
    live = _orphaned_store(root)
    store = json.loads((root / STORE_FILE).read_text(encoding="utf-8"))
    store["rules"][live] = {"text": JUDGED, "status": "converted", "check": "old-tool"}
    (root / STORE_FILE).write_text(json.dumps(store), encoding="utf-8")

    assert main(["--root", str(root), "rules", "prune", "--yes"]) == 0

    automation = RuleStore(root).load()
    assert set(automation.rules) == {live}
    assert set(automation.checks) == {"old-tool"}


# rules revoke ------------------------------------------------------------------


def _converted_store(root: Path) -> str:
    """A converted check, approved by the operator, covering the judged rule."""
    live = rule_text_hash(JUDGED)
    (root / STORE_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": {
                    live: {
                        "text": JUDGED,
                        "status": "converted",
                        "check": "cli-diff",
                        "approved_by": "operator",
                        "approved_in": "TASK-1/01-task",
                    }
                },
                "checks": {
                    "cli-diff": {
                        "argv": ["scripts/cli-diff"],
                        "assert": None,
                        "config": ["cli-diff.toml"],
                        "covers": [live],
                        "proven": True,
                        "status": "converted",
                        "approved_by": "auto",
                        "approved_in": "TASK-1/01-task",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "cli-diff.toml").write_text("strict = true\n", encoding="utf-8")
    return live


def test_revoke_shows_the_check_and_asks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)
    live = _converted_store(root)
    answers = iter(["n"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    assert main(["--root", str(root), "rules", "revoke", "cli-diff"]) == 1

    error = capsys.readouterr().err
    assert "Check cli-diff (converted): scripts/cli-diff" in error
    assert "- approved by auto" in error
    assert "Revoke cancelled" in error
    assert RuleStore(root).load().checks["cli-diff"].status == "converted"
    answers = iter(["y"])

    assert (
        main(["--root", str(root), "rules", "revoke", "cli-diff", "--reason", "slow"])
        == 0
    )

    out = capsys.readouterr().out
    assert "Revoked check cli-diff" in out
    assert "judged by a verifier from now on: 1" in out
    assert "config files (cli-diff.toml) stay" in out
    automation = RuleStore(root).load()
    check = automation.checks["cli-diff"]
    assert check.status == "rejected"
    assert check.reason == "revoked by the operator: slow"
    assert automation.rules[live].status == "rejected"
    assert automation.rules[live].reason == "revoked by the operator: slow"
    assert automation.converted_check(live) is None
    # Only the store changed: the check's configuration stays.
    assert (root / "cli-diff.toml").read_text(encoding="utf-8") == "strict = true\n"


def test_revoke_with_yes_prints_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)
    live = _converted_store(root)
    _no_prompt(monkeypatch)

    assert (
        main(["--root", str(root), "rules", "revoke", "cli-diff", "--yes", "--json"])
        == 0
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "revoked": {
            "check": "cli-diff",
            "rules": [live],
            "reason": "revoked by the operator",
            "config": ["cli-diff.toml"],
        }
    }
    assert "Confirmed with --yes" in captured.err
    assert main(["--root", str(root), "rules", "revoke", "cli-diff", "--yes"]) == 1
    assert "already rejected" in capsys.readouterr().err


def test_revoke_refuses_without_a_terminal_or_an_unknown_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)
    _converted_store(root)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    _no_prompt(monkeypatch)

    assert main(["--root", str(root), "rules", "revoke", "cli-diff"]) == 1

    assert "no terminal to ask at" in capsys.readouterr().err
    assert RuleStore(root).load().checks["cli-diff"].status == "converted"
    assert main(["--root", str(root), "rules", "revoke", "nothing", "--yes"]) == 1
    assert "has no check 'nothing'" in capsys.readouterr().err


# instruction --role worker on the manager's item ------------------------------


@pytest.mark.parametrize(
    ("setting", "why"),
    [
        ("role: manager", "is the manager's (`role: manager`)"),
        ("interactive: true", "is interactive"),
    ],
)
def test_a_worker_is_refused_the_managers_item(
    tmp_path: Path, setting: str, why: str
) -> None:
    (tmp_path / "ww.yaml").write_text(
        f"""workflows:
  - name: task
    steps:
      - develop: Develop it.
      - review: Review it.
        {setting}
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "task", "TASK-1", agent="codex", workflow_runtime="auto", init_artifact="Do."
    )
    service.next("TASK-1", caller_role="manager")
    service.complete(
        "TASK-1",
        artifact="Built.",
        summary_for_next="Built.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    manager = service.next("TASK-1", caller_role="manager")
    assert manager.item_name == "review"
    assert manager.item_status == "in_progress"

    page = service.instruction(
        "TASK-1", caller_role="worker", assignment=assignment_token(service, "TASK-1")
    )

    assert page.manager_only is True
    assert page.continuation_command is None
    rendered = _markdown(page)
    assert "## `review` is the manager's" in rendered
    assert f"`review` {why}" in rendered
    assert "complete TASK-1" not in rendered
    assert json.loads(JsonOutputAdapter().render_instruction(page))["manager_only"]
    own = service.instruction("TASK-1", caller_role="manager")
    assert own.manager_only is False
    assert own.continuation_command is not None
