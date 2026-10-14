# SPDX-License-Identifier: GPL-3.0-or-later
"""Goto: a step sends the flow back to an earlier step, or ahead past others."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.cli import main
from ww.errors import ConfigurationError, StateError
from ww.instructions import Instruction
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

md = MarkdownOutputAdapter()
TASK = "TASK-1"

# ``review`` may send the work back to ``develop``, and ``develop`` may skip
# ahead to ``publish``.
REVIEWED = """workflows:
  - name: task
    allowed_gotos:
      - [review, develop]
      - [develop, publish]
    steps:
      - develop: Develop it.
      - review: Review it; when it needs more work, send it back to develop.
      - check: Check it.
      - publish: Publish it.
"""
# An assessment outcome goes back to ``develop``; the short form passes the
# assessment's summary, the long form a detail reading it.
ASSESSED = """workflows:
  - name: task
    steps:
      - develop: Develop it.
      - assess:
          question: Did the review find issues that must be fixed?
          outcomes:
            positive:
              goto: {goto}
      - publish: Publish it.
"""
# A goto between substeps, named by their full paths.
NESTED = """workflows:
  - name: task
    allowed_gotos:
      - [develop/review, develop/write]
    steps:
      - name: develop
        steps:
          - write: Write it.
          - review: Review it.
      - publish: Publish it.
"""
# A goto inside one items body.
ITEMS = """workflows:
  - name: task
    allowed_gotos:
      - [triage/review, triage/fix]
    steps:
      - name: triage
        description: Collect the issues.
        items:
          steps:
            - fix: Fix them.
            - review: Review the fixes.
      - publish: Publish it.
