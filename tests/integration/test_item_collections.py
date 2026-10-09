# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded collection contracts exercised through the public service."""

from pathlib import Path

import pytest

from tests.workflow_helpers import configured_service, start_after_init
from ww.errors import StateError
from ww.items import WorkItem
from ww.service import WorkflowService

TASK = "TASK-1"


def service(root: Path, body: str = "", settings: str = "") -> WorkflowService:
    text = (
        "workflows:\n  - name: review\n    steps:\n"
        "      - name: feedback\n        description: One item per comment.\n"
        "        items:\n"
        + settings
        + "          steps:"
        + ("\n" + body if body else " []\n")
        + "      - finish: Summarize.\n"
    )
    result = configured_service(root, text)
    start_after_init(result, "review", TASK, agent="codex", workflow_runtime="single")
    result.next(TASK)
    return result


def complete(svc: WorkflowService) -> None:
    svc.complete(TASK, artifact="Work completed.", summary_for_next="Done.")


def mark(svc: WorkflowService, item: str) -> None:
    svc.resolve_item(TASK, item)
    svc.report_item(TASK, item)


@pytest.mark.parametrize("count", [0, 1, 5, 50])
def test_steps_do_not_depend_on_item_count(tmp_path: Path, count: int) -> None:
    svc = service(
        tmp_path,
        "            - develop: Fix together.\n"
        "            - send-replies: Report each.\n",
    )
    _, original = svc.load(TASK)
    for i in range(count):
        svc.add_item(TASK, WorkItem(str(i), "Same shared fix"))
    complete(svc)
    assert svc.next(TASK).item_name == "develop"
    complete(svc)  # unresolved records do not gate intermediate steps
    assert svc.next(TASK).item_name == "send-replies"
    for i in range(count):
        mark(svc, str(i))
    complete(svc)
    _, final = svc.load(TASK)
    assert original.plan == final.plan
    assert svc.next(TASK).item_name == "finish"


def test_final_boundary_resumes_without_replaying_work(tmp_path: Path) -> None:
    svc = service(tmp_path, "            - develop: Fix it.\n")
    svc.add_item(TASK, WorkItem("a", "Obligation"))
    complete(svc)
    svc.next(TASK)
    complete(svc)
    page = svc.next(TASK)
    state, snapshot = svc.load(TASK)
    assert snapshot.plan.items[state.cursor].item_operation == "complete_collection"
    before = state.item_executions
    assert "a: unresolved" in page.action_text
    with pytest.raises(StateError, match="incomplete"):
        complete(svc)
    svc.resolve_item(TASK, "a", actual_solution="Rejected with explanation")
    with pytest.raises(StateError, match="unreported"):
        complete(svc)
    assert not svc.item(TASK, "a").reported
    svc.report_item(TASK, "a")
    reloaded = WorkflowService(svc.storage)
    assert reloaded.load(TASK)[0].cursor == state.cursor
    complete(reloaded)
    after, _ = reloaded.load(TASK)
    assert after.item_executions[: state.cursor] == before[: state.cursor]
    assert reloaded.next(TASK).item_name == "finish"


def test_fields_apply_to_all_items_and_late_additions(tmp_path: Path) -> None:
    svc = service(
        tmp_path,
        """            - bootstrap: Save IDs.
              saves:
                - item.field.source: Source ID.
            - develop: Fix.
""",
    )
    for name in ("a", "b"):
        svc.add_item(TASK, WorkItem(name, "A source comment"))
    complete(svc)
    svc.next(TASK)
    svc.update_item(TASK, "a", fields={"source": "100"})
    with pytest.raises(StateError, match="b: field.source"):
        complete(svc)
    svc.update_item(TASK, "b", fields={"source": " "})
    with pytest.raises(StateError, match="b: field.source"):
        complete(svc)
    svc.update_item(TASK, "b", fields={"source": "101"})
    complete(svc)
    svc.next(TASK)
    svc.add_item(TASK, WorkItem("late", "Discovered later"))
    for name in ("a", "b", "late"):
        mark(svc, name)
    complete(svc)
    page = svc.next(TASK)
    assert "late: field.source" in page.action_text
    svc.update_item(TASK, "late", fields={"source": "102"})
    svc.update_item(TASK, "a", fields={"source": ""})
    with pytest.raises(StateError, match="a: field.source"):
        complete(svc)
    svc.update_item(TASK, "a", fields={"source": "100"})
    complete(svc)
    assert svc.next(TASK).item_name == "finish"


