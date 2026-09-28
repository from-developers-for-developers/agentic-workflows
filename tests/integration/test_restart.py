# SPDX-License-Identifier: GPL-3.0-or-later
"""A restartable workflow abandons its unfinished run on a new start."""

from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.errors import ConfigurationError, StateError
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """workflows:
  - name: manual
    restartable: true
    steps:
      - collect: Collect the cases.
        items:
          shared: true
  - name: fix
    steps:
      - work: Fix it.
"""


def _service(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    return WorkflowService(Storage(tmp_path))


def test_a_new_start_abandons_the_unfinished_run_and_keeps_its_history(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    start_after_init(service, "manual", "TASK-1", agent="codex")
    service.next("TASK-1")
    service.add_item("TASK-1", WorkItem("case-1", "Upload."))
    service.complete("TASK-1", artifact="one", summary_for_next="one")
    # Round one is mid-way through its item stage; round two starts anyway.
    second = start_after_init(service, "manual", "TASK-1", agent="codex")
    assert second.run_id == "02-manual"

    first_state = service.tasks.read_execution_state("TASK-1", "01-manual")
    assert first_state is not None
    assert first_state.status == "abandoned"
    assert first_state.last_error == "abandoned by a new start of 'manual'"
    assert [item.id for item in service.items("TASK-1", "01-manual")] == ["case-1"]
    assert [item.id for item in service.items("TASK-1")] == ["case-1"]  # seeded
    old = service.instruction("TASK-1", "01-manual")
    assert old.status == "abandoned"
    rendered = MarkdownOutputAdapter().render_instruction(old)
    assert "## Run abandoned" in rendered
    assert "work on the task's current run" in rendered
    # Every task command addresses the open run, which is the new one; with
    # two runs, an unqualified instruction lists them as before.
    assert service.next("TASK-1").run_id == "02-manual"
    assert service.instruction("TASK-1").status == "task_summary"
    assert service.instruction("TASK-1", "02-manual").run_id == "02-manual"
    assert service.status("TASK-1", "02-manual").run_id == "02-manual"


def test_other_unfinished_runs_still_refuse_a_start(tmp_path: Path) -> None:
    service = _service(tmp_path)
    start_after_init(service, "fix", "TASK-2", agent="codex")
    with pytest.raises(StateError, match="unfinished run '01-fix' of workflow 'fix'"):
        service.start("fix", "TASK-2", agent="codex")
    # Restartable or not, a different workflow's run is never abandoned.
    with pytest.raises(StateError, match="unfinished run '01-fix'"):
        service.start("manual", "TASK-2", agent="codex")


def test_restartable_is_a_boolean(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        "workflows:\n  - name: t\n    restartable: yes please\n    steps:\n"
        "      - work: Work.\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="restartable must be true or false"):
        WorkflowService(Storage(tmp_path)).start("t", "TASK-3", agent="codex")