"""


def _service(
    tmp_path: Path, workflows: str = REVIEWED, **limits: int
) -> WorkflowService:
    (tmp_path / "ww.yaml").write_text(workflows, encoding="utf-8")
    if limits:
        (tmp_path / "ww.json").write_text(
            json.dumps({"limits": limits}), encoding="utf-8"
        )
    service = WorkflowService(Storage(tmp_path))
    service.start("task", TASK, agent="codex", init_artifact="Do it.")
    return service


def _complete(
    service: WorkflowService, artifact: str, summary: str, goto: str | None = None
) -> Instruction:
    return service.complete(
        TASK, artifact=artifact, summary_for_next=summary, goto=goto
    )


def _to_review(service: WorkflowService) -> Instruction:
    assert service.next(TASK).item_name == "develop"
    page = _complete(service, "Developed.", "Developed it.")
    assert page.item_name == "review"
    return page


def test_a_page_offers_the_gotos_its_step_may_take(tmp_path: Path) -> None:
    page = _to_review(_service(tmp_path))
    rendered = md.render_instruction(page)

    assert page.goto_targets == ("develop",)
    assert (
        "When the work instruction says to send the flow to another step, "
        "complete with `--goto develop` added to this command (allowed from "
        "this step)" in rendered
    )


def test_a_page_without_an_allowed_goto_offers_none(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _to_review(service)
    page = _complete(service, "Reviewed.", "Fine.")

    assert page.item_name == "check"
    assert page.goto_targets == ()
    assert "--goto" not in md.render_instruction(page)


def test_a_goto_back_runs_the_steps_again_with_what_sent_it_back(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _to_review(service)

    page = _complete(service, "Two issues.", "Fix the two issues.", goto="develop")

    assert page.item_name == "develop"
    assert page.goto_notice is not None
    assert page.goto_notice.kind == "target"
    assert page.goto_notice.text == "Fix the two issues."
    rendered = md.render_instruction(page)
    assert "### Sent back by a goto" in rendered
    assert "`review` sent the flow back to this step (goto round 1)" in rendered
    review_artifact = page.goto_notice.artifact
    assert review_artifact is not None
    assert review_artifact.endswith("steps/03-review.md")
    # The previous step is the one that sent the flow back: its result,
    # summary and artifact, is the reason, given once.
    assert page.previous_step == "review"
    assert page.previous_step_artifact == review_artifact
    assert "Why: `review`'s result, below." in rendered
    assert "Why, from" not in rendered

    state, _ = service.load(TASK)
    (goto,) = state.gotos
    assert (goto.source, goto.target, goto.direction, goto.round) == (
        "review",
        "develop",
        "back",
        1,
    )
    # The earlier round's records stay in the run's history.
    assert {record.summary_for_next for record in state.execution_history} >= {
        "Developed it.",
        "Fix the two issues.",
    }


def test_the_steps_on_the_way_back_know_they_run_again(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _to_review(service)
    _complete(service, "Two issues.", "Fix the two issues.", goto="develop")

    page = _complete(service, "Fixed.", "Fixed both.")

    assert page.item_name == "review"
    assert page.goto_notice is not None
    assert page.goto_notice.kind == "source"
    rendered = md.render_instruction(page)
    assert "### Running again after a goto" in rendered
    assert (
        "This step sent the flow back to `develop` (goto round 1), and the "
        "steps from `develop` to here ran again since." in rendered
    )
    assert f"Your earlier result: `{page.goto_notice.artifact}`" in rendered
    # Its previous step is the new round's develop, not its own earlier round.
    assert page.previous_step == "develop"
    assert page.previous_step_summary == "Fixed both."
    # Past the step that asked, the flow goes on as usual.
    after = _complete(service, "Clean.", "All fixed.")
    assert after.item_name == "check"
    assert after.goto_notice is None


def test_a_later_round_keeps_its_artifacts_apart(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _to_review(service)
    _complete(service, "Two issues.", "Fix the two issues.", goto="develop")
    _complete(service, "Fixed.", "Fixed both.")
    _complete(service, "Clean.", "All fixed.")

    steps = tmp_path / ".ww/tasks" / TASK / "runs/01-task/steps"
    assert sorted(path.name for path in steps.glob("*.md")) == [
        "01-init.md",
        "02-develop-1.md",
        "02-develop.md",
        "03-review-1.md",
        "03-review.md",
    ]
    assert "Developed." in (steps / "02-develop.md").read_text(encoding="utf-8")
    assert "Fixed." in (steps / "02-develop-1.md").read_text(encoding="utf-8")


def test_a_goto_ahead_skips_the_steps_between(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.next(TASK).item_name == "develop"

    page = _complete(service, "Nothing to do.", "Already done.", goto="publish")

    assert page.item_name == "publish"
    assert page.goto_notice is not None
    assert page.goto_notice.kind == "ahead"
    rendered = md.render_instruction(page)
    assert "### Reached by a goto" in rendered
    assert "`develop` sent the flow ahead to this step" in rendered
    state, snapshot = service.load(TASK)
    skipped = {
        item.step: record.result
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.step in {"review", "check"}
    }
    assert skipped == {
        "review": "skipped: goto from develop to publish",
        "check": "skipped: goto from develop to publish",
    }
    assert state.gotos[0].direction == "forward"


def test_a_goto_the_workflow_does_not_allow_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _to_review(service)

    with pytest.raises(StateError, match="'review' may not go to 'publish'"):
        _complete(service, "Reviewed.", "Fine.", goto="publish")
    _complete(service, "Reviewed.", "Fine.")
    with pytest.raises(StateError, match="allowed_gotos lists no goto from it"):
        _complete(service, "Checked.", "Fine.", goto="develop")


def test_a_goto_stops_for_the_operator_at_its_limit(tmp_path: Path) -> None:
    service = _service(tmp_path, gotos=1)
    _to_review(service)
    _complete(service, "Issues.", "Fix them.", goto="develop")
    _complete(service, "Fixed.", "Fixed.")

    stopped = _complete(service, "Still issues.", "Fix again.", goto="develop")

    assert stopped.status == "failed"
    assert stopped.operator_reason == "goto_limit"
    rendered = md.render_instruction(stopped)
    assert "Operator decision: a goto reached its limit" in rendered
    assert "that goto already ran 1 times (limits.gotos)" in rendered
    assert "To take the goto once more:" in rendered
    # Left untaken, the run continues after the step that asked.
    forced = service.next(TASK, force=True, force_reason="Good enough.")
    assert forced.item_name == "check"


def test_the_operator_may_let_a_goto_run_once_more(tmp_path: Path) -> None:
    service = _service(tmp_path, gotos=1)
    _to_review(service)
    _complete(service, "Issues.", "Fix them.", goto="develop")
    _complete(service, "Fixed.", "Fixed.")
    _complete(service, "Still issues.", "Fix again.", goto="develop")

    page = service.next(TASK)

    assert page.item_name == "develop"
    assert page.goto_notice is not None
    assert page.goto_notice.round == 2  # noqa: PLR2004


def test_an_assessment_outcome_goes_back_with_the_summary(tmp_path: Path) -> None:
    service = _service(tmp_path, ASSESSED.format(goto="develop"))
    assert service.next(TASK).item_name == "develop"
    assess = _complete(service, "Developed.", "Developed it.")
    assert "- `positive` — sends the flow to `develop` with a goto." in (
        md.render_instruction(assess)
    )
    _complete(service, "Two issues.", "Fix the two issues.")

    page = service.next(TASK, outcome="positive")

    assert page.item_name == "develop"
    assert page.goto_notice is not None
    assert page.goto_notice.source == "assess"
    assert page.goto_notice.text == "Fix the two issues."


def test_an_assessment_outcome_continues_when_it_has_no_goto(tmp_path: Path) -> None:
    service = _service(tmp_path, ASSESSED.format(goto="develop"))
    service.next(TASK)
    _complete(service, "Developed.", "Developed it.")
    _complete(service, "No issues.", "All good.")

    page = service.next(TASK, outcome="negative")

    assert page.item_name == "publish"


def test_a_goto_detail_reads_the_summary_and_the_asking_step(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        ASSESSED.format(
            goto='[develop, "Fix what {{ww.goto.from}} found: {{ww.goto.summary}}"]'
        ),
    )
    service.next(TASK)
    _complete(service, "Developed.", "Developed it.")
    _complete(service, "Two issues.", "Fix the two issues.")

    page = service.next(TASK, outcome="positive")

    assert page.goto_notice is not None
    assert page.goto_notice.text == "Fix what assess found: Fix the two issues."


def test_a_goto_between_substeps_uses_their_paths(tmp_path: Path) -> None:
    service = _service(tmp_path, NESTED)
    assert service.next(TASK).item_name == "write"
    review = _complete(service, "Written.", "Wrote it.")
    assert review.goto_targets == ("develop/write",)

    page = _complete(service, "Unclear.", "Rewrite it.", goto="develop/write")

    assert page.item_name == "write"
    assert page.goto_notice is not None
    assert page.goto_notice.kind == "target"


def test_a_goto_inside_an_items_body(tmp_path: Path) -> None:
    service = _service(tmp_path, ITEMS)
    assert service.next(TASK).item_name == "triage"
    service.add_item(TASK, WorkItem("1", "One issue."))
    assert _complete(service, "Collected.", "One issue.").item_name == "fix"
    assert _complete(service, "Fixed.", "Fixed it.").item_name == "review"

    page = _complete(service, "Not fixed.", "Fix it properly.", goto="triage/fix")

    assert page.item_name == "fix"
    assert page.goto_notice is not None


def test_a_goto_ends_the_workers_assignment(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(REVIEWED, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "task", TASK, agent="codex", workflow_runtime="auto", init_artifact="Do it."
    )
    service.next(TASK, caller_role="manager")
    service.complete(
        TASK,
        artifact="Developed.",
        summary_for_next="Developed it.",
        caller_role="worker",
        assignment=assignment_token(service, TASK),
    )
    service.next(TASK, caller_role="manager")

    ended = service.complete(
        TASK,
        artifact="Issues.",
        summary_for_next="Fix them.",
        caller_role="worker",
        assignment=assignment_token(service, TASK),
        goto="develop",
    )

    assert ended.next_role == "manager"
    page = service.next(TASK, caller_role="manager")
    assert page.item_name == "develop"


@pytest.mark.parametrize(
    ("gotos", "message"),
    [
        ("[[review, nowhere]]", "'nowhere' is not a step of the workflow"),
        ("[[nowhere, develop]]", "'nowhere' is not an agent step of the workflow"),
        ("[[review, review]]", "a step cannot go to itself"),
        ("[[review, init]]", "init takes no goto"),
        ("[review]", r"allowed_gotos\[0\] must be \[<from step path>"),
        ("[[review, develop], [review, develop]]", "lists \\[review, develop\\] twice"),
    ],
)
def test_allowed_gotos_are_checked(tmp_path: Path, gotos: str, message: str) -> None:
    workflows = REVIEWED.replace(
        "    allowed_gotos:\n      - [review, develop]\n      - [develop, publish]\n",
        f"    allowed_gotos: {gotos}\n",
    )
    (tmp_path / "ww.yaml").write_text(workflows, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))

    with pytest.raises(ConfigurationError, match=message):
        service.start("task", TASK, agent="codex", init_artifact="Do it.")


def test_a_goto_cannot_cross_an_items_boundary(tmp_path: Path) -> None:
    workflows = ITEMS.replace("[triage/review, triage/fix]", "[triage/review, publish]")
    (tmp_path / "ww.yaml").write_text(workflows, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="not in the same items body"):
        WorkflowService(Storage(tmp_path)).start(
            "task", TASK, agent="codex", init_artifact="Do it."
        )


def test_a_goto_detail_reads_only_the_goto_values(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        ASSESSED.format(goto='[develop, "Fix {{ww.task.id}}"]'), encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match="the detail reads"):
        WorkflowService(Storage(tmp_path)).start(
            "task", TASK, agent="codex", init_artifact="Do it."
        )


def test_an_outcome_goto_stands_alone(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        ASSESSED.format(goto="develop\n              stop_workflow: true"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="stand alone"):
        WorkflowService(Storage(tmp_path)).start(
            "task", TASK, agent="codex", init_artifact="Do it."
        )


def test_a_step_between_knows_it_is_on_the_way_back(tmp_path: Path) -> None:
    workflows = REVIEWED.replace(
        "      - review: Review it",
        "      - document: Document it.\n      - review: Review it",
    )
    service = _service(tmp_path, workflows)
    service.next(TASK)
    _complete(service, "Developed.", "Developed it.")
    _complete(service, "Documented.", "Documented it.")
    _complete(service, "Two issues.", "Fix the two issues.", goto="develop")

    page = _complete(service, "Fixed.", "Fixed both.")

    assert page.item_name == "document"
    assert page.goto_notice is not None
    assert page.goto_notice.kind == "path"
    rendered = md.render_instruction(page)
    assert (
        "`review` sent the flow back to `develop` (goto round 1), and this "
        "step runs again on the way back to `review`." in rendered
    )
    assert "Why, from `review`: Fix the two issues." in rendered
    assert f"`review`'s artifact: `{page.goto_notice.artifact}`" in rendered


def test_complete_takes_goto_on_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    _to_review(service)
    monkeypatch.chdir(tmp_path)

    code = main(
        [
            "complete",
            TASK,
            "--role",
            "worker",
            "--artifact",
            "Issues.",
            "--summary",
            "Fix them.",
            "--goto",
            "develop",
        ]
    )

    assert code == 0
    assert "### Sent back by a goto" in capsys.readouterr().out


def test_a_goto_waits_for_the_verifier_of_a_held_completion(tmp_path: Path) -> None:
    workflows = REVIEWED.replace(
        "      - review: Review it; when it needs more work, send it back "
        "to develop.\n",
        "      - name: review\n"
        "        description: Review it; when it needs more work, send it back.\n"
        "        rules: [Keep the public CLI unchanged.]\n",
    )
    (tmp_path / "ww.yaml").write_text(workflows, encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main", "."),
        ("config", "user.email", "t@e.st"),
        ("config", "user.name", "Test"),
        ("commit", "-q", "--allow-empty", "-m", "seed"),
    ):
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    service.start("task", TASK, agent="codex", init_artifact="Do it.")
    _to_review(service)
    (tmp_path / "notes.md").write_text("Two issues.\n", encoding="utf-8")

    held = _complete(service, "Two issues.", "Fix the two issues.", goto="develop")

    assert held.completion_held is True
    page = service.complete(
        TASK,
        artifact="Checked.",
        rule_results=(
            json.dumps(
                {
                    "id": "review/1",
                    "status": "judged",
                    "verdict": "pass",
                    "checked": "c",
                }
            ),
        ),
    )
    assert page.item_name == "develop"
    assert page.goto_notice is not None
    assert page.goto_notice.source == "review"


def test_an_outcome_goto_has_at_most_a_step_and_a_detail(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        ASSESSED.format(goto="[develop, one, two]"), encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match=r"or \[<step path>, <detail>\]"):
        WorkflowService(Storage(tmp_path)).start(
            "task", TASK, agent="codex", init_artifact="Do it."
        )


def test_an_inheriting_workflow_keeps_the_gotos_it_copies(tmp_path: Path) -> None:
    workflows = REVIEWED + "  - name: copy\n    inherit: task\n"
    (tmp_path / "ww.yaml").write_text(workflows, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    service.start("copy", TASK, agent="codex", init_artifact="Do it.")
    service.next(TASK)

    page = _complete(service, "Developed.", "Developed it.")

    assert page.goto_targets == ("develop",)
    (tmp_path / "ww.yaml").write_text(
        workflows + "    allowed_gotos: [[review, develop]]\n", encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="cannot declare allowed_gotos"):
        WorkflowService(Storage(tmp_path)).start(
            "copy", "TASK-2", agent="codex", init_artifact="Do it."
        )


def test_only_a_goto_detail_reads_the_goto_values(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n"
        '      - develop: "Develop {{ww.goto.summary}}."\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="unavailable variable"):
        WorkflowService(Storage(tmp_path)).start(
            "task", TASK, agent="codex", init_artifact="Do it."
        )
