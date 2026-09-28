# SPDX-License-Identifier: GPL-3.0-or-later
"""A task that needs the operator says so, apart from waiting on ww's own work."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.errors import StateError
from ww.output_adapters.json_adapter import JsonOutputAdapter
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

REJECTING = """handlers:
  - name: reject
    command:
      argv: ["false"]
workflows:
  - name: task
    steps:
      - name: work
        hooks:
          before_complete:
            - name: reject
"""

PLAIN = """workflows:
  - name: task
    steps:
      - name: work
"""


def _service(root: Path, config: str, runtime: str = "single") -> WorkflowService:
    (root / "ww-agentic-workflows.yaml").write_text(config, encoding="utf-8")
    service = WorkflowService(Storage(root))
    service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime=runtime,
        caller_role="manager",
    )
    service.next("TASK-1", caller_role="manager")
    return service


def test_a_failed_handler_awaits_the_operator(tmp_path: Path) -> None:
    service = _service(tmp_path, REJECTING)
    service.complete(
        "TASK-1", artifact="done", caller_role="worker", summary_for_next="Done."
    )

    status = service.status("TASK-1", caller_role="manager")
    data = json.loads(JsonOutputAdapter().render_instruction(status))
    rendered = MarkdownOutputAdapter().render_instruction(status)

    assert (data["control"], data["next_role"], data["operator_reason"]) == (
        "awaiting_operator",
        "operator",
        "handler_failed",
    )
    assert "## Operator decision: the automatic handler failed" in rendered
    assert "`ww` is waiting for the operator, the user" in rendered
    assert "./ww next TASK-1 --retry" in rendered
    assert any(
        "ww waits for the operator" in line
        for line in data["workflow_runtime_instruction"]
    )


def test_failed_agent_work_awaits_the_operator(tmp_path: Path) -> None:
    service = _service(tmp_path, PLAIN)

    failed = service.fail("TASK-1", "The API is down.", caller_role="worker")

    assert (failed.control, failed.next_role, failed.operator_reason) == (
        "awaiting_operator",
        "operator",
        "work_failed",
    )
    rendered = MarkdownOutputAdapter().render_instruction(
        service.status("TASK-1", caller_role="manager")
    )
    assert "## Operator decision: the step's work failed" in rendered


def test_a_delegated_worker_returns_the_operator_wait_to_its_manager(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, REJECTING, runtime="auto")
    failed = service.complete(
        "TASK-1", artifact="done", caller_role="worker", summary_for_next="Done."
    )
    rendered = MarkdownOutputAdapter().render_instruction(failed)

    assert failed.control == "awaiting_operator"
    assert "Return this `ww` response to the manager" in rendered
    assert "`ww` is waiting for the operator" not in rendered


def test_work_in_progress_has_no_operator_reason(tmp_path: Path) -> None:
    service = _service(tmp_path, PLAIN)

    active = service.status("TASK-1", caller_role="worker")

    assert active.control == "continue_worker"
    assert active.operator_reason is None
    assert active.to_dict()["operator_reason"] is None


def test_the_operator_is_not_a_caller_role(tmp_path: Path) -> None:
    service = _service(tmp_path, REJECTING)
    service.complete(
        "TASK-1", artifact="done", caller_role="worker", summary_for_next="Done."
    )

    with pytest.raises(StateError, match="caller role must be"):
        service.next("TASK-1", retry=True, caller_role="operator")  # type: ignore[arg-type]
