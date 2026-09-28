# SPDX-License-Identifier: GPL-3.0-or-later
"""The manager's pages for a ``subagents: false`` step under ``auto``."""

from __future__ import annotations

from pathlib import Path

from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """workflows:
  - name: task
    steps:
      - coordinate: Coordinate it.
        subagents: false
"""


def test_a_step_the_manager_performs_selects_no_worker(tmp_path: Path) -> None:
    (tmp_path / "workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    md = MarkdownOutputAdapter()

    dispatch = md.render_instruction(
        service.start(
            "task",
            "TASK-1",
            agent="claudecode",
            init_artifact="Do it.",
            workflow_runtime="auto",
            caller_role="manager",
        )
    )
    assert "no worker is selected" in dispatch
    assert "--selected-agent" not in dispatch
    assert "`None`" not in dispatch
    assert "to the selected worker" not in dispatch

    work = md.render_instruction(service.next("TASK-1", caller_role="manager"))
    assert "## Manager: perform the `coordinate` assignment" in work
    assert "### Worker bootstrap" not in work
    assert "Coordinate it." in work
