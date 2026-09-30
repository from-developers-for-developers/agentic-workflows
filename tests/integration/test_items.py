# SPDX-License-Identifier: GPL-3.0-or-later
"""Dynamic item collection and per-item execution."""

import json
from pathlib import Path

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.items import WorkItem
from ww.service import WorkflowService
from ww.storage import Storage


def test_collected_items_expand_to_a_per_item_plan(tmp_path: Path, capsys) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: collect
        description: Collect source items.
        items:
          steps:
            - name: process
              description: Process it.
              item_phase: analyze
            - name: resolve
              description: Resolve it.
              item_phase: resolve
            - name: report
              description: Report it.
              item_phase: report
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    service.add_item("TASK-1", WorkItem("comment-1", "First comment"))
    ready = service.complete("TASK-1", artifact="collected", summary_for_next="Done.")

    snapshot = service.tasks.read_plan_snapshot("TASK-1", "01-task")
    assert snapshot is not None
    assert [item.name for item in snapshot.plan.items] == [
        "init",
        "collect",
        "process",
        "resolve",
        "report",
        "update-workflow-summary",
    ]
    assert [item.item_id for item in snapshot.plan.items[2:5]] == [
        "comment-1",
        "comment-1",
        "comment-1",
    ]
    assert ready.item_name == "process"

    active = service.next("TASK-1")
    assert (
        "./ww update-item TASK-1 --id comment-1 "
        '--processed-item="<agent-friendly analysis>"' in (active.action_text or "")
    )
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "update-item",
                "TASK-1",
                "--id",
                "comment-1",
                "--processed-item=analysis",
                "--role",
                "worker",
            ]
        )
        == 0
    )
    update_output = capsys.readouterr().out
    assert "Item `comment-1` updated successfully" in update_output
    assert "### Continue with completion" in update_output
    assert "./ww complete TASK-1 --role worker" in update_output
    assert "### Work instruction" not in update_output

    assert main(["--root", str(tmp_path), "item", "TASK-1", "--id", "comment-1"]) == 0
    item_output = json.loads(capsys.readouterr().out)
    assert item_output["id"] == "comment-1"
    assert item_output["processed_item"] == "analysis"

    service.complete("TASK-1", artifact="processed", summary_for_next="Done.")
    active = service.next("TASK-1")
    assert (
        "./ww update-item TASK-1 --id comment-1 "
        '--actual-solution="<actual solution>" --resolved=true'
        in (active.action_text or "")
    )
    service.update_item("TASK-1", "comment-1", actual_solution="fixed", resolved=True)
    service.complete("TASK-1", artifact="resolved", summary_for_next="Done.")
    active = service.next("TASK-1")
    assert "./ww update-item TASK-1 --id comment-1 --reported=true" in (
        active.action_text or ""
    )
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "update-item",
                "TASK-1",
                "--id",
                "comment-1",
                "--reported=true",
                "--role",
                "worker",
            ]
        )
        == 0
    )
    report_output = capsys.readouterr().out
    assert "Item `comment-1` updated successfully" in report_output
    assert "./ww complete TASK-1 --role worker" in report_output
    ready = service.complete("TASK-1", artifact="reported", summary_for_next="Done.")

    assert ready.item_name == "update-workflow-summary"
    assert service.items("TASK-1")[0].reported


def test_items_reads_run_and_items_from_one_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-SNAPSHOT", agent="codex")
    service.add_item("TASK-SNAPSHOT", WorkItem("item-1", "First item"))

    def unexpected_lookup(*_args, **_kwargs):
        raise AssertionError("items should use one aggregate snapshot")

    monkeypatch.setattr(service.tasks, "active_execution_run", unexpected_lookup)
    monkeypatch.setattr(service.tasks, "read_items", unexpected_lookup)

    assert service.items("TASK-SNAPSHOT") == (
        WorkItem("item-1", "First item"),
    )


