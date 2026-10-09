# SPDX-License-Identifier: GPL-3.0-or-later
"""Rules on the step page, checks at completion, and the fix loop, end to end."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.cli import main
from ww.execution_models import ExecutionState, PlanItemExecution, TaskRunAggregate
from ww.instructions import Instruction
from ww.output_adapters.json_adapter import JsonOutputAdapter
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.rule_stats import RuleFailure
from ww.service import WorkflowService
from ww.storage import Storage

# A judged rule, a rule with a command scoped to Markdown, a ``fix`` hook,
# and an ordinary hook, all on ``develop``.
WORKFLOWS = """rules:
  docs: [rules/docs/]
workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        rules:
          - Keep the public CLI unchanged.
        hooks:
          before_complete:
            - argv: [sh, -c, "test ! -e broken"]
              on_failure: fix
            - name: record
              argv: [touch, recorded.txt]
      - check: Check it.
"""

HEADER_RULE = """---
paths: ["*.md"]
check:
  shell: grep -L foo $WW_STEP_CHANGED_FILES || true
  assert: [empty]
---
Include "foo" in every Markdown file you change.
"""


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _project(root: Path, workflows: str = WORKFLOWS, *, git: bool = True) -> Path:
    rules = root / "rules/docs"
    rules.mkdir(parents=True)
    (rules / "header.md").write_text(HEADER_RULE, encoding="utf-8")
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    (root / "README.md").write_text("foo seed\n", encoding="utf-8")
    if git:
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
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime=runtime,
        init_artifact="Do it.",
    )
    return service


def _develop(root: Path) -> WorkflowService:
    service = _started(root)
    page = service.next("TASK-1")
    assert page.item_name == "develop"
    return service


def _complete(service: WorkflowService, artifact: str = "Done.") -> Instruction:
    return service.complete("TASK-1", artifact=artifact, summary_for_next="Done.")


def _verify(service: WorkflowService) -> Instruction:
    """Complete the verification of ``develop/1`` with a passing verdict."""
    return service.complete(
        "TASK-1",
        artifact="Verified.",
        rule_results=(
            json.dumps({"id": "develop/1", "status": "judged", "verdict": "pass"}),
        ),
    )


def _violate(root: Path) -> None:
    (root / "notes.md").write_text("no marker here\n", encoding="utf-8")
    (root / "broken").write_text("", encoding="utf-8")


def _develop_artifact(root: Path) -> Path:
    return root / ".ww/tasks/TASK-1/runs/01-task/steps/02-develop.md"


def _record(
    service: WorkflowService, name: str = "develop"
) -> tuple[ExecutionState, PlanItemExecution]:
    state, snapshot = service.load("TASK-1")
    index = next(
        index
        for index, item in enumerate(snapshot.plan.items)
        if item.name == name and item.phase == "step"
    )
    return state, state.item_executions[index]


def test_the_step_page_lists_judged_rules_and_names_the_checked_ones(
    tmp_path: Path,
) -> None:
    service = _develop(_project(tmp_path))

    page = service.instruction("TASK-1")
    rendered = MarkdownOutputAdapter().render_instruction(page)

    assert [(rule.id, rule.has_command) for rule in page.rules] == [
        ("docs/header", True),
        ("develop/1", False),
        ("develop/sh", True),
    ]
    assert "### Rules" in rendered
    assert "- `develop/1` — Keep the public CLI unchanged." in rendered
    assert (
        "Checked automatically when you complete: `docs/header`, `develop/sh`."
        in rendered
    )
    assert "under a **Rules** heading" in rendered
    assert rendered.index("### Work instruction") < rendered.index("### Rules")
    assert json.loads(JsonOutputAdapter().render_instruction(page))["rules"][1] == {
        "id": "develop/1",
        "summary": "Keep the public CLI unchanged.",
        "paths": [],
        "contains_in_file": [],
        "contains_in_diff": [],
        "has_command": False,
        "hook": False,
        "interpretation": None,
        "check": None,
        "missing": None,
    }


def test_a_violation_rejects_the_completion_and_keeps_the_step(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service = _develop(root)
    _violate(root)

    rejected = _complete(service, "First try.")

    assert rejected.fix_required is not None
    assert rejected.control == "continue_worker"
    assert rejected.next_role == "worker"
    assert rejected.item_name == "develop"
    assert rejected.item_status == "in_progress"
    assert [failure.id for failure in rejected.fix_required.failures] == [
        "docs/header",
        "develop/sh",
    ]
    assert rejected.fix_required.attempt == 1
    assert rejected.fix_required.max_fixes == 3
    assert rejected.fix_required.limiting == "docs/header"
    assert [
        (failure.failures, failure.max_fixes)
        for failure in rejected.fix_required.failures
    ] == [(1, 3), (1, 3)]
    assert rejected.fix_required.failures[0].output == "notes.md"
    assert not _develop_artifact(root).exists()
    # The ordinary hook runs only with an accepted completion.
    assert not (root / "recorded.txt").exists()
    _, record = _record(service)
    assert record.draft_artifact == "First try."
    assert record.status == "in_progress"
    rendered = MarkdownOutputAdapter().render_instruction(rejected)
    assert (
        "## Fix required: 2 of 2 checks failed (attempt 1 of 3 for `docs/header`)"
        in rendered
    )
    assert "### `develop/sh` (hook)" in rendered
    assert rendered.count("Failures: 1 of 3 allowed.") == 2
    assert 'Include "foo" in every Markdown file you change.' in rendered
    assert "    notes.md" in rendered
    assert "it replaces the draft below entirely" in rendered
    assert "### Draft artifact\n\n```markdown\nFirst try.\n```" in rendered
    assert "./ww complete TASK-1" in rendered
    assert "Completion recorded successfully" not in rendered
    assert service.instruction("TASK-1").fix_required is not None


def test_fixing_the_causes_completes_the_step_with_a_rules_section(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service = _develop(root)
    _violate(root)
    _complete(service, "First try.")
    (root / "notes.md").write_text("foo now\n", encoding="utf-8")
    (root / "broken").unlink()

    held = _complete(service, "Fixed.")
    assert held.item_name == "develop-verify-1"
    accepted = _verify(service)

    assert accepted.fix_required is None
    assert accepted.item_name == "check"
    assert (root / "recorded.txt").exists()
    artifact = _develop_artifact(root).read_text(encoding="utf-8")
    assert artifact.index("## Result") < artifact.index("## Rules")
    assert "- `docs/header`: passed" in artifact
    assert "- `develop/1`: verified pass (by `task:develop:verify:1`)" in artifact
    assert "- `develop/sh` (hook): passed" in artifact
    assert "Completions rejected before this one: 1." in artifact
    # The rejected draft survives the hold and the accepted completion.
    assert artifact.endswith("## Previous attempt\n\nFirst try.\n")
    assert artifact.index("## Result\n\nFixed.") < artifact.index("## Rules")
    _, record = _record(service)
    assert record.draft_artifact is None
    assert [bool(report.failed) for report in record.check_reports] == [True, False]
    listed = [entry for entry in service.artifacts("TASK-1") if "check" in entry]
    assert {entry["check"] for entry in listed} == {"docs/header"}
    assert all(Path(entry["path"]).is_file() for entry in listed)


def test_a_rule_whose_glob_matches_nothing_is_not_applicable(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _develop(root)
    (root / "app.py").write_text("code\n", encoding="utf-8")

    _complete(service)
    accepted = _verify(service)

    assert accepted.item_name == "check"
    artifact = _develop_artifact(root).read_text(encoding="utf-8")
    assert "- `docs/header`: not applicable" in artifact
    assert "No completion was rejected." in artifact
    header = service.rule_stats.load()["docs/header"]
    assert (header.applied, header.checked, header.not_applicable) == (0, 0, 1)


def test_every_settled_completion_counts_in_the_rule_statistics(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service = _develop(root)
    _violate(root)
    _complete(service, "First.")
    _complete(service, "Second.")
    (root / "notes.md").write_text("foo now\n", encoding="utf-8")
    (root / "broken").unlink()
    _complete(service, "Fixed.")
    _verify(service)

    stats = service.rule_stats.load()

    # The hook is a check without a rule: only rules are counted.
    assert set(stats) == {"docs/header", "develop/1"}
    header = stats["docs/header"]
    assert (header.applied, header.checked, header.check_failures) == (3, 3, 2)
    assert header.last_failure == RuleFailure("TASK-1", "develop", "check")
    assert header.last_failed_at is not None
    assert header.last_applied_at is not None
    assert header.last_applied_at >= header.last_failed_at
    judged = stats["develop/1"]
    assert (judged.applied, judged.judged, judged.failures) == (1, 1, 0)
    assert judged.last_failure is None


MAIL_RULE = """---
paths: ["*.php"]
contains_in_file: [Mailer, Postman]
check:
  shell: test "$WW_STEP_CHANGED_FILES" = "mail.php"
