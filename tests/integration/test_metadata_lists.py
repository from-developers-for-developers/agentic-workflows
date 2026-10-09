# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved metadata: ``append: true`` lists grow, and project metadata keeps one shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import configured_service, start_after_init
from ww.errors import StateError
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage
from ww.storage_adapters import ProjectMetadata

WORKFLOWS = """workflows:
  - name: review
    steps:
      - name: fetch
        description: "Already handled: {{ww.metadata.pull_request.handled}}."
        artifact: false
      - name: process
        description: Split by thread.
        items:
          steps:
            - name: resolve
              description: Resolve the threads.
              saves:
                - metadata.pull_request.handled: The root comment of a resolved thread.
                  append: true
                - metadata.pull_request.url: The pull request URL.
"""


def _service(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    return WorkflowService(Storage(tmp_path))


def _finish_run(service: WorkflowService, task: str) -> None:
    """Complete the built-in workflow summary; the satisfied gate passes itself."""
    summary = service.next(task)
    assert summary.item_name == "update-workflow-summary"
    assert service.complete(task, variables=(("summary", "Done."),)).status == (
        "completed"
    )


def _resolve_item(service: WorkflowService, task: str) -> None:
    service.resolve_item(task, "t1", actual_solution="b")
    service.report_item(task, "t1")


def _run_review(service: WorkflowService, task: str, handled: tuple[str, ...]) -> None:
    """Run one review pass that resolves ``handled`` in its single item."""
    service.next(task)
    service.add_item(task, WorkItem("t1", "A thread"))
    service.complete(task, artifact="collected", summary_for_next="One item.")
    service.next(task)
    _resolve_item(service, task)
    service.complete(
        task,
        artifact="handled",
        summary_for_next="Handled.",
        metadata_values=(
            ("pull_request.url", "https://example.test/pr/1"),
            *(("pull_request.handled", value) for value in handled),
        ),
    )
    _finish_run(service, task)


def test_an_append_key_grows_across_completions_and_reads_as_a_list(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    start_after_init(service, "review", "TASK-1", agent="codex")

    fetch = service.next("TASK-1")
    # Nothing saved yet: the placeholder resolves to nothing, not to itself.
    assert fetch.action_text == "Already handled: ."
    rendered = MarkdownOutputAdapter().render_instruction(fetch)
    assert "{{ww.metadata" not in rendered
    service.complete("TASK-1", summary_for_next="Fetched.")

    collect = service.next("TASK-1")
    service.add_item("TASK-1", WorkItem("t1", "A thread"))
    service.complete("TASK-1", artifact="collected", summary_for_next="One.")
    assert collect.item_name == "process"
    assert service.next("TASK-1").item_name == "resolve"
    handle = service.status("TASK-1")
    rendered = MarkdownOutputAdapter().render_instruction(handle)
    assert (
        "(a list: repeat `--metadata pull_request.handled=<value>` once per value"
        in rendered
    )

    # A scalar key is still required exactly once; the list key is optional.
    with pytest.raises(
        StateError, match="missing required task metadata.*pull_request.url"
    ):
        service.complete("TASK-1", artifact="x", summary_for_next="x")
    with pytest.raises(StateError, match="supplied more than once: pull_request.url"):
        service.complete(
            "TASK-1",
            artifact="x",
            summary_for_next="x",
            metadata_values=(("pull_request.url", "a"), ("pull_request.url", "b")),
        )

    _resolve_item(service, "TASK-1")
    service.complete(
        "TASK-1",
        artifact="handled",
        summary_for_next="Handled two threads.",
        metadata_values=(
            ("pull_request.url", "https://example.test/pr/1"),
            ("pull_request.handled", "4711"),
            ("pull_request.handled", "4718"),
            ("pull_request.handled", "4711"),
        ),
    )

    assert service.metadata("TASK-1") == {
        "pull_request": {
            "handled": ["4711", "4718"],
            "url": "https://example.test/pr/1",
        }
    }
    saved = json.loads((tmp_path / ".ww/tasks/TASK-1/metadata.json").read_text())
    assert saved["metadata"]["pull_request"]["handled"] == ["4711", "4718"]
    _finish_run(service, "TASK-1")

    # A later run of the same task appends and reads the whole list.
    service.start("review", "TASK-1", agent="codex", init_artifact="Second pass.")
    fetch = service.next("TASK-1")
    assert fetch.action_text == "Already handled: 4711, 4718."
    service.complete("TASK-1", summary_for_next="Fetched again.")
    _run_review(service, "TASK-1", ("4718", "4720"))
    assert service.metadata("TASK-1")["pull_request"]["handled"] == [
        "4711",
        "4718",
        "4720",
    ]


def test_a_project_scoped_append_key_merges_values_from_several_tasks(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: note
        description: "Seen so far: {{ww.project_metadata.seen}}."
        artifact: false
        saves:
          - project_metadata.seen: Something seen.
            append: true
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    for task, values in (("T-1", ("a", "b")), ("T-2", ("b", "c")), ("T-3", ())):
        start_after_init(service, "task", task, agent="codex")
        note = service.next(task)
        service.complete(
            task,
            summary_for_next="Noted.",
            metadata_values=tuple(("project_metadata.seen", value) for value in values),
        )
        assert note.item_name == "note"

    assert service.project_metadata() == {"seen": ["a", "b", "c"]}
    start_after_init(service, "task", "T-4", agent="codex")
    assert service.next("T-4").action_text == "Seen so far: a, b, c."


@pytest.mark.parametrize(
    "existing, output",
    [("result.existing", "result"), ("result", "result.existing")],
)
def test_invalid_project_metadata_shape_does_not_complete_source_item(
    tmp_path: Path, existing: str, output: str
) -> None:
    service = configured_service(
        tmp_path,
        f"""workflows:
  - name: task
    steps:
      - name: capture
        artifact: false
        saves:
          - project_metadata.{output}:
""",
    )
    service.project_metadata_store.write_project_metadata(
        ProjectMetadata(((existing, "old"),))
    )
    service.start("task", "TASK-METADATA", init_artifact="requirements")
    service.next("TASK-METADATA")

    with pytest.raises(StateError, match="conflicting project metadata key"):
        service.complete(
            "TASK-METADATA",
            metadata_values=((f"project_metadata.{output}", "new"),),
            summary_for_next="Done.",
        )

    state = service.tasks.read_execution_state("TASK-METADATA", "01-task")
    assert state is not None
    assert state.item_executions[1].status == "in_progress"
    assert state.pending_project_metadata is None


def test_concurrent_project_metadata_shape_conflict_can_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = configured_service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: capture
        artifact: false
        saves:
          - project_metadata.result:
""",
    )
    service.start("task", "TASK-CONCURRENT", init_artifact="requirements")
    service.next("TASK-CONCURRENT")
    original_commit = service.commit

    def commit_with_concurrent_shape(*args: object, **kwargs: object) -> None:
        state = args[0]
        if getattr(state, "pending_project_metadata", None) is not None:
            service.project_metadata_store.write_project_metadata(
                ProjectMetadata((("result.existing", "other"),))
            )
        original_commit(*args, **kwargs)

    monkeypatch.setattr(service, "commit", commit_with_concurrent_shape)
    with pytest.raises(StateError, match="incompatible metadata shape"):
        service.complete(
            "TASK-CONCURRENT",
            metadata_values=(("project_metadata.result", "new"),),
            summary_for_next="Done.",
        )

    state = service.tasks.read_execution_state("TASK-CONCURRENT", "01-task")
    assert state is not None
    assert state.item_executions[1].status == "completed"
    assert state.pending_project_metadata is not None

    monkeypatch.setattr(service, "commit", original_commit)
    service.project_metadata_store.write_project_metadata(ProjectMetadata())
    service.next("TASK-CONCURRENT")
    assert service.project_metadata() == {"result": "new"}
