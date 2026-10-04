# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-child parent stages: the parent's own loop over its children."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.workflow_helpers import advance_init, start_after_init
from ww.cli import main
from ww.errors import StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "T1"

STAGES = """workflows:
  - name: parent
    steps:
      - slices: One child per slice.
        children:
          steps:
            - refine: Refine {{ww.child.text}} ({{ww.child.field.area}}).
            - implement:
                workflow: child
            - review: Review {{ww.child.id}}.
              artifact_from: implement
              break: Nothing more is worth doing.
            - land: Land {{ww.child.id}}.
  - name: child
    steps:
      - work: Do child work.
"""


def _service(root: Path, workflows: str = STAGES) -> WorkflowService:
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    return WorkflowService(Storage(root))


def _collect(service: WorkflowService, *children: str) -> None:
    start_after_init(service, "parent", TASK, agent="codex")
    service.next(TASK)
    for child in children:
        service.add_child(
            TASK, child, f"Slice {child}", fields=(("area", f"area-{child}"),)
        )
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")


def _run_child(service: WorkflowService, child: str) -> None:
    advance_init(service, service.start_child(TASK, child))
    child_task = f"{TASK}/{child}"
    service.next(child_task)
    service.complete(child_task, artifact="Child work.", summary_for_next="Done.")
    service.next(child_task)
    service.complete(
        child_task, (("summary", f"{child} is done."),), summary_for_next="Done."
    )


def _step(service: WorkflowService, artifact: str = "Done.") -> str | None:
    page = service.next(TASK)
    service.complete(TASK, artifact=artifact, summary_for_next="Done.")
    return page.item_name


