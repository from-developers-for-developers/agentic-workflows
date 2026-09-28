# SPDX-License-Identifier: GPL-3.0-or-later
"""Assessment outcomes: named on the pages, chosen with ``next --outcome``."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.errors import ConfigurationError, StateError
from ww.instructions import Instruction
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
md = MarkdownOutputAdapter()


def _assessed(
    tmp_path: Path, workflows: str
) -> tuple[WorkflowService, Instruction, Instruction]:
    """Run up to the assessment's completion; return its page and the next."""
    (tmp_path / "workflows.yaml").write_text(workflows, encoding="utf-8")
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


def test_a_delegating_manager_chooses_without_a_worker_preview(
    tmp_path: Path,
) -> None:
    (tmp_path / "workflows.yaml").write_text(DECLARED, encoding="utf-8")
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
            "TASK-1", artifact="Done.", summary_for_next="Done.", caller_role="worker"
        )

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
    path = tmp_path / "workflows.yaml"
    path.write_text(
        "workflows:\n  - name: m\n    steps:\n      - assess:\n"
        "          question: Q?\n          outcomes:\n            " + outcomes + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(path)
