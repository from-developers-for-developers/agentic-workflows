# SPDX-License-Identifier: GPL-3.0-or-later
"""A parallel step: its page, its branches' pages and artifacts, its gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import (
    assignment_token,
    configured_service,
    start_after_init,
)
from ww.cli.main import main
from ww.errors import StateError
from ww.instructions import Instruction
from ww.output_adapters.branch_pages import render_branch_page
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService

md = MarkdownOutputAdapter()

REVIEW = """workflows:
  - name: task
    steps:
      - develop: Develop.
      - name: review
        description: Merge the reviews.
        parallel: true
        artifact_from: develop
        steps:
          - security: Review for security issues.
            model: claude-opus-5-5
            reasoning: high
          - performance: Review for performance regressions.
          - quality: Review readability and tests.
      - fix: Fix the findings.
        artifact_from: review
"""


def _to_review(service: WorkflowService, runtime: str = "single") -> Instruction:
    start_after_init(service, "task", "TASK-1", workflow_runtime=runtime)
    if runtime == "auto":
        service.next("TASK-1", caller_role="manager")
        service.complete(
            "TASK-1",
            artifact="Developed.",
            summary_for_next="Developed it.",
            caller_role="worker",
            assignment=assignment_token(service, "TASK-1"),
        )
        return service.next("TASK-1", caller_role="manager")
    service.next("TASK-1")
    return service.complete(
        "TASK-1", artifact="Developed.", summary_for_next="Developed it."
    )


def _record(service: WorkflowService, *branches: str) -> None:
    for branch in branches:
        service.complete_branch(
            "TASK-1", branch, f"# {branch}\n\nNo findings.", caller_role="worker"
        )


def test_the_step_page_asks_for_one_subagent_per_branch(tmp_path: Path) -> None:
    page = _to_review(configured_service(tmp_path, REVIEW))

    assert page.item_name == "review"
    assert [branch.name for branch in page.parallel_branches] == [
        "security",
        "performance",
        "quality",
    ]
    security = page.parallel_branches[0]
    assert (security.requested_model, security.requested_reasoning) == (
        "claude-opus-5-5",
        "high",
    )
    assert security.artifact is None
    assert security.instruction_command.endswith(
        "instruction TASK-1 --role worker --branch security"
    )
    text = md.render_instruction(page)
    assert "### Parallel branches" in text
    assert "Launch one subagent per branch now, all of them together" in text
    assert "- Model: `claude-opus-5-5`" in text
    # The single session is told this is where it spawns subagents.
    assert "launch its branches as subagents" in text
    assert "not through a subagent" not in text
    assert page.to_dict()["parallel_branches"][0]["name"] == "security"


def test_a_branch_page_changes_nothing_and_names_its_command(tmp_path: Path) -> None:
    service = configured_service(tmp_path, REVIEW)
    _to_review(service)
    before, _ = service.load("TASK-1")

    page = service.branch_instruction("TASK-1", "security", caller_role="worker")

    after, _ = service.load("TASK-1")
    assert after == before
    assert page.step == "review"
    assert page.siblings == ("performance", "quality")
    assert page.action_text.startswith("Review for security issues.")
    assert "`develop` step" in page.action_text
    assert page.task_requirements
    assert page.completion_command.endswith(
        'complete TASK-1 --role worker --branch security --artifact="<whole result'
        ' in Markdown>"'
    )
    text = render_branch_page(page)
    assert "## Worker: perform the `security` branch of `review`" in text
    assert "- Reasoning: `high`" in text
    assert page.to_dict()["branch"]["name"] == "security"


def test_unknown_branches_and_other_steps_are_refused(tmp_path: Path) -> None:
    service = configured_service(tmp_path, REVIEW)
    start_after_init(service, "task", "TASK-1")
    service.next("TASK-1")

    with pytest.raises(StateError, match="no parallel step"):
        service.branch_instruction("TASK-1", "security")
    with pytest.raises(StateError, match="no parallel step"):
        service.complete_branch("TASK-1", "security", "x")

    service.complete("TASK-1", artifact="Developed.", summary_for_next="Done.")
    with pytest.raises(StateError, match="has no branch 'nope'"):
        service.branch_instruction("TASK-1", "nope")
    with pytest.raises(StateError, match="non-empty --artifact"):
        service.complete_branch("TASK-1", "security", "  ")


def test_the_step_completes_only_after_every_branch(tmp_path: Path) -> None:
    service = configured_service(tmp_path, REVIEW)
    _to_review(service)
    _record(service, "quality", "security")

    with pytest.raises(StateError, match="waits for the artifact of branch 'perf"):
        service.complete("TASK-1", artifact="Merged.", summary_for_next="Done.")

    recorded = service.complete_branch(
        "TASK-1", "performance", "Nothing slow.", caller_role="worker"
    )
    assert recorded.missing == ()
    assert recorded.artifact.endswith("steps/03-review/02-performance.md")
    assert "Nothing slow." in Path(recorded.artifact).read_text(encoding="utf-8")

    page = service.complete("TASK-1", artifact="Merged.", summary_for_next="Done.")

    assert page.item_name == "fix"
    # ``artifact_from`` gives every branch's artifact in declaration order,
    # then the step's own.
    text = page.action_text or ""
    order = [text.index(f"`{name}`: ") for name in ("security", "performance")]
    assert order == sorted(order)
    assert text.index("`quality`: ") > order[-1]
    assert "steps/03-review.md" in text


def test_recording_a_branch_again_replaces_its_artifact(tmp_path: Path) -> None:
    service = configured_service(tmp_path, REVIEW)
    _to_review(service)
    first = service.complete_branch("TASK-1", "security", "First.")
    second = service.complete_branch("TASK-1", "security", "Second.")

    assert first.artifact == second.artifact
    assert second.recorded == ("security",)
    assert "Second." in Path(second.artifact).read_text(encoding="utf-8")
    page = service.instruction("TASK-1", caller_role="manager")
    assert page.parallel_branches[0].artifact == second.artifact


def test_artifacts_lists_branches_as_they_record(tmp_path: Path) -> None:
    service = configured_service(tmp_path, REVIEW)
    _to_review(service)
    _record(service, "security")

    listed = [entry for entry in service.artifacts("TASK-1") if "branch" in entry]

    assert listed == [
        {
            "step": "review/security",
            "branch": "security",
            "artifact": ".ww/tasks/TASK-1/runs/01-task/steps/03-review/01-security.md",
            "path": str(
                tmp_path
                / ".ww/tasks/TASK-1/runs/01-task/steps/03-review/01-security.md"
            ),
        }
    ]


def test_in_auto_the_manager_launches_the_branches_and_completes(
    tmp_path: Path,
) -> None:
    service = configured_service(tmp_path, REVIEW)
    page = _to_review(service, runtime="auto")

    assert page.item_name == "review"
    assert page.role == "manager"
    text = md.render_instruction(page)
    assert "This step is parallel, so it is yours" in text
    assert "--role manager" in (page.continuation_command or "")

    _record(service, "security", "performance", "quality")
    with pytest.raises(StateError, match="return to your manager"):
        service.complete(
            "TASK-1", artifact="Merged.", summary_for_next="Done.", caller_role="worker"
        )
    page = service.complete(
        "TASK-1", artifact="Merged.", summary_for_next="Done.", caller_role="manager"
    )
    state, _ = service.load("TASK-1")
    review = next(
        record
        for record in state.item_executions
        if record.plan_item_id.startswith("task:review:step")
    )
    assert review.status == "completed"
    assert [name for name, _ in review.branch_artifacts] == [
        "security",
        "performance",
        "quality",
    ]


def test_the_cli_shows_and_records_a_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = configured_service(tmp_path, REVIEW)
    _to_review(service)
    common = ["--root", str(tmp_path)]

    assert (
        main(
            [
                *common,
                "instruction",
                "TASK-1",
                "--role",
                "worker",
                "--branch",
                "quality",
            ]
        )
        == 0
    )
    assert "branch of `review`" in capsys.readouterr().out

    arguments = [*common, "complete", "TASK-1", "--role", "worker"]
    assert main([*arguments, "--branch", "quality", "--summary", "x"]) == 1
    assert "drop --summary" in capsys.readouterr().err

    assert (
        main([*arguments, "--branch", "quality", "--artifact", "Fine.", "--json"]) == 0
    )
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["branch"] == "quality"
    assert recorded["missing"] == ["security", "performance"]
