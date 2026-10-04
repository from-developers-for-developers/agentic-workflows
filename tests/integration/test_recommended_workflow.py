# SPDX-License-Identifier: GPL-3.0-or-later
"""``recommended_next_workflow``: offered on completion, started on confirmation."""

from __future__ import annotations

from pathlib import Path

from ww.instructions import Instruction
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """workflows:
  - hotfix: Fix a bug on main.
    recommended_next_workflow: merge-to-dev
    steps:
      - fix: Fix it.
  - merge-to-dev: Merge the fix into dev.
    steps:
      - merge: Merge it.
"""


def _finish(service: WorkflowService, workflow: str, agent: str) -> Instruction:
    service.start(workflow, "TASK-1", agent=agent, init_artifact="Fix the bug.")
    service.next("TASK-1")
    service.complete("TASK-1", artifact="Fixed.", summary_for_next="Fixed.")
    return service.complete(
        "TASK-1", artifact="Fixed.", variables={"summary": "Fixed the bug."}
    )


def test_a_completed_run_offers_its_recommendation(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))

    done = _finish(service, "hotfix", "codex")
    page = MarkdownOutputAdapter().render_instruction(done)

    assert done.status == "completed"
    assert done.recommended_workflow == "merge-to-dev"
    assert done.to_dict()["recommended_workflow"] == "merge-to-dev"
    assert "### Recommended next workflow" in page
    assert "Do not start it on your own" in page
    assert "question-tool schema" in page
    assert "structured options when offered" in page
    assert "1. **Start merge-to-dev**" in page
    assert "2. **Stop here**" in page
    assert (
        "./ww start TASK-1 --workflow merge-to-dev --agent codex "
        '--requirements "<the request, normalized>" --role manager'
    ) in page
    # Showing the finished run again offers the same choice.
    again = service.status("TASK-1", caller_role="manager")
    assert again.recommended_workflow == "merge-to-dev"

    # The operator accepted: the next workflow starts on the same task.
    started = service.start(
        "merge-to-dev", "TASK-1", agent="codex", init_artifact="Merge into dev."
    )
    assert started.workflow == "merge-to-dev"


def test_a_workflow_without_a_recommendation_offers_nothing(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))

    done = _finish(service, "merge-to-dev", "codex")

    assert done.recommended_workflow is None
    assert "Recommended next workflow" not in (
        MarkdownOutputAdapter().render_instruction(done)
    )
