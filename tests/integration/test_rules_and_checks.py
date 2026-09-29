# SPDX-License-Identifier: GPL-3.0-or-later
"""Rules on the step page, checks at completion, and the fix loop, end to end."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ww.cli import main
from ww.execution_models import ExecutionState, PlanItemExecution, TaskRunAggregate
from ww.instructions import Instruction
from ww.output_adapters.json_adapter import JsonOutputAdapter
from ww.output_adapters.markdown import MarkdownOutputAdapter
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
  assert: { operator: empty }
---
Include "foo" in every Markdown file you change.
"""


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _project(root: Path, workflows: str = WORKFLOWS, *, git: bool = True) -> Path:
    rules = root / "rules/docs"
    rules.mkdir(parents=True)
    (rules / "header.md").write_text(HEADER_RULE, encoding="utf-8")
    (root / "ww-agentic-workflows.yaml").write_text(workflows, encoding="utf-8")
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
            json.dumps(
                {
                    "id": "develop/1",
                    "status": "not-convertible",
                    "reason": "A matter of review.",
                    "verdict": "pass",
                }
            ),
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

    page = service.status("TASK-1")
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
        "has_command": False,
        "hook": False,
        "interpretation": None,
        "check": None,
        "pending_operator": False,
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
    assert rejected.fix_required.failures[0].output == "notes.md"
    assert not _develop_artifact(root).exists()
    # The ordinary hook runs only with an accepted completion.
    assert not (root / "recorded.txt").exists()
    _, record = _record(service)
    assert record.draft_artifact == "First try."
    assert record.status == "in_progress"
    rendered = MarkdownOutputAdapter().render_instruction(rejected)
    assert "## Fix required: 2 of 2 checks failed (attempt 1 of 3)" in rendered
    assert "### `develop/sh` (hook)" in rendered
    assert 'Include "foo" in every Markdown file you change.' in rendered
    assert "    notes.md" in rendered
    assert "Your previous artifact is kept as a draft." in rendered
    assert "./ww complete TASK-1" in rendered
    assert "Completion recorded successfully" not in rendered
    assert service.status("TASK-1").fix_required is not None


def test_fixing_the_causes_completes_the_step_with_a_rules_section(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service = _develop(root)
    _violate(root)
    _complete(service)
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
    assert "ww rejected this step's completion 3 times" in rendered
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
    assert waived.checks_waived == "Checked by hand."
    assert waived.fix_required is None
    rendered = MarkdownOutputAdapter().render_instruction(waived)
    assert "The operator waived this step's checks: Checked by hand." in rendered
    accepted = _complete(service, "Done anyway.")
    assert accepted.item_name == "check"
    artifact = _develop_artifact(root).read_text(encoding="utf-8")
    assert "- `docs/header`: failed" in artifact
    # The waiver covers the verification too: the judged rule is self-declared.
    assert "- `develop/1`: self-declared" in artifact
    assert "Checks waived by the operator: Checked by hand." in artifact
    assert "Completions rejected before this one: 3." in artifact


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
        "TASK-1", artifact="Try.", summary_for_next="Done.", caller_role="worker"
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
            "--summary-for-next-step",
            "Done.",
        ]
    )

    assert code == 1
    assert "## Fix required" in capsys.readouterr().out


def test_lint_reports_the_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, git=False)

    assert main(["--root", str(root), "lint"]) == 0

    assert capsys.readouterr().out.endswith("Rules: 1 group, 2 rules\n")
