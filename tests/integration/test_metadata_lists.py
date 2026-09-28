# SPDX-License-Identifier: GPL-3.0-or-later
"""Metadata keys declared ``append: true`` grow across completions and runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.errors import StateError
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """workflows:
  - name: review
    steps:
      - name: fetch
        description: "Already handled: {{metadata.pull_request.handled}}."
        artifact: false
      - name: process
        description: Split by thread.
        items:
          report_item: Resolve the thread.
          update_metadata:
            - handled: The root comment id of the resolved thread.
              key: pull_request.handled
              append: true
            - url: The pull request URL.
              key: pull_request.url
"""


def _service(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    return WorkflowService(Storage(tmp_path))


def _finish_run(service: WorkflowService, task: str) -> None:
    """Complete the built-in workflow summary so the run closes."""
    summary = service.status(task)
    if summary.item_status != "in_progress":
        summary = service.next(task)
    assert summary.item_name == "update-workflow-summary"
    assert service.complete(task, variables=(("summary", "Done."),)).status == (
        "completed"
    )


def _resolve_item(service: WorkflowService, task: str) -> None:
    service.update_item(
        task,
        "t1",
        processed_item="a",
        actual_solution="b",
        resolved=True,
        reported=True,
    )


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
            ("url", "https://example.test/pr/1"),
            *(("handled", value) for value in handled),
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
    assert "{{metadata" not in rendered
    service.complete("TASK-1", summary_for_next="Fetched.")

    collect = service.next("TASK-1")
    service.add_item("TASK-1", WorkItem("t1", "A thread"))
    handle = service.complete("TASK-1", artifact="collected", summary_for_next="One.")
    assert collect.item_name == "process"
    service.next("TASK-1")
    handle = service.status("TASK-1")
    rendered = MarkdownOutputAdapter().render_instruction(handle)
    assert "(a list: repeat `--metadata handled=<value>` once per value" in rendered

    # A scalar key is still required exactly once; the list key is optional.
    with pytest.raises(StateError, match="missing required task metadata.*url"):
        service.complete("TASK-1", artifact="x", summary_for_next="x")
    with pytest.raises(StateError, match="supplied more than once: url"):
        service.complete(
            "TASK-1",
            artifact="x",
            summary_for_next="x",
            metadata_values=(("url", "a"), ("url", "b")),
        )

    _resolve_item(service, "TASK-1")
    service.complete(
        "TASK-1",
        artifact="handled",
        summary_for_next="Handled two threads.",
        metadata_values=(
            ("url", "https://example.test/pr/1"),
            ("handled", "4711"),
            ("handled", "4718"),
            ("handled", "4711"),
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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: note
        description: "Seen so far: {{project_metadata.seen}}."
        artifact: false
        update_metadata:
          - seen: Something seen.
            key: seen
            scope: project
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
            metadata_values=tuple(("seen", value) for value in values),
        )
        assert note.item_name == "note"

    assert service.project_metadata() == {"seen": ["a", "b", "c"]}
    start_after_init(service, "task", "T-4", agent="codex")
    assert service.next("T-4").action_text == "Seen so far: a, b, c."
