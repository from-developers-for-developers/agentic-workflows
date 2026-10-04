# SPDX-License-Identifier: GPL-3.0-or-later
"""Assessment outcomes: named on the pages, chosen with ``next --outcome``."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.config import load_configuration
from ww.errors import ConfigurationError, StateError
from ww.instructions import Instruction
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

DECLARED = """workflows:
  - name: merge
    steps:
      - merge: Merge.
      - assess:
          question: Were conflicts resolved in non-trivial code?
          outcomes:
            positive:
              steps:
                - review: Review the resolutions.
            negative:
              stop_workflow: true
      - tests: Run the tests.
"""
COMPACT = """workflows:
  - name: merge
    steps:
      - assess: Is the merge worth reviewing?
      - tests: Run the tests.
"""
DIRECT = """workflows:
  - name: merge
    steps:
      - assess:
          question: Were conflicts resolved in non-trivial code?
          positive:
            steps:
              - review-conflicts: Review the conflicts.
          negative:
            stop_workflow: true
      - tests: Run the tests.
"""
md = MarkdownOutputAdapter()


def _assessed(
    tmp_path: Path, workflows: str
) -> tuple[WorkflowService, Instruction, Instruction]:
    """Run up to the assessment's completion; return its page and the next."""
    (tmp_path / "ww.yaml").write_text(workflows, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    service.start("merge", "TASK-1", agent="codex", init_artifact="Merge.")
    page = service.next("TASK-1")
    if page.item_name != "assess":
        page = service.complete("TASK-1", artifact="Merged.", summary_for_next="Ok.")
    after = service.complete(
        "TASK-1", artifact="Conflicts in code.", summary_for_next="Assessed."
    )
    return service, page, after


def test_the_assessment_page_names_each_outcome_and_its_effect(
    tmp_path: Path,
) -> None:
    _, page, _ = _assessed(tmp_path, DECLARED)
    rendered = md.render_instruction(page)

    assert "### Possible outcomes" in rendered
    assert "- `positive` — continues with `review`." in rendered
    assert "- `negative` — ends the workflow here" in rendered
    # An undeclared standard answer is accepted too, and runs nothing extra.
    assert "- `mixed` — runs nothing extra and continues with `tests`." in rendered
    assert [outcome.label for outcome in page.assessment_outcomes] == [
        "positive",
        "negative",
        "mixed",
    ]


def test_the_next_page_asks_for_the_outcome_with_a_command_each(
    tmp_path: Path,
) -> None:
    _, _, after = _assessed(tmp_path, DECLARED)
    rendered = md.render_instruction(after)

    assert after.choosing_outcome_of == "assess"
    assert "choose the outcome of `assess`" in rendered
    assert "./ww next TASK-1 --role manager --outcome positive" in rendered
    assert "./ww next TASK-1 --role manager --outcome negative" in rendered
    # The plain command that ww would refuse is not offered.
    assert "./ww next TASK-1 --role manager\n" not in rendered
    assert "review" not in rendered.split("### Choose the outcome")[0]
    assert after.continuation_command == (
        "./ww next TASK-1 --role manager --outcome <outcome>"
    )


def test_a_stopping_outcome_completes_the_workflow(tmp_path: Path) -> None:
    service, _, _ = _assessed(tmp_path, DECLARED)

    done = service.next("TASK-1", outcome="negative", caller_role="manager")

    assert done.status == "completed"


def test_a_working_outcome_runs_and_the_workflow_continues(tmp_path: Path) -> None:
    service, _, _ = _assessed(tmp_path, DECLARED)

    review = service.next("TASK-1", outcome="positive", caller_role="manager")
    assert review.item_name == "review"
    # Showing the chosen step again needs no outcome.
    assert service.next("TASK-1", caller_role="manager").item_name == "review"
    tests = service.complete(
        "TASK-1",
        artifact="Reviewed.",
        summary_for_next="Reviewed.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    assert tests.item_name == "tests"


GATE = """workflows:
  - name: merge
    steps:
      - merge: Merge.
      - assess:
          question: Were conflicts resolved in non-trivial code?
          outcomes:
            positive:
              steps:
                - review: Review the resolutions.
      - tests: Run the tests.
"""


@pytest.mark.parametrize("answer", ["negative", "mixed"])
def test_an_undeclared_standard_outcome_skips_straight_past(
    tmp_path: Path, answer: str
) -> None:
    service, _, after = _assessed(tmp_path, GATE)
    assert f"--outcome {answer}" in md.render_instruction(after)

    tests = service.next("TASK-1", outcome=answer, caller_role="manager")

    assert tests.item_name == "tests"


def test_a_custom_label_must_still_be_declared(tmp_path: Path) -> None:
    service, _, _ = _assessed(tmp_path, GATE)

    with pytest.raises(StateError, match="unknown assessment outcome 'maybe'"):
        service.next("TASK-1", outcome="maybe", caller_role="manager")


def test_the_compact_form_gets_the_same_pages(tmp_path: Path) -> None:
    service, page, after = _assessed(tmp_path, COMPACT)

    assert "- `positive` — continues with `tests`." in md.render_instruction(page)
    assert "--outcome negative" in md.render_instruction(after)
    with pytest.raises(StateError, match="pending assess requires --outcome"):
        service.next("TASK-1", caller_role="manager")


def test_direct_standard_branches_use_the_existing_outcome_transitions(
    tmp_path: Path,
) -> None:
    service, page, _ = _assessed(tmp_path, DIRECT)

    assert [outcome.label for outcome in page.assessment_outcomes] == [
        "positive",
        "negative",
        "mixed",
    ]
    assert "continues with `review-conflicts`" in md.render_instruction(page)
    assert service.status("TASK-1").choosing_outcome_of == "assess"
    assert service.resume(*service.load("TASK-1")).choosing_outcome_of == "assess"
    chosen = service.next("TASK-1", outcome="positive", caller_role="manager")
    assert chosen.item_name == "review-conflicts"
    assert chosen.choosing_outcome_of is None
    assert "--outcome" not in md.render_instruction(chosen)


@pytest.mark.parametrize(
    ("branch", "message"),
    [
        (
            "          positive:\n"
            "            steps:\n"
            "              - review: Review it.\n"
            "          outcomes:\n"
            "            negative:\n"
            "              stop_workflow: true\n",
            ".outcomes cannot be combined with direct",
        ),
        (
            "          positiv:\n"
            "            steps:\n"
            "              - review: Review it.\n",
            ".positiv is not a direct assessment branch",
        ),
    ],
)
def test_invalid_direct_assessment_branches_report_their_paths(
    tmp_path: Path, branch: str, message: str
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        "workflows:\n  - name: m\n    steps:\n      - assess:\n"
        "          question: Q?\n" + branch,
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(path)


def test_direct_assessment_branches_require_a_question(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        "workflows:\n  - name: m\n    steps:\n      - assess:\n"
        "          positive:\n            stop_workflow: true\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigurationError, match=r"\.positive requires an assess question"
    ):
        load_configuration(path)


def test_a_delegating_manager_chooses_without_a_worker_preview(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(DECLARED, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "merge",
        "TASK-1",
        agent="codex",
        init_artifact="Merge.",
        workflow_runtime="auto",
        caller_role="manager",
    )
    for _ in ("merge", "assess"):
        service.next("TASK-1", caller_role="manager")
        service.complete(
            "TASK-1",
            artifact="Done.",
            summary_for_next="Done.",
            caller_role="worker",
            assignment=assignment_token(service, "TASK-1"),
        )

    returned = md.render_instruction(
        service.instruction(
            "TASK-1",
            caller_role="worker",
            assignment=assignment_token(service, "TASK-1"),
        )
    )
    # The worker that assessed hands back; choosing is the manager's.
    assert "## Worker: return control to the manager" in returned
    assert "choose the outcome" not in returned.split("\n### ")[0]

    shown = service.status("TASK-1", caller_role="manager")

    assert shown.choosing_outcome_of == "assess"
    assert shown.assignment_preview is None
    rendered = md.render_instruction(shown)
    assert "Upcoming assignment" not in rendered
    assert "to the selected worker" not in rendered
    assert "--outcome positive" in rendered


@pytest.mark.parametrize(
    ("outcomes", "message"),
    [
        (
            "positive:\n              stop_workflow: true\n"
            "              steps:\n                - x: X.",
            "must be true and stand alone",
        ),
        ("positive:\n              stop_workflow: false", "must be true"),
        (
            "positive:\n              stop_workflow: true\n"
            "            negative:\n              stop_workflow: true",
            "only outcomes that stop the workflow",
        ),
    ],
)
def test_invalid_stops_are_rejected(
    tmp_path: Path, outcomes: str, message: str
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        "workflows:\n  - name: m\n    steps:\n      - assess:\n"
        "          question: Q?\n          outcomes:\n            " + outcomes + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(path)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("first_outcome", ["positive", "negative"])
def test_per_item_assessments_keep_outcomes_in_their_own_item(
    tmp_path: Path, legacy: bool, first_outcome: str
) -> None:
    (tmp_path / "ww.yaml").write_text("""workflows:
  - task: ~
    steps:
      - collect: Collect comments.
        items:
          steps:
            - assess:
                question: Does this comment need operator input?
                outcomes:
                  positive:
                    steps:
                      - discuss: Discuss the question.
                  mixed:
                    steps:
                      - clarify: Clarify the question.
            - resolve: Resolve the comment.
""")
    service = WorkflowService(Storage(tmp_path))
    service.start("task", "TASK-1", agent="codex", init_artifact="Review.")
    service.next("TASK-1")
    for item_id in ("one", "two"):
        service.add_item("TASK-1", WorkItem(item_id, "Comment."))
    service.complete("TASK-1", artifact="Collected.", summary_for_next="Assess.")
    state, snapshot = service.load("TASK-1")
    for item in snapshot.plan.items:
        if item.assessment_parent is not None:
            assert "{item}" not in item.assessment_parent
    if legacy:
        plan = replace(
            snapshot.plan,
            items=tuple(
                replace(
                    item,
                    assessment_parent=(
                        item.assessment_parent.replace("/item-1/", "/{item}/").replace(
                            "/item-2/", "/{item}/"
                        )
                        if item.assessment_parent is not None
                        else None
                    ),
                )
                for item in snapshot.plan.items
            ),
        )
        snapshot = replace(snapshot, plan=plan)
        service.commit(replace(state, plan_digest=snapshot.plan_digest), snapshot)
    assert service.next("TASK-1").item_name == "assess"
    choice = service.complete(
        "TASK-1", artifact="Assessed.", summary_for_next="Choose."
    )
    assert choice.choosing_outcome_of == "assess"
    assert service.status("TASK-1").choosing_outcome_of == "assess"
    selected = service.next("TASK-1", outcome=first_outcome)
    if first_outcome == "positive":
        assert selected.item_name == "discuss"
        service.complete("TASK-1", artifact="Discussed.", summary_for_next="Resolve.")
        service.next("TASK-1")
    assert service.status("TASK-1").item_name == "resolve"
    service.complete("TASK-1", artifact="Resolved.", summary_for_next="Next comment.")
    assert service.next("TASK-1").item_name == "assess"
    service.complete("TASK-1", artifact="Uncertain.", summary_for_next="Clarify.")
    assert service.next("TASK-1", outcome="mixed").item_name == "clarify"
    state, snapshot = service.load("TASK-1")
    active = snapshot.plan.items[state.cursor]
    assert active.item_id == "two"
    skipped = [
        item.item_id
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if record.result == "skipped: assessment selected negative"
    ]
    assert skipped == (["one", "one"] if first_outcome == "negative" else [])
