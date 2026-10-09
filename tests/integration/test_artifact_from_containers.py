# SPDX-License-Identifier: GPL-3.0-or-later
"""``artifact_from`` naming a group or an assessment: the latest artifact inside."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token, start_after_init
from ww.instructions import Instruction
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

md = MarkdownOutputAdapter()

ANALYSIS = """handlers:
  - name: investigate
    kind: prompt
    description: Investigate the cause.
  - name: research
    kind: prompt
    description: Research the options.
  - name: analyse
    steps:
      - assess:
          question: Is the cause clear?
          outcomes:
            positive:
              handler: investigate
            negative:
              handler: research
workflows:
  - name: medium-task
    steps:
      - name: analysis
        handler: analyse
      - name: develop
        description: Develop the change.
        artifact_from: analysis
"""


def _service(tmp_path: Path, workflows: str) -> WorkflowService:
    (tmp_path / "ww.yaml").write_text(workflows, encoding="utf-8")
    return WorkflowService(Storage(tmp_path))


def _complete(service: WorkflowService, text: str) -> Instruction:
    return service.complete(
        "TASK-1",
        artifact=text,
        summary_for_next="Done.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )


def _complete_bare(service: WorkflowService) -> Instruction:
    """Complete a step that saves no artifact."""
    return service.complete(
        "TASK-1",
        summary_for_next="Done.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )


def _open(service: WorkflowService, outcome: str | None = None) -> Instruction:
    return service.next("TASK-1", outcome=outcome, caller_role="manager")


@pytest.mark.parametrize(
    ("outcome", "step", "result"),
    [
        ("positive", "analysis/assess/positive", "Investigated."),
        ("negative", "analysis/assess/negative", "Researched."),
    ],
)
def test_a_group_holding_an_assessment_supplies_the_chosen_outcomes_artifact(
    tmp_path: Path, outcome: str, step: str, result: str
) -> None:
    service = _service(tmp_path, ANALYSIS)
    start_after_init(service, "medium-task", "TASK-1", agent="codex")
    assert _open(service).item_name == "assess"
    _complete(service, "Assessed.")
    assert _open(service, outcome).step == step
    _complete(service, result)

    develop = _open(service)

    assert develop.item_name == "develop"
    text = develop.action_text or ""
    assert f"artifact produced by the `{step}` step, the latest saved inside " in text
    assert "`analysis`" in text
    artifact = text.rsplit("`", 2)[-2]
    assert Path(artifact).read_text(encoding="utf-8").endswith(f"{result}\n")


def test_an_assessment_whose_outcome_saved_nothing_supplies_its_own_artifact(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - assess:
          question: Does it need a review?
          outcomes:
            positive:
              steps:
                - review: Review it.
      - name: ship
        description: Ship it.
        artifact_from: assess
""",
    )
    start_after_init(service, "task", "TASK-1", agent="codex")
    _open(service)
    _complete(service, "No review needed.")

    ship = _open(service, "negative")

    assert ship.item_name == "ship"
    text = ship.action_text or ""
    assert (
        "Use the artifact produced by the `assess` step as input to this work: `"
        in text
    )
    artifact = text.rsplit("`", 2)[-2]
    assert Path(artifact).read_text(encoding="utf-8").endswith("No review needed.\n")


def test_an_assessment_without_an_artifact_whose_outcome_saved_nothing_offers_none(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - assess:
          artifact: false
          question: Does it need a review?
          outcomes:
            positive:
              steps:
                - review: Review it.
      - name: ship
        description: Ship it.
        artifact_from: assess
""",
    )
    start_after_init(service, "task", "TASK-1", agent="codex")
    _open(service)
    _complete_bare(service)

    ship = _open(service, "negative")

    assert ship.item_name == "ship"
    assert "No artifact is available from `assess`: no step inside it" in (
        ship.action_text or ""
    )


def test_an_outcome_naming_its_assessment_still_gets_the_assessment_artifact(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - assess:
          question: Does it need a review?
          outcomes:
            positive:
              steps:
                - name: review
                  description: Review it.
                  artifact_from: assess
""",
    )
    start_after_init(service, "task", "TASK-1", agent="codex")
    _open(service)
    _complete(service, "Review needed.")

    review = _open(service, "positive")

    assert "Use the artifact produced by the `assess` step as input" in (
        review.action_text or ""
    )