def test_artifact_false_keeps_a_completed_step_out_of_artifact_storage(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
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


def test_per_item_stages_materialize_their_full_hook_lifecycle(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: collect
        items:
          steps:
            - name: process
              description: Process it.
              item_phase: analyze
              hooks:
                before_start:
                  - name: prepare
                    description: Prepare it.
                after_complete:
                  - name: clean-up
                    description: Clean it up.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "task", "TASK-3", agent="codex")
    service.next("TASK-3")
    service.add_item("TASK-3", WorkItem("comment-1", "First comment"))
    ready = service.complete("TASK-3", artifact="collected", summary_for_next="Done.")

    snapshot = service.tasks.read_plan_snapshot("TASK-3", "01-task")
    assert snapshot is not None
    assert [item.name for item in snapshot.plan.items] == [
        "init",
        "collect",
        "prepare",
        "process",
        "clean-up",
        "update-workflow-summary",
    ]
    assert [item.item_id for item in snapshot.plan.items[2:5]] == [
        "comment-1",
        "comment-1",
        "comment-1",
    ]
    assert [item.item_operation for item in snapshot.plan.items[2:5]] == [
        "process_item",
        "process_item",
        "process_item",
    ]
    assert ready.item_name == "prepare"


def test_item_materialization_is_a_complete_executable_plan_revision(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """handlers:
  - name: prepare
    shell: printf 'prepared\\n' >> expanded.txt

workflows:
  - name: task
    steps:
      - name: collect
        description: Collect source items.
        items:
          steps:
            - name: process
              description: Process it.
              item_phase: analyze
              hooks:
                before_start:
                  - name: prepare
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "task", "TASK-4", agent="codex")
    service.next("TASK-4")
    service.add_item("TASK-4", WorkItem("comment-1", "First comment"))
    service.add_item("TASK-4", WorkItem("comment-2", "Second comment"))
    ready = service.complete("TASK-4", artifact="collected", summary_for_next="Done.")

    snapshot = service.tasks.read_plan_snapshot("TASK-4", "01-task")
    state = service.tasks.read_execution_state("TASK-4", "01-task")
    assert snapshot is not None and state is not None
    assert snapshot.plan_revision == state.plan_revision == 2
    assert snapshot.template_plan is not None
    assert any(item.item_template for item in snapshot.template_plan.items)
    assert all(not item.item_template for item in snapshot.plan.items)

    expanded = [item for item in snapshot.plan.items if item.item_id]
    assert [item.parent for item in expanded] == [
        "collect/item-1",
        "collect/item-1",
        "collect/item-2",
        "collect/item-2",
    ]
    assert [item.ancestors for item in expanded] == [
        ("collect", "collect/item-1"),
        ("collect", "collect/item-1"),
        ("collect", "collect/item-2"),
        ("collect", "collect/item-2"),
    ]
    assert [item.step_ordinals for item in expanded] == [
        (2, 1, 1),
        (2, 1, 1),
        (2, 2, 1),
        (2, 2, 1),
    ]
    assert [step.path for step in state.steps] == ["init", "collect"]
    assert [step.path for step in state.steps[1].children] == [
        "collect/item-1",
        "collect/item-2",
    ]
    assert state.steps[1].children[0].children[0].path == "collect/item-1/process"

    prepare_records = [
        record
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.name == "prepare"
    ]
    assert [len(record.commands) for record in prepare_records] == [1, 1]
    assert prepare_records[0].commands[0].status == "completed"
    assert prepare_records[1].commands[0].status == "pending"
    assert ready.item_name == "process"
    assert (tmp_path / "expanded.txt").read_text(encoding="utf-8") == "prepared\n"

    service.next("TASK-4")
    service.update_item("TASK-4", "comment-1", processed_item="done")
    ready = service.complete("TASK-4", artifact="processed", summary_for_next="Done.")

    state = service.tasks.read_execution_state("TASK-4", "01-task")
    assert state is not None
    prepare_records = [
        record
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.name == "prepare"
    ]
    assert [record.commands[0].status for record in prepare_records] == [
        "completed",
        "completed",
    ]
    assert ready.item_name == "process"
    assert (tmp_path / "expanded.txt").read_text(encoding="utf-8") == (
        "prepared\nprepared\n"
    )


def test_a_loop_inside_per_item_stages_runs_independently_per_item(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: collect
        description: Collect source items.
        items:
          steps:
            - name: review
              loop:
                - check: Check it.
                  break: It is good.
                - fix: Fix it.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    service.add_item("TASK-1", WorkItem("a", "First"))
    service.add_item("TASK-1", WorkItem("b", "Second"))
    service.complete("TASK-1", artifact="collected", summary_for_next="Done.")

    snapshot = service.tasks.read_plan_snapshot("TASK-1", "01-task")
    assert snapshot is not None
    loop_ids = {
        item.item_id: item.loop_id
        for item in snapshot.plan.items
        if item.name == "check"
    }
    assert loop_ids["a"] != loop_ids["b"]
    assert all("{item}" not in (loop_id or "") for loop_id in loop_ids.values())

    for name in ("check", "fix"):
        page = service.next("TASK-1")
        assert (page.item_name, page.loop_iteration) == (name, 1)
        assert page.item_id.endswith(":item:a")
        service.complete("TASK-1", artifact="Done.", summary_for_next="Done.")
    again = service.next("TASK-1")
    assert (again.item_name, again.loop_iteration) == ("check", 2)
    assert again.item_id.endswith(":item:a")
    service.loop("TASK-1", artifact="Good.", summary_for_next="Done.")

    # The second item's loop starts at its own first round.
    first = service.next("TASK-1")
    assert (first.item_name, first.loop_iteration) == ("check", 1)
    assert first.item_id.endswith(":item:b")
