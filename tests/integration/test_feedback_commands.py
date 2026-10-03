# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional post-workflow deduction from explicitly learnable artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.test_feedback import _analysis
from tests.workflow_helpers import configured_service
from ww.cli import main
from ww.config import parse_yaml_text
from ww.errors import ConfigurationError, StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService

WORKFLOW = """workflows:
  - name: task
    steps:
      - review: Review the naming.
        learnable: true
      - discuss: Confirm the result.
        interactive: true
"""


def _finish(root: Path) -> tuple[WorkflowService, object]:
    service = configured_service(root, WORKFLOW)
    service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    active = service.next("TASK-1")
    assert active.feedback_deduction_command is None
    assert (
        "Learn from operator feedback"
        not in MarkdownOutputAdapter().render_instruction(active)
    )
    with pytest.raises(StateError, match="completed workflow"):
        service.feedback_sources("TASK-1")
    service.complete(
        "TASK-1",
        artifact="Negative feedback: Use English names.",
        summary_for_next="Naming review done.",
    )
    service.next("TASK-1")
    service.interact("TASK-1", operator="Approved.", end=True)
    done = service.complete("TASK-1", artifact="Approved.", summary_for_next="Done.")
    assert done.item_name == "update-workflow-summary"
    done = service.complete("TASK-1", variables=(("summary", "Done."),))
    return service, done


def test_completion_suggests_skill_without_changing_plan_and_records_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service, done = _finish(tmp_path)
    assert done.status == "completed"
    assert done.feedback_deduction_command
    rendered = MarkdownOutputAdapter().render_instruction(done)
    assert "ww-deduce-feedback" in rendered
    assert "optional follow-up" in rendered
    sources = service.feedback_sources("TASK-1")
    assert len(sources["sources"]) == 1
    assert sources["sources"][0]["step"] == "review"
    before, snapshot = service.load("TASK-1")
    analysis = _analysis()
    analysis[0]["evidence"][0]["source"] = sources["sources"][0]["id"]
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(analysis))
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "feedback",
                "record",
                "TASK-1",
                "--analysis",
                str(path),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    point = result["points"][0]
    assert point["occurrences"] == 1
    assert point["last_encountered_at"] == sources["sources"][0]["encountered_at"]
    assert service.load("TASK-1") == (before, snapshot)
    assert (
        main(["--root", str(tmp_path), "feedback", "get", point["id"], "--json"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["id"] == point["id"]


def test_disabled_feedback_keeps_workflow_and_store_unchanged(tmp_path: Path) -> None:
    (tmp_path / "ww.json").write_text('{"feedback_learning": false}')
    service, done = _finish(tmp_path)
    assert done.feedback_deduction_command is None
    with pytest.raises(StateError, match="disabled"):
        service.record_feedback("TASK-1", [])
    assert not service.feedback.path.exists()


def test_interactive_alone_is_not_a_source(tmp_path: Path) -> None:
    service = configured_service(
        tmp_path, WORKFLOW.replace("learnable: true", "learnable: false")
    )
    service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    service.next("TASK-1")
    service.complete("TASK-1", artifact="Review.", summary_for_next="Reviewed.")
    service.next("TASK-1")
    service.interact("TASK-1", operator="Fix the naming.", end=True)
    done = service.complete(
        "TASK-1", artifact="Fix the naming.", summary_for_next="Done."
    )
    done = service.complete("TASK-1", variables=(("summary", "Done."),))
    assert done.feedback_deduction_command is None
    assert service.feedback_sources("TASK-1")["sources"] == []


@pytest.mark.parametrize("value", ['"true"', "null", "1"])
def test_learnable_requires_a_boolean(value: str) -> None:
    with pytest.raises(ConfigurationError, match="learnable must be true or false"):
        parse_yaml_text(
            WORKFLOW.replace("learnable: true", f"learnable: {value}"), "test"
        )


def test_learnable_requires_an_artifact() -> None:
    with pytest.raises(ConfigurationError, match="requires artifact"):
        parse_yaml_text(
            WORKFLOW.replace(
                "learnable: true", "learnable: true\n        artifact: false"
            ),
            "test",
        )


def test_sources_include_retained_loop_artifacts_and_saved_learnable_flag(
    tmp_path: Path,
) -> None:
    workflow = """workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        loop:
          - review: Review the changes.
            learnable: true
            break: No more findings.
          - fix: Fix the findings.
"""
    service = configured_service(tmp_path, workflow)
    service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    service.next("TASK-1")
    service.complete(
        "TASK-1", artifact="Use English names.", summary_for_next="Fix names."
    )
    service.next("TASK-1")
    service.complete(
        "TASK-1", artifact="Fixed names.", summary_for_next="Review again."
    )
    service.next("TASK-1")
    service.loop("TASK-1", artifact="No more findings.", summary_for_next="Done.")
    done = service.complete("TASK-1", variables=(("summary", "Reviewed."),))
    assert done.status == "completed"
    assert done.feedback_deduction_command
    # Later configuration edits cannot change a completed run's source selection.
    (tmp_path / "ww.yaml").write_text(
        workflow.replace("learnable: true", "learnable: false")
    )
    sources = service.feedback_sources("TASK-1")["sources"]
    assert len(sources) == 2
    assert len({source["id"] for source in sources}) == 2
    assert any("Use English names." in source["content"] for source in sources)
    assert any("No more findings." in source["content"] for source in sources)
