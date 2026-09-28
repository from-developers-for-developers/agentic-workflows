# SPDX-License-Identifier: GPL-3.0-or-later
"""Items a task shares across its runs: reconciled, not split again."""

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.errors import ConfigurationError, StateError
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """workflows:
  - name: manual
    steps:
      - name: collect
        description: Read the test cases and make the items match them.
        items:
          shared: true
          process_item: Verify it.
  - name: plain
    steps:
      - name: collect
        description: Split it.
        items: ~
"""


def _service(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    return WorkflowService(Storage(tmp_path))


def _run_round(service: WorkflowService, task_id: str, results: dict[str, str]) -> None:
    """Complete the collection and every item's stage with the given results."""
    service.complete(task_id, artifact="collected", summary_for_next="Cases.")
    for item_id, result in results.items():
        service.next(task_id)
        service.update_item(
            task_id, item_id, actual_solution=result, resolved=True, reported=True
        )
        service.complete(task_id, artifact=result, summary_for_next=result)


def _finish_run(service: WorkflowService, task_id: str) -> None:
    """Complete the workflow summary that closes a run."""
    service.next(task_id)
    service.complete(task_id, variables=(("summary", "Round done."),))


def test_a_shared_flow_seeds_every_run_and_reconciles_instead_of_splitting(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    md = MarkdownOutputAdapter()

    # Round one: nothing stored, so the collection is a split.
    start_after_init(service, "manual", "TASK-1", agent="codex")
    first = service.next("TASK-1")
    assert (first.shared_items, first.stored_items) == (True, ())
    rendered = md.render_instruction(first)
    assert "### Stored items" in rendered
    assert "none are stored yet: split as instructed above" in rendered
    service.add_item("TASK-1", WorkItem("upload-large", "Upload a 10 MB image."))
    service.add_item("TASK-1", WorkItem("upload-empty", "Upload an empty file."))
    stored = json.loads((tmp_path / ".ww/tasks/TASK-1/items.json").read_text())
    assert [item["id"] for item in stored["items"]] == ["upload-large", "upload-empty"]
    _run_round(service, "TASK-1", {"upload-large": "passed", "upload-empty": "failed"})
    assert service.instruction("TASK-1").item_name == "update-workflow-summary"
    shared = service.tasks.read_shared_items("TASK-1")
    assert [(i.id, i.actual_solution, i.resolved) for i in shared] == [
        ("upload-large", "passed", True),
        ("upload-empty", "failed", True),
    ]

    # Round two: a new run starts from the stored items with outcomes cleared,
    # and the collection reconciles.
    _finish_run(service, "TASK-1")
    second = start_after_init(service, "manual", "TASK-1", agent="codex")
    assert second.run_id == "02-manual"
    seeded = service.items("TASK-1")
    assert [
        (i.id, i.item, i.actual_solution, i.resolved, i.reported) for i in seeded
    ] == [
        ("upload-large", "Upload a 10 MB image.", "", False, False),
        ("upload-empty", "Upload an empty file.", "", False, False),
    ]
    collect = service.next("TASK-1")
    assert [item.id for item in collect.stored_items] == [
        "upload-large",
        "upload-empty",
    ]
    rendered = md.render_instruction(collect)
    assert "Do not split again: compare the source with this list" in rendered
    assert "- `upload-empty`: Upload an empty file." in rendered
    assert "./ww remove-item TASK-1 --id <id>" in rendered
    assert "./ww update-item TASK-1 --id <id> --item <text>" in rendered

    # Reconcile: reword one, drop one, add one.
    service.update_item("TASK-1", "upload-large", item="Upload a 25 MB image.")
    service.remove_item("TASK-1", "upload-empty")
    service.add_item("TASK-1", WorkItem("upload-slow", "Upload on a slow link."))
    with pytest.raises(StateError, match="was not found"):
        service.remove_item("TASK-1", "upload-empty")
    assert [(i.id, i.item) for i in service.items("TASK-1")] == [
        ("upload-large", "Upload a 25 MB image."),
        ("upload-slow", "Upload on a slow link."),
    ]
    _run_round(service, "TASK-1", {"upload-large": "passed", "upload-slow": "passed"})

    # Both rounds keep their own copy; the store holds the latest.
    assert [i.actual_solution for i in service.items("TASK-1", "01-manual")] == [
        "passed",
        "failed",
    ]
    latest = service.tasks.read_shared_items("TASK-1")
    assert [(i.id, i.actual_solution) for i in latest] == [
        ("upload-large", "passed"),
        ("upload-slow", "passed"),
    ]

    # Text and removal are collection-time operations only.
    with pytest.raises(StateError, match="only while the collection step"):
        service.update_item("TASK-1", "upload-large", item="Later.")
    with pytest.raises(StateError, match="only while the collection step"):
        service.remove_item("TASK-1", "upload-large")


def test_fresh_items_forgets_the_store_and_references_guard_removal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    start_after_init(service, "manual", "TASK-2", agent="codex")
    service.next("TASK-2")
    service.add_item("TASK-2", WorkItem("a", "A."))
    service.add_item("TASK-2", WorkItem("b", "B.", reference_to_id="a"))
    with pytest.raises(StateError, match="'a' is referenced by b"):
        service.remove_item("TASK-2", "a")
    _run_round(service, "TASK-2", {"a": "ok", "b": "ok"})
    _finish_run(service, "TASK-2")

    root = ["--root", str(tmp_path)]
    assert (
        main(
            [
                *root,
                "start",
                "TASK-2",
                "--workflow",
                "manual",
                "--agent",
                "codex",
                "--init-artifact",
                "Again.",
                "--fresh-items",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert service.items("TASK-2") == ()
    assert service.tasks.read_shared_items("TASK-2") == ()
    service.next("TASK-2")
    assert main([*root, "add-item", "TASK-2", "--id", "c", "--item", "C."]) == 0
    reword = [*root, "update-item", "TASK-2", "--id", "c", "--item", "C, reworded."]
    assert main(reword) == 0
    assert main([*root, "remove-item", "TASK-2", "--id", "c"]) == 0
    out = capsys.readouterr().out
    assert '"item": "C, reworded."' in out
    assert service.items("TASK-2") == ()
    assert main([*root, "remove-item", "TASK-2", "--id", "c"]) == 1
    assert "was not found" in capsys.readouterr().err


def test_a_plain_flow_keeps_items_to_its_run(tmp_path: Path) -> None:
    service = _service(tmp_path)
    start_after_init(service, "plain", "TASK-3", agent="codex")
    plain = service.next("TASK-3")
    assert (plain.shared_items, plain.stored_items) == (False, ())
    assert "### Stored items" not in MarkdownOutputAdapter().render_instruction(plain)
    service.add_item("TASK-3", WorkItem("x", "X."))
    assert not (tmp_path / ".ww/tasks/TASK-3/items.json").exists()
    assert service.tasks.read_shared_items("TASK-3") == ()
    service.reset("TASK-3")


def test_shared_is_a_boolean_on_the_items_mapping(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        "workflows:\n  - task: ~\n    steps:\n      - collect: Collect.\n"
        "        items:\n          shared: yes please\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="shared must be true or false"):
        WorkflowService(Storage(tmp_path)).start("task", "TASK-4", agent="codex")