---
Send mail only through the mailer.
"""
FLUSH_RULE = """---
paths: ["*.php"]
contains_in_diff: [Mailer]
check:
  shell: test "$WW_STEP_CHANGED_FILES" = "mail.php"
---
Flush the mailer after sending.
"""


def _mail_project(root: Path) -> Path:
    _project(root)
    (root / "rules/docs/mail.md").write_text(MAIL_RULE, encoding="utf-8")
    return root


def test_a_rule_check_runs_on_the_files_with_the_glob_and_the_text(
    tmp_path: Path,
) -> None:
    root = _mail_project(tmp_path)
    service = _develop(root)
    (root / "mail.php").write_text("<?php new Mailer();\n", encoding="utf-8")
    (root / "plain.php").write_text("<?php echo 1;\n", encoding="utf-8")
    (root / "notes.md").write_text("foo Mailer\n", encoding="utf-8")

    _complete(service)
    accepted = _verify(service)

    assert accepted.item_name == "check"
    artifact = _develop_artifact(root).read_text(encoding="utf-8")
    assert "- `docs/mail`: passed" in artifact


def test_a_rule_check_selecting_other_files_than_expected_fails(
    tmp_path: Path,
) -> None:
    root = _mail_project(tmp_path)
    service = _develop(root)
    (root / "mail.php").write_text("<?php new Mailer();\n", encoding="utf-8")
    (root / "post.php").write_text("<?php new Postman();\n", encoding="utf-8")

    rejected = _complete(service)

    assert rejected.fix_required is not None
    assert [failure.id for failure in rejected.fix_required.failures] == ["docs/mail"]


def test_a_rule_whose_text_is_in_no_changed_file_is_not_applicable(
    tmp_path: Path,
) -> None:
    root = _mail_project(tmp_path)
    service = _develop(root)
    (root / "plain.php").write_text("<?php echo 1;\n", encoding="utf-8")

    _complete(service)
    accepted = _verify(service)

    assert accepted.item_name == "check"
    artifact = _develop_artifact(root).read_text(encoding="utf-8")
    assert "- `docs/mail`: not applicable" in artifact


def _flush_project(root: Path) -> Path:
    """A project with a mailer file committed and a rule on changed lines."""
    _project(root)
    (root / "rules/docs/flush.md").write_text(FLUSH_RULE, encoding="utf-8")
    (root / "mail.php").write_text("<?php\nnew Mailer();\necho 1;\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "mail", cwd=root)
    return root


def test_a_diff_check_does_not_run_on_an_edit_elsewhere_in_the_file(
    tmp_path: Path,
) -> None:
    root = _flush_project(tmp_path)
    service = _develop(root)
    (root / "mail.php").write_text("<?php\nnew Mailer();\necho 2;\n", encoding="utf-8")

    _complete(service)
    accepted = _verify(service)

    assert accepted.item_name == "check"
    artifact = _develop_artifact(root).read_text(encoding="utf-8")
    assert "- `docs/flush`: not applicable" in artifact


def test_a_diff_check_runs_when_a_changed_line_holds_the_string(
    tmp_path: Path,
) -> None:
    root = _flush_project(tmp_path)
    service = _develop(root)
    (root / "mail.php").write_text("<?php\necho 1;\n", encoding="utf-8")

    _complete(service)
    accepted = _verify(service)

    assert accepted.item_name == "check"
    artifact = _develop_artifact(root).read_text(encoding="utf-8")
    assert "- `docs/flush`: passed" in artifact


def test_the_step_page_prints_the_strings_next_to_the_globs(tmp_path: Path) -> None:
    root = tmp_path
    _mail_project(root)
    (root / "ww.yaml").write_text(
        WORKFLOWS.replace(
            "- Keep the public CLI unchanged.",
            "- Keep the public CLI unchanged.\n          - rules/docs/judged.md",
        ),
        encoding="utf-8",
    )
    (root / "rules/docs/judged.md").write_text(
        '---\npaths: ["*.php"]\ncontains_in_file: [Mailer, "Post man"]\n'
        "contains_in_diff: [flush]\n---\nJudge the mail.\n",
        encoding="utf-8",
    )
    service = _develop(root)

    page = service.instruction("TASK-1")

    rendered = MarkdownOutputAdapter().render_instruction(page)
    assert (
        '- `docs/judged` — *.php; in file "Mailer", "Post man"; in diff "flush" '
        "— Judge the mail." in rendered
    )
    line = next(rule for rule in page.rules if rule.id == "docs/judged")
    assert line.contains_in_file == ("Mailer", "Post man")
    assert line.contains_in_diff == ("flush",)
    payload = json.loads(JsonOutputAdapter().render_instruction(page))["rules"]
    judged = next(rule for rule in payload if rule["id"] == "docs/judged")
    assert judged["contains_in_file"] == ["Mailer", "Post man"]
    assert judged["contains_in_diff"] == ["flush"]


def test_work_that_was_uncommitted_before_the_step_is_not_its_change(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    (root / "old.md").write_text("no marker, but not this step's\n", encoding="utf-8")
    service = _develop(root)

    _complete(service)
    accepted = _verify(service)

    assert accepted.item_name == "check"


def _fail_three_times(root: Path) -> WorkflowService:
    service = _develop(root)
    _violate(root)
    for attempt in (1, 2):
        rejected = _complete(service)
        assert rejected.fix_required is not None
        assert rejected.fix_required.attempt == attempt
    return service


def test_the_fix_limit_stops_the_task_for_the_operator(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _fail_three_times(root)

    stopped = _complete(service)

    assert stopped.status == "failed"
    assert stopped.control == "awaiting_operator"
    assert stopped.operator_reason == "fix_limit"
    assert stopped.error == "check limit reached: docs/header, develop/sh"
    assert stopped.result_saved is False
    assert [command.action for command in stopped.recovery_commands] == [
        "retry",
        "force",
    ]
    rendered = MarkdownOutputAdapter().render_instruction(stopped)
    assert "the step's checks reached their fix limit" in rendered
    assert (
        "ww rejected this step's completion: `docs/header` failed 3 of 3 times "
        "allowed." in rendered
    )
    assert stopped.fix_required is not None
    assert (stopped.fix_required.attempt, stopped.fix_required.max_fixes) == (3, 3)
    assert "To complete the step without these checks" in rendered
    assert not _develop_artifact(root).exists()


def test_retry_after_the_fix_limit_gives_the_worker_a_fresh_count(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service = _fail_three_times(root)
    _complete(service)

    service.next("TASK-1", retry=True)
    resumed = service.next("TASK-1")

    assert resumed.item_name == "develop"
    assert resumed.item_status == "in_progress"
    assert resumed.fix_required is None
    state, record = _record(service)
    assert state.failure_kind is None
    assert record.check_reports == ()
    rejected = _complete(service)
    assert rejected.fix_required is not None
    assert rejected.fix_required.attempt == 1
    (root / "notes.md").write_text("foo\n", encoding="utf-8")
    (root / "broken").unlink()
    _complete(service)
    _verify(service)
    artifact = _develop_artifact(root).read_text(encoding="utf-8")
    assert "Completions rejected before this one: 4." in artifact


def test_force_after_the_fix_limit_waives_the_checks(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _fail_three_times(root)
    _complete(service)

    assert "waive the failed checks" in service.force_target("TASK-1")
    waived = service.next("TASK-1", force=True, force_reason="Checked by hand.")

    assert waived.item_name == "develop"
    assert waived.item_status == "in_progress"
    assert waived.checks_waived == (
        ("docs/header", "Checked by hand."),
        ("develop/1", "Checked by hand."),
        ("develop/sh", "Checked by hand."),
    )
    assert waived.fix_required is None
    rendered = MarkdownOutputAdapter().render_instruction(waived)
    assert (
        "The operator waived these checks for this step (`docs/header`, "
        "`develop/1`, `develop/sh`): Checked by hand." in rendered
    )
    accepted = _complete(service, "Done anyway.")
    assert accepted.item_name == "check"
    artifact = _develop_artifact(root).read_text(encoding="utf-8")
    assert "- `docs/header`: failed" in artifact
    # The waiver covers the verification too: the judged rule is self-declared.
    assert "- `develop/1`: self-declared" in artifact
    assert (
        "Checks waived by the operator (`docs/header`, `develop/1`, "
        "`develop/sh`): Checked by hand." in artifact
    )
    assert "Completions rejected before this one: 3." in artifact
    stats = service.rule_stats.load()
    header = stats["docs/header"]
    assert (header.checked, header.check_failures, header.waived) == (3, 3, 1)
    assert (stats["develop/1"].applied, stats["develop/1"].waived) == (0, 1)


def test_an_operator_hook_still_stops_as_a_failed_handler(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        hooks:
          before_complete:
            - argv: [sh, -c, "exit 3"]
      - check: Check it.
""",
    )
    service = _develop(root)

    stopped = _complete(service)

    assert stopped.fix_required is None
    assert stopped.operator_reason == "handler_failed"
    assert _develop_artifact(root).exists()


