# SPDX-License-Identifier: GPL-3.0-or-later
"""``complete --adjustments``: what the operator asked for during a step."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import configured_service
from ww.cli import main
from ww.errors import StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService

ASKED = "Use English names; keep the old flag."

WORKFLOW = """workflows:
  - name: task
    steps:
      - develop: Develop it.
        learnable: true
      - review: Review it.
"""

WITH_RULE = """workflows:
  - name: task
    steps:
      - develop: Develop it.
        rules:
          - Keep the public CLI unchanged.
      - review: Review it.
"""


def _develop(root: Path, workflow: str = WORKFLOW) -> WorkflowService:
    service = configured_service(root, workflow)
    service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    assert service.next("TASK-1").item_name == "develop"
    return service


def test_every_agent_step_page_asks_for_the_adjustments(tmp_path: Path) -> None:
    service = _develop(tmp_path)

    rendered = MarkdownOutputAdapter().render_instruction(service.status("TASK-1"))

    assert (
        "Any explicit change the operator asked for in this session for this "
        "step goes into the artifact and into `complete --adjustments`." in rendered
    )


def test_adjustments_are_stored_and_shown_to_the_next_step_and_in_status(
    tmp_path: Path,
) -> None:
    service = _develop(tmp_path)

    review = service.complete(
        "TASK-1",
        artifact="Done.",
        summary_for_next="Developed.",
        adjustments=f"  {ASKED}\n",
    )

    state, snapshot = service.load("TASK-1")
    develop = next(
        record
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.name == "develop"
    )
    assert develop.adjustments == ASKED
    assert review.previous_step_adjustments == ASKED
    rendered = MarkdownOutputAdapter().render_instruction(service.status("TASK-1"))
    assert "The operator asked for these changes during `develop`:" in rendered
    assert f"> {ASKED}" in rendered
    status = service.task_status("TASK-1").to_dict()
    assert status["adjustments"] == ASKED
    assert status["adjustments_step"] == "develop"


def test_a_step_without_adjustments_shows_none(tmp_path: Path) -> None:
    service = _develop(tmp_path)

    review = service.complete("TASK-1", artifact="Done.", summary_for_next="Done.")

    assert review.previous_step_adjustments is None
    assert "adjustments" not in service.task_status("TASK-1").to_dict()
    rendered = MarkdownOutputAdapter().render_instruction(service.status("TASK-1"))
    assert "The operator asked for these changes" not in rendered


def test_the_flag_reaches_the_cli_and_the_status_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _develop(tmp_path)
    capsys.readouterr()

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "complete",
                "TASK-1",
                "--artifact",
                "Done.",
                "--summary",
                "Done.",
                "--adjustments",
                ASKED,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "status", "TASK-1", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["adjustments"] == ASKED


def test_overlong_adjustments_are_refused(tmp_path: Path) -> None:
    service = _develop(tmp_path)

    with pytest.raises(StateError, match="--adjustments has 501 characters"):
        service.complete(
            "TASK-1",
            artifact="Done.",
            summary_for_next="Done.",
            adjustments="x" * 501,
        )


def test_a_held_completion_keeps_the_adjustments_for_the_verifier(
    tmp_path: Path,
) -> None:
    service = _develop(tmp_path, WITH_RULE)

    held = service.complete(
        "TASK-1", artifact="Done.", summary_for_next="Done.", adjustments=ASKED
    )

    assert held.verification is not None
    assert held.verification.adjustments == ASKED
    rendered = MarkdownOutputAdapter().render_instruction(held)
    assert "#### Operator adjustments" in rendered
    assert f"> {ASKED}" in rendered
    assert service.task_status("TASK-1").to_dict()["adjustments"] == ASKED
    verified = service.complete(
        "TASK-1",
        artifact="Verified.",
        rule_results=(
            json.dumps({"id": "develop/1", "status": "judged", "verdict": "pass"}),
        ),
    )
    assert verified.previous_step_adjustments == ASKED


def test_a_verification_refuses_adjustments(tmp_path: Path) -> None:
    service = _develop(tmp_path, WITH_RULE)
    held = service.complete("TASK-1", artifact="Done.", summary_for_next="Done.")
    assert held.verification is not None

    with pytest.raises(StateError, match="is a verification"):
        service.complete(
            "TASK-1",
            artifact="Verified.",
            adjustments=ASKED,
            rule_results=(
                json.dumps({"id": "develop/1", "status": "judged", "verdict": "pass"}),
            ),
        )


def test_adjustments_are_a_labelled_part_of_the_feedback_sources(
    tmp_path: Path,
) -> None:
    service = _develop(tmp_path)
    service.complete(
        "TASK-1", artifact="Done.", summary_for_next="Done.", adjustments=ASKED
    )
    service.complete("TASK-1", artifact="Reviewed.", summary_for_next="Reviewed.")
    done = service.complete("TASK-1", variables=(("summary", "Done."),))
    assert done.status == "completed"

    (source,) = service.feedback_sources("TASK-1")["sources"]

    assert source["step"] == "develop"
    assert source["adjustments"] == ASKED
    point = {
        "summary": "Names stay English.",
        "reason": "The operator corrected the names.",
        "enforcement": "reasoning",
        "approach": "Check names against the convention.",
        "evidence": [{"source": source["id"], "quote": "keep the old flag"}],
    }
    assert service.record_feedback("TASK-1", [point])


def test_the_deduce_feedback_skill_names_the_adjustments() -> None:
    skill = Path("src/ww/assets/ww-deduce-feedback_skill.md").read_text(
        encoding="utf-8"
    )

    assert "`adjustments`" in skill


def test_a_repair_completion_refuses_adjustments(tmp_path: Path) -> None:
    service = configured_service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - build: ~
        shell: "false"
        on_failure: fix
""",
    )
    service.start("task", "TASK-1")
    repair = service.next("TASK-1")
    assert repair.handler_repair is not None
    rendered = MarkdownOutputAdapter().render_instruction(repair)
    assert "`complete --adjustments`" not in rendered

    with pytest.raises(StateError, match="repair completion accepts only"):
        service.complete("TASK-1", artifact="Fixed.", adjustments=ASKED)