def test_independent_links_and_reopen(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.add_item(TASK, WorkItem("a", "First"))
    svc.add_item(TASK, WorkItem("b", "Second", reference_to_id="a"))
    mark(svc, "a")
    with pytest.raises(StateError, match="resolution first"):
        svc.report_item(TASK, "b")
    mark(svc, "b")
    svc.resolve_item(TASK, "b", reopen=True)
    assert not svc.item(TASK, "b").reported
    assert svc.item(TASK, "a").reported
    svc.update_item(TASK, "a", item="Corrected obligation")
    assert not svc.item(TASK, "a").resolved
    with pytest.raises(StateError, match="referenced"):
        svc.remove_item(TASK, "a")
    svc.remove_item(TASK, "b")
    svc.remove_item(TASK, "a")
    complete(svc)
    assert svc.next(TASK).item_name == "finish"


def test_nested_contexts_isolate_identical_ids(tmp_path: Path) -> None:
    svc = service(
        tmp_path,
        """            - inner: Split inner work.
              items:
                steps:
                  - develop: Handle inner work.
            - outer-work: Finish outer work.
""",
    )
    svc.add_item(TASK, WorkItem("same", "Outer"))
    complete(svc)
    svc.next(TASK)
    svc.add_item(TASK, WorkItem("same", "Inner"))
    assert svc.item(TASK, "same").item == "Inner"
    assert svc.item(TASK, "same", context="feedback").item == "Outer"
    with pytest.raises(StateError, match="nearest context"):
        svc.resolve_item(TASK, "same", context="feedback")
    complete(svc)
    svc.next(TASK)
    mark(svc, "same")
    complete(svc)
    assert svc.next(TASK).item_name == "outer-work"
    assert not svc.item(TASK, "same").resolved
    mark(svc, "same")
    complete(svc)
    assert svc.next(TASK).item_name == "finish"
    with pytest.raises(StateError, match="nearest context"):
        svc.add_item(TASK, WorkItem("late", "Closed"), context="feedback")


def test_prompt_tokens_are_lookup_references(tmp_path: Path) -> None:
    svc = service(
        tmp_path,
        '            - develop: "{{ww.item.text}} {{ww.item.field.source}} '
        '{{ww.item.text}} {{ww.task.id}}"\n',
    )
    svc.add_item(TASK, WorkItem("a", "Do not substitute this"))
    complete(svc)
    page = svc.next(TASK)
    assert "{{ww.item.text}}" in page.action_text
    assert TASK in page.action_text
    assert "Do not substitute this" not in page.action_text
    assert page.action_text.count("--get text") == 1
    for command in ("items", "item", "resolve-item", "report-item"):
        assert f"{command} {TASK} --context feedback" in page.action_text
    assert svc.item(TASK, "a").get("resolved") is False
    assert svc.item(TASK, "a").get("actual_solution") == ""
    with pytest.raises(StateError, match="has no field"):
        svc.item(TASK, "a").get("field.missing")


def test_successful_command_waits_for_fields_without_replay(tmp_path: Path) -> None:
    svc = service(
        tmp_path,
        """            - run: Once.
              shell: echo ran >> effects.txt
              saves:
                - item.field.result: Per-item outcome.
            - develop: Continue.
""",
    )
    svc.add_item(TASK, WorkItem("a", "Case"))
    complete(svc)
    page = svc.next(TASK)
    assert "field.result" in page.action_text
    assert (tmp_path / "effects.txt").read_text() == "ran\n"
    assert svc.item(TASK, "a").field("result") is None
    with pytest.raises(StateError, match="field.result"):
        complete(svc)
    svc.update_item(TASK, "a", fields={"result": "failed as expected"})
    complete(svc)
    assert svc.next(TASK).item_name == "develop"
    assert (tmp_path / "effects.txt").read_text() == "ran\n"


def test_stale_run_cannot_write(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.add_item(TASK, WorkItem("a", "Case"))
    with pytest.raises(StateError, match="stale"):
        svc.resolve_item(TASK, "a", run_id="old-run")
    assert not svc.item(TASK, "a").resolved


def test_persistent_runs_clear_outcomes_and_keep_history(tmp_path: Path) -> None:
    svc = service(tmp_path, settings="          persistent: true\n")
    svc.add_item(TASK, WorkItem("a", "Case", fields=(("source", "100"),)))
    mark(svc, "a")
    old_run = svc.load(TASK)[0].run_id
    complete(svc)
    svc.next(TASK)
    complete(svc)
    svc.next(TASK)
    svc.complete(TASK, variables=(("summary", "Done."),))
    start_after_init(svc, "review", TASK, agent="codex", workflow_runtime="single")
    svc.next(TASK)
    current = svc.item(TASK, "a")
    assert current.field("source") == "100"
    assert not current.resolved and not current.reported
    assert svc.item(TASK, "a", run_id=old_run, context="feedback").reported


def test_group_save_is_checked_at_group_boundary(tmp_path: Path) -> None:
    svc = service(
        tmp_path,
        """            - name: grouped
              saves:
                - item.field.result: Every outcome.
              steps:
                - first: Gather input.
                - last: Finish work.
            - after: Later work.
""",
    )
    svc.add_item(TASK, WorkItem("a", "Case"))
    complete(svc)
    assert svc.next(TASK).item_name == "first"
    complete(svc)
    assert svc.next(TASK).item_name == "last"
    complete(svc)
    page = svc.next(TASK)
    assert "a: field.result" in page.action_text
    with pytest.raises(StateError, match="field.result"):
        complete(svc)
    svc.update_item(TASK, "a", fields={"result": "done"})
    complete(svc)
    assert svc.next(TASK).item_name == "after"


def test_multiple_contexts_are_independent_and_reads_require_choice(
    tmp_path: Path,
) -> None:
    svc = configured_service(
        tmp_path,
        """workflows:
  - name: review
    steps:
      - first: Split first.
        items: ~
      - second: Split second.
        items: ~
      - finish: Summarize.
""",
    )
    start_after_init(svc, "review", TASK, agent="codex", workflow_runtime="single")
    svc.next(TASK)
    svc.add_item(TASK, WorkItem("same", "First"))
    mark(svc, "same")
    complete(svc)
    svc.next(TASK)
    svc.add_item(TASK, WorkItem("same", "Second"))
    assert not svc.item(TASK, "same").resolved
    mark(svc, "same")
    complete(svc)
    svc.next(TASK)
    with pytest.raises(StateError, match="choices"):
        svc.items(TASK)
    assert svc.item(TASK, "same", context="first").item == "First"
    assert svc.item(TASK, "same", context="second").item == "Second"


def test_terminal_assessment_cannot_skip_gate(tmp_path: Path) -> None:
    svc = service(
        tmp_path,
        """            - assess:
                question: Stop now?
                positive:
                  steps:
                    - more: More work.
                negative:
                  stop_workflow: true
""",
    )
    svc.add_item(TASK, WorkItem("a", "Case"))
    complete(svc)
    svc.next(TASK)
    complete(svc)
    with pytest.raises(StateError, match="cannot exit"):
        svc.next(TASK, outcome="negative", caller_role="manager")
    mark(svc, "a")
    svc.next(TASK, outcome="negative", caller_role="manager")
    assert svc.load(TASK)[0].status == "completed"


def test_incompatible_snapshot_is_not_rewritten(tmp_path: Path) -> None:
    from dataclasses import replace

    from ww.execution_models import PlanSnapshot

    svc = service(tmp_path)
    _, snapshot = svc.load(TASK)
    before = snapshot.to_dict()
    old = replace(snapshot, schema_version=1).to_dict()
    with pytest.raises(ValueError, match="previous ww build"):
        PlanSnapshot.from_dict(old)
    assert snapshot.to_dict() == before