def test_without_git_every_matching_file_is_checked(tmp_path: Path) -> None:
    root = _project(tmp_path, git=False)
    (root / "old.md").write_text("no marker\n", encoding="utf-8")
    service = _develop(root)

    rejected = _complete(service)

    assert rejected.fix_required is not None
    assert rejected.fix_required.failures[0].output == "old.md"
    _, record = _record(service)
    assert record.change_mark is None
    assert record.check_reports[0].all_files is True


def test_state_with_check_records_survives_a_round_trip(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _fail_three_times(root)
    _complete(service)

    reloaded = WorkflowService(Storage(root))
    state, record = _record(reloaded)

    assert state.failure_kind == "fix_limit"
    assert record.change_mark is not None
    assert len(record.check_reports) == 3
    assert record.check_reports[0].results[0].id == "docs/header"
    run = reloaded.tasks.read_task_record("TASK-1")[0][0]
    assert TaskRunAggregate.from_dict(run.to_dict()) == run


def test_the_auto_runtime_returns_the_fix_page_to_the_worker(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _started(root, runtime="auto")
    service.next("TASK-1", caller_role="manager")
    _violate(root)

    rejected = service.complete(
        "TASK-1",
        artifact="Try.",
        summary_for_next="Done.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )

    assert rejected.fix_required is not None
    assert rejected.control == "continue_worker"
    assert rejected.next_role == "worker"
    assert "./ww complete TASK-1 --role worker" in (
        MarkdownOutputAdapter().render_instruction(rejected)
    )


def test_the_complete_command_exits_non_zero_on_a_rejection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    _develop(root)
    _violate(root)

    code = main(
        [
            "--root",
            str(root),
            "complete",
            "TASK-1",
            "--artifact",
            "Try.",
            "--summary",
            "Done.",
        ]
    )

    assert code == 1
    assert "## Fix required" in capsys.readouterr().out


@pytest.mark.usefixtures("shipped_builtins")
def test_lint_reports_the_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, git=False)

    assert main(["--root", str(root), "lint"]) == 0

    assert capsys.readouterr().out.endswith(
        "Rules: 1 group, 2 rules\n"
        "Warning: 1 rule has no check yet (develop/1); `ww-scriptize-rules` "
        "builds checks for them.\n"
        "Hint: 1 judged rule is verified on every step (develop/1); add paths, "
        "contains_in_file or contains_in_diff to narrow it.\n"
    )


# Checks that cannot run here ---------------------------------------------------

# A script the shell does not find, a declared file that is missing, and a
# program that cannot be launched: each on a Markdown rule of ``docs``.
UNAVAILABLE_RULES = {
    "declared.md": (
        '---\npaths: ["*.md"]\ncheck:\n  argv: [python3, tools/lint.py]\n'
        "  files: [tools/lint.py]\n---\nLint the docs with the tool.\n"
    ),
    "program.md": (
        '---\npaths: ["*.md"]\ncheck:\n  argv: [./no-such-program]\n---\n'
        "Lint the docs with the program.\n"
    ),
    "script.md": (
        '---\npaths: ["*.md"]\ncheck:\n'
        "  shell: tools/lint.sh $WW_STEP_CHANGED_FILES\n---\n"
        "Lint the docs with the script.\n"
    ),
}
DECLARED_REASON = "`tools/lint.py` is missing in this step's directory"


def _unavailable_project(root: Path) -> Path:
    _project(root)
    for name, content in UNAVAILABLE_RULES.items():
        (root / "rules/docs" / name).write_text(content, encoding="utf-8")
    # The script lives outside the change set, so creating it later keeps
    # the tree the held completion was measured to.
    with (root / ".gitignore").open("a", encoding="utf-8") as ignored:
        ignored.write("tools/\n")
    return root


def _pass(*rule_ids: str) -> tuple[str, ...]:
    return tuple(
        json.dumps({"id": rule_id, "status": "judged", "verdict": "pass"})
        for rule_id in rule_ids
    )


def test_a_check_that_cannot_run_is_unavailable_not_failed(tmp_path: Path) -> None:
    root = _unavailable_project(tmp_path)
    service = _develop(root)
    _violate(root)

    rejected = _complete(service, "First try.")

    fix = rejected.fix_required
    assert fix is not None
    assert [failure.id for failure in fix.failures] == ["docs/header", "develop/sh"]
    assert fix.limiting == "docs/header"
    assert [check.id for check in fix.unavailable] == [
        "docs/declared",
        "docs/program",
        "docs/script",
    ]
    reasons = dict((check.id, check.reason) for check in fix.unavailable)
    assert reasons["docs/declared"] == DECLARED_REASON
    assert reasons["docs/program"].startswith("could not launch the command: ")
    assert reasons["docs/script"].startswith("command not found (exit 127): ")
    rendered = MarkdownOutputAdapter().render_instruction(rejected)
    assert "These checks could not run here" in rendered
    assert f"- `docs/declared`: check unavailable: {DECLARED_REASON}" in rendered
    assert "### `docs/declared`" not in rendered
    data = json.loads(JsonOutputAdapter().render_instruction(rejected))
    assert [check["id"] for check in data["fix_required"]["unavailable"]] == [
        "docs/declared",
        "docs/program",
        "docs/script",
    ]
    _, record = _record(service)
    assert record.check_failures("docs/script") == 0
    assert record.check_failures("docs/header") == 1
    stats = service.rule_stats.load()
    assert stats["docs/header"].check_failures == 1
    assert not {"docs/declared", "docs/program", "docs/script"} & set(stats)
    preview = service.check("TASK-1")
    assert [check.id for check in preview.unavailable] == [
        "docs/declared",
        "docs/program",
        "docs/script",
    ]
    assert preview.judged == (
        "docs/declared",
        "docs/program",
        "docs/script",
        "develop/1",
    )


def test_a_rule_whose_check_is_unavailable_is_judged_for_that_completion(
    tmp_path: Path,
) -> None:
    root = _unavailable_project(tmp_path)
    service = _develop(root)
    (root / "notes.md").write_text("foo now\n", encoding="utf-8")

    held = _complete(service, "Done.")

    assert held.item_name == "develop-verify-1"
    assert held.verification is not None
    assert [rule.id for rule in held.verification.rules] == [
        "docs/declared",
        "docs/program",
        "docs/script",
        "develop/1",
    ]
    declared = held.verification.rules[0]
    assert (declared.check, declared.unavailable) == ("docs/declared", DECLARED_REASON)
    rendered = MarkdownOutputAdapter().render_instruction(held)
    assert (
        "Its check `docs/declared` could not run here (check unavailable: "
        f"{DECLARED_REASON}), so judge it instead." in rendered
    )
    # The script turns up before the verdict: the held report's unavailable
    # result is never reused, so the check runs now and passes.
    (root / "tools").mkdir()
    script = root / "tools/lint.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    accepted = service.complete(
        "TASK-1",
        artifact="Verified.",
        rule_results=_pass("docs/declared", "docs/program", "docs/script", "develop/1"),
    )

    assert accepted.item_name == "check"
    artifact = _develop_artifact(root).read_text(encoding="utf-8")
    assert (
        "- `docs/declared`: verified pass (by `task:develop:verify:1`; check "
        f"unavailable: {DECLARED_REASON})" in artifact
    )
    assert "- `docs/script`: passed\n" in artifact
    assert "- `docs/header`: passed\n" in artifact
    assert "No completion was rejected." in artifact
    stats = service.rule_stats.load()
    declared_stats = stats["docs/declared"]
    assert (declared_stats.applied, declared_stats.checked, declared_stats.judged) == (
        1,
        0,
        1,
    )
    assert (stats["docs/script"].checked, stats["docs/script"].judged) == (1, 0)


def test_the_attempt_counts_the_check_nearest_its_own_limit(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _develop(root)
    _violate(root)
    _complete(service)
    (root / "broken").unlink()

    second = _complete(service)

    assert second.fix_required is not None
    assert (second.fix_required.attempt, second.fix_required.limiting) == (
        2,
        "docs/header",
    )
    assert [failure.id for failure in second.fix_required.failures] == ["docs/header"]
    rendered = MarkdownOutputAdapter().render_instruction(second)
    assert "(attempt 2 of 3 for `docs/header`)" in rendered
    assert "Failures: 2 of 3 allowed." in rendered
    (root / "broken").write_text("", encoding="utf-8")

    stopped = _complete(service)

    assert stopped.operator_reason == "fix_limit"
    assert stopped.error == "check limit reached: docs/header"
    assert stopped.fix_required is not None
    assert [
        (failure.id, failure.failures, failure.max_fixes)
        for failure in stopped.fix_required.failures
    ] == [("docs/header", 3, 3), ("develop/sh", 2, 3)]
    rendered = MarkdownOutputAdapter().render_instruction(stopped)
    assert "`docs/header` failed 3 of 3 times allowed" in rendered
    assert "Failures: 2 of 3 allowed." in rendered
