from pathlib import Path

from tests.workflow_helpers import start_after_init
from ww.service import WorkflowService
from ww.storage import Storage


def test_artifact_false_keeps_a_completed_step_out_of_artifact_storage(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: quiet-work
        description: Do work without an artifact.
        artifact: false
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "task", "TASK-2", agent="codex")
    service.next("TASK-2")
    ready = service.complete(
        "TASK-2",
        artifact="discard this",
        summary_for_next="Done.",
    )

    assert ready.item_name == "update-workflow-summary"
    state = service.tasks.read_execution_state("TASK-2", "01-task")
    assert state is not None
    assert state.item_executions[1].artifact is None
    assert not (
        tmp_path / ".ww/tasks/TASK-2/runs/01-task/steps/02-quiet-work.md"
    ).exists()