def test_each_child_runs_its_parent_stages_in_order(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _collect(service, "A", "B")

    state, snapshot = service.load(TASK)
    assert [
        (item.step, item.child_number)
        for item in snapshot.plan.items
        if item.child_stage is not None
    ] == [
        ("slices/child-1/refine", 1),
        ("slices/child-1/implement", 1),
        ("slices/child-1/review", 1),
        ("slices/child-1/land", 1),
        ("slices/child-2/refine", 2),
        ("slices/child-2/implement", 2),
        ("slices/child-2/review", 2),
        ("slices/child-2/land", 2),
    ]

    refine = service.next(TASK)
    assert refine.item_name == "refine"
    assert "Refine Slice A (area-A)." in (refine.action_text or "")
    assert "Current child: `A` (pending), 1 of 2." in (refine.action_text or "")
    waiting = service.complete(TASK, artifact="Refined.", summary_for_next="Done.")
    assert waiting.action_kind == "child_workflow"
    assert "./ww start-child T1 A" in (waiting.action_text or "")

    _run_child(service, "A")
    review = service.next(TASK)
    assert review.item_name == "review"
    assert "Review A." in (review.action_text or "")
    assert "`slices/child-1/implement` step" in (review.action_text or "")
    service.complete(TASK, artifact="Reviewed.", summary_for_next="Done.")
    assert _step(service) == "land"

    assert _step(service) == "refine"
    _run_child(service, "B")
    assert [_step(service), _step(service)] == ["review", "land"]
    assert service.next(TASK).item_name == "update-workflow-summary"


def test_the_child_stage_saves_the_childs_summary_as_its_artifact(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _collect(service, "A")
    _step(service)
    _run_child(service, "A")

    implement = next(
        entry
        for entry in service.artifacts(TASK)
        if entry["step"] == "slices/child-1/implement"
    )
    text = Path(str(implement["path"])).read_text(encoding="utf-8")
    assert "Child task `T1/A` completed its `child` workflow." in text
    assert "Summary: A is done." in text


def test_a_pre_stage_refines_the_child_before_it_starts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _collect(service, "A")
    service.next(TASK)

    service.update_child(TASK, "A", text="Slice A, narrowed to the parser")
    service.complete(TASK, artifact="Refined.", summary_for_next="Done.")
    advance_init(service, service.start_child(TASK, "A"))

    init = next(
        entry for entry in service.artifacts(f"{TASK}/A") if entry["step"] == "init"
    )
    assert "Requirements for child task A: Slice A, narrowed to the parser" in (
        Path(str(init["path"])).read_text(encoding="utf-8")
    )
    with pytest.raises(StateError, match="only a pending child can change"):
        service.update_child(TASK, "A", text="Too late.")
    # Fields only feed the parent's stages, so they may change at any time.
    assert service.update_child(TASK, "A", fields=(("area", "done"),)).fields == (
        ("area", "done"),
    )


def test_only_the_current_child_can_start(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _collect(service, "A", "B")
    _step(service)

    with pytest.raises(StateError, match="the parent runs child 'A' now"):
        service.start_child(TASK, "B")
    assert service.start_child(TASK, "A").task_id == "T1/A"


def test_a_failed_child_stops_the_parent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _collect(service, "A", "B")
    _step(service)
    advance_init(service, service.start_child(TASK, "A"))
    service.next(f"{TASK}/A")

    service.fail(f"{TASK}/A", "deliberately stopped")

    parent = service.status(TASK)
    assert parent.status == "failed"
    assert parent.item_name == "implement"
    assert [child.status for child in parent.child_tasks] == ["failed", "pending"]


def test_a_break_on_a_parent_stage_skips_the_remaining_children(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _collect(service, "A", "B", "C")
    _step(service)
    _run_child(service, "A")
    review = service.next(TASK)
    assert review.breaks_children
    rendered = MarkdownOutputAdapter().render_instruction(review)
    assert "### Children outcome" in rendered
    assert "stop running children" in rendered

    after = service.loop(TASK, artifact="Enough.", summary_for_next="Done.")

    assert after.item_name == "update-workflow-summary"
    state, snapshot = service.load(TASK)
    assert [child.status for child in service.tasks.read_children(TASK)] == [
        "completed",
        "skipped",
        "skipped",
    ]
    skipped = {
        item.step
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if record.result == "skipped because a children break gate passed"
    }
    assert "slices/child-1/land" in skipped
    assert "slices/child-3/implement" in skipped
    with pytest.raises(StateError, match="parent workflow is not running its children"):
        service.start_child(TASK, "B")


def test_a_loop_inside_the_stages_runs_once_per_child(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: parent
    steps:
      - slices: Split.
        children:
          steps:
            - implement:
                workflow: child
            - name: review
              loop:
                - check: Check {{ww.child.id}}.
                  break: It is good.
                - fix: Fix it.
            - land: Land {{ww.child.id}}.
  - name: child
    steps:
      - work: Do child work.
""",
    )
    _collect(service, "A", "B")
    _run_child(service, "A")
    assert [_step(service), _step(service)] == ["check", "fix"]
    second = service.next(TASK)
    assert (second.item_name, second.loop_iteration) == ("check", 2)
    service.loop(TASK, artifact="Good.", summary_for_next="Done.")
    assert _step(service) == "land"

    _run_child(service, "B")
    first = service.next(TASK)
    assert (first.item_name, first.loop_iteration) == ("check", 1)
    service.loop(TASK, artifact="Good.", summary_for_next="Done.")
    assert "Land B." in (service.next(TASK).action_text or "")


def test_the_auto_manager_also_manages_each_child(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: parent
    steps:
      - slices: Split.
        role: manager
        children:
          steps:
            - refine: Refine {{ww.child.text}}.
            - implement:
                workflow: child
            - review: Review {{ww.child.id}}.
              role: worker
  - name: child
    steps:
      - work: Do child work.
""",
    )
    service.start(
        "parent",
        TASK,
        agent="claudecode",
        workflow_runtime="auto",
        init_artifact="Do it.",
        caller_role="manager",
    )
    assert service.next(TASK, caller_role="manager").item_name == "slices"
    service.add_child(TASK, "A", "Slice A")
    service.complete(
        TASK, artifact="Collected.", summary_for_next="Done.", caller_role="manager"
    )
    refine = service.next(TASK, caller_role="manager")
    assert (refine.item_name, refine.role) == ("refine", "manager")
    service.complete(
        TASK, artifact="Refined.", summary_for_next="Done.", caller_role="manager"
    )

    started = service.start_child(TASK, "A")
    assert started.task_id == "T1/A"
    # The child's steps are ordinary worker assignments the same manager
    # dispatches from the child's own pages.
    work = service.next(f"{TASK}/A", caller_role="manager")
    assert work.item_name == "work"
    token = work.assignment_token
    assert token is not None
    service.complete(
        f"{TASK}/A",
        artifact="Child work.",
        summary_for_next="Done.",
        caller_role="worker",
        assignment=token,
    )
    done = service.complete(
        f"{TASK}/A",
        (("summary", "A is done."),),
        summary_for_next="Done.",
        caller_role="worker",
        assignment=token,
    )
    assert done.status == "completed"
    finished = service.instruction(f"{TASK}/A", caller_role="manager")
    assert finished.parent_task_id == TASK
    assert "./ww next T1 --role manager" in (
        MarkdownOutputAdapter().render_instruction(finished)
    )

    review = service.next(TASK, caller_role="manager")
    assert (review.item_name, review.role) == ("review", "worker")
    assert review.assignment_token is not None


def test_a_stage_reads_the_childs_branch_once_it_is_recorded(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: parent
    steps:
      - slices: Split.
        children:
          steps:
            - implement:
                workflow: child
            - land: Merge {{ww.child.git.branch}} from {{ww.child.git.base_branch}}.
  - name: child
    steps:
      - work: Do child work.
""",
    )
    (tmp_path / "ww.json").write_text(
        json.dumps({"enabled": True, "extensions": {"ww/git": {}}}),
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _collect(service, "A")
    _run_child(service, "A")
    stopped = service.next(TASK)
    assert stopped.operator_reason == "value_unavailable"
    assert "{{ww.child.git.branch}}" in (stopped.error or "")

    service.extensions.store("ww/git").append_line(
        "branches.jsonl",
        json.dumps({"task_id": "T1/A", "branch": "feature/a", "base": "main"}),
    )
    assert service.next(TASK, retry=True).operator_reason is None
    land = service.next(TASK)
    assert "Merge feature/a from main." in (land.action_text or "")


def test_child_fields_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    start_after_init(service, "parent", TASK, agent="codex")
    service.next(TASK)
    root = ["--root", str(tmp_path)]

    added = [*root, "add-child", TASK, "--id", "A", "--text", "Slice A"]
    assert main([*added, "--field", "area=parser"]) == 0
    assert json.loads(capsys.readouterr().out)["fields"] == {"area": "parser"}
    assert main([*root, "update-child", TASK, "A", "--field", "area=lexer"]) == 0
    assert json.loads(capsys.readouterr().out)["fields"] == {"area": "lexer"}
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")

    assert "Refine Slice A (lexer)." in (service.next(TASK).action_text or "")


def test_a_missing_child_field_says_how_to_set_it(tmp_path: Path) -> None:
    service = _service(tmp_path)
    start_after_init(service, "parent", TASK, agent="codex")
    service.next(TASK)
    service.add_child(TASK, "A", "Slice A")
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")

    stopped = service.next(TASK)

    assert stopped.operator_reason == "value_unavailable"
    assert (
        "{{ww.child.field.area}} (the child has no field 'area'; set it with "
        "`ww update-child T1 A --field area=<value>`, or `--field` on add-child)"
    ) in (stopped.error or "")
    service.update_child(TASK, "A", fields=(("area", "parser"),))
    assert service.next(TASK, retry=True).operator_reason is None


def _landing_workflow() -> str:
    program = (
        "import os, sys; "
        "open('landed', 'a').write(sys.argv[1] + chr(10)); "
        "print('CONFLICT in file.txt'); "
        "sys.exit(1 if os.path.exists('conflict') else 0)"
    )
    argv = json.dumps([sys.executable, "-c", program, "{{ww.child.git.branch}}"])
    return f"""workflows:
  - name: parent
    steps:
      - slices: Split.
        children:
          steps:
            - implement:
                workflow: child
            - land: ~
              argv: {argv}
              on_failure: fix
              on_failure_instruction: Resolve the merge conflict.
  - name: child
    steps:
      - work: Do child work.
"""


def test_an_automatic_landing_step_hands_a_conflict_to_the_agent(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _landing_workflow())
    (tmp_path / "ww.json").write_text(
        json.dumps({"enabled": True, "extensions": {"ww/git": {}}}),
        encoding="utf-8",
    )
    (tmp_path / "conflict").touch()
    service = WorkflowService(Storage(tmp_path))
    _collect(service, "A")
    service.extensions.store("ww/git").append_line(
        "branches.jsonl",
        json.dumps({"task_id": "T1/A", "branch": "feature/a", "base": "main"}),
    )
    _run_child(service, "A")
    repair = service.next(TASK)
    # The branch was interpolated and the command ran in the task workspace.
    assert (tmp_path / "landed").read_text() == "feature/a\n"
    assert repair.handler_repair is not None
    assert repair.operator_reason is None
    assert "CONFLICT in file.txt" in repair.action_text
    assert "Resolve the merge conflict." in repair.action_text
    (tmp_path / "conflict").unlink()
    done = service.complete(TASK, artifact="Resolved the conflict.")
    assert done.item_name != "land"
    assert (tmp_path / "landed").read_text() == "feature/a\nfeature/a\n"
