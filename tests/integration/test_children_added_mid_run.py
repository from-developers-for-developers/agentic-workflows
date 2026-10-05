# SPDX-License-Identifier: GPL-3.0-or-later
"""Children added while the roadmap is already running its children."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.workflow_helpers import advance_init, start_after_init
from ww.errors import StateError
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "T1"

STAGES = """workflows:
  - name: parent
    steps:
      - slices: One child per slice.
        children:
          steps:
            - refine: Refine {{ww.child.text}}.
            - implement:
                workflow: child
            - land: Land {{ww.child.id}}.
      - close-plan: Close the plan.
  - name: child
    steps:
      - work: Do child work.
  - name: binding-parent
    steps:
      - slices: One child per slice.
        children:
          steps:
            - refine: Refine {{ww.child.text}}.
            - implement:
                workflow: story
  - name: story
    steps:
      - create-story: Create the story.
        variables:
          - name: task_id
            description: The key returned by the tracker.
      - implement: Implement the story.
"""


def _service(root: Path) -> WorkflowService:
    (root / "ww.yaml").write_text(STAGES, encoding="utf-8")
    return WorkflowService(Storage(root))


def _collect(service: WorkflowService, workflow: str, *children: str | None) -> None:
    start_after_init(service, workflow, TASK, agent="codex")
    service.next(TASK)
    for child in children:
        service.add_child(TASK, child, f"Slice {child}")
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")


def _steps(service: WorkflowService) -> list[tuple[str, int | None, str]]:
    _, snapshot = service.load(TASK)
    return [
        (item.step, item.child_number, record.status)
        for item, record in zip(
            snapshot.plan.items, service.load(TASK)[0].item_executions, strict=True
        )
        if item.child_stage is not None
    ]


def _run_child(service: WorkflowService, child: str) -> None:
    advance_init(service, service.start_child(TASK, child))
    child_task = f"{TASK}/{child}"
    service.next(child_task)
    service.complete(child_task, artifact="Child work.", summary_for_next="Done.")
    service.next(child_task)
    service.complete(
        child_task, (("summary", f"{child} is done."),), summary_for_next="Done."
    )


def test_a_child_added_during_child_one_runs_after_child_two(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _collect(service, "parent", "A", "B")
    service.next(TASK)  # refine of child 1 is active

    child = service.add_child(TASK, "C", "Slice C")

    assert child.id == "C"
    steps = _steps(service)
    assert [(step, number) for step, number, _ in steps] == [
        (f"slices/child-{n}/{stage}", n)
        for n in (1, 2, 3)
        for stage in ("refine", "implement", "land")
    ]
    assert [status for _, number, status in steps if number == 3] == ["pending"] * 3
    _, snapshot = service.load(TASK)
    names = [item.step for item in snapshot.plan.items]
    assert names.index("slices/child-3/refine") > names.index("slices/child-2/land")
    assert names.index("close-plan") > names.index("slices/child-3/land")

    children = service.tasks.read_children(TASK, service.load(TASK)[0].run_id)
    assert [entry.id for entry in children] == ["A", "B", "C"]
    page = service.status(TASK).action_text or ""
    assert "`A` (pending), 1 of 3." in page


def test_the_added_child_runs_through_its_stages(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _collect(service, "parent", "A")
    service.next(TASK)
    service.add_child(TASK, "B", "Slice B")
    service.complete(TASK, artifact="Refined.", summary_for_next="Done.")
    _run_child(service, "A")
    service.next(TASK)
    service.complete(TASK, artifact="Landed.", summary_for_next="Done.")

    refine = service.next(TASK)
    assert refine.item_name == "refine"
    assert "Refine Slice B." in (refine.action_text or "")
    service.complete(TASK, artifact="Refined.", summary_for_next="Done.")
    _run_child(service, "B")
    service.next(TASK)
    service.complete(TASK, artifact="Landed.", summary_for_next="Done.")
    assert service.next(TASK).item_name == "close-plan"


def test_a_snapshot_with_the_added_child_round_trips(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _collect(service, "parent", "A")
    service.next(TASK)
    service.add_child(TASK, "B", "Slice B")

    first = service.load(TASK)
    second = WorkflowService(Storage(tmp_path)).load(TASK)
    assert second[1].plan == first[1].plan
    assert second[1].template_plan == first[1].template_plan
    assert any(
        item.item_template and item.child_stage is not None
        for item in (second[1].template_plan or second[1].plan).items
    )


def test_adding_a_child_past_the_last_child_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _collect(service, "parent", "A")
    service.next(TASK)
    service.complete(TASK, artifact="Refined.", summary_for_next="Done.")
    _run_child(service, "A")
    service.next(TASK)
    service.complete(TASK, artifact="Landed.", summary_for_next="Done.")
    assert service.next(TASK).item_name == "close-plan"

    with pytest.raises(StateError, match="this run is past them"):
        service.add_child(TASK, "B", "Late")


def test_adding_while_collecting_behaves_as_before(tmp_path: Path) -> None:
    service = _service(tmp_path)
    start_after_init(service, "parent", TASK, agent="codex")
    service.next(TASK)
    service.add_child(TASK, "A", "Slice A")
    service.add_child(TASK, "B", "Slice B")
    _, snapshot = service.load(TASK)
    assert not any(
        item.child_stage is not None and not item.item_template
        for item in snapshot.plan.items
    )
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    assert sorted({n for _, n, _ in _steps(service)}) == [1, 2]


def test_a_child_that_binds_its_id_added_mid_run_is_reserved_and_started(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _collect(service, "binding-parent", None)
    service.next(TASK)
    added = service.add_child(TASK, None, "Slice later")

    children = service.tasks.read_children(TASK, service.load(TASK)[0].run_id)
    assert [entry.id for entry in children][-1] == added.id
    assert len(children) == 2
    assert {n for _, n, _ in _steps(service) if n} == {1, 2}
    first = children[0]

    service.complete(TASK, artifact="Refined.", summary_for_next="Done.")
    service.start_child(TASK, first.id)
    service.next(first.id)
    bound = service.complete(
        first.id,
        variables=(("task_id", "PROJ-1"),),
        artifact="Created.",
        summary_for_next="Done.",
    )
    service.next(bound.task_id)
    service.complete(bound.task_id, artifact="Implemented.", summary_for_next="Done.")
    service.next(bound.task_id)
    service.complete(
        bound.task_id, (("summary", "Story is done."),), summary_for_next="Done."
    )
    assert service.next(TASK).item_name == "refine"
    service.complete(TASK, artifact="Refined.", summary_for_next="Done.")
    request = service.start_child(TASK, added.id)
    assert request.task_id == added.id
    assert request.status == "pending"
