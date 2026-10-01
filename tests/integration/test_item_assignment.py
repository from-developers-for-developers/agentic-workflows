# SPDX-License-Identifier: GPL-3.0-or-later
"""Worker assignments that span the per-item stages of an ``items`` step."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.assignments import assignment_at
from ww.instructions import Instruction
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"


def _service(tmp_path: Path, assignment: str, stage_hook: str = "") -> WorkflowService:
    (tmp_path / "ww.yaml").write_text(
        f"""workflows:
  - name: review
    steps:
      - review: Review the pull request.
        items:
          description: Split based on the review comments.
          assignment: {assignment}
          steps:
            - analyze: Analyze this comment.
              item_phase: analyze
            - fix: Fix this comment.
              item_phase: resolve
{stage_hook}""",
        encoding="utf-8",
    )
    return WorkflowService(Storage(tmp_path))


def _collect(service: WorkflowService, runtime: str = "auto") -> Instruction:
    service.start("review", TASK, agent="codex", workflow_runtime=runtime)
    service.next(TASK, caller_role="manager")
    service.add_item(TASK, WorkItem("c1", "First comment"))
    service.add_item(TASK, WorkItem("c2", "Second comment"))
    return service.complete(
        TASK,
        artifact="collected",
        caller_role="worker",
        assignment=assignment_token(service, TASK),
        summary_for_next="Done.",
    )


def _finish_stage(service: WorkflowService, item_id: str, stage: str) -> Instruction:
    if stage == "analyze":
        service.update_item(TASK, item_id, processed_item="analysis")
    else:
        service.update_item(
            TASK, item_id, actual_solution="fixed", resolved=True, reported=True
        )
    return service.complete(
        TASK,
        artifact=f"{stage} done",
        caller_role="worker",
        assignment=assignment_token(service, TASK),
        summary_for_next="Done.",
    )


def _boundaries(service: WorkflowService) -> list[tuple[str, str | None, str | None]]:
    """Run every item stage and record who acts after each completion."""
    route: list[tuple[str, str | None, str | None]] = []
    for item_id in ("c1", "c2"):
        for stage in ("analyze", "fix"):
            current = service.status(
                TASK, caller_role="worker", assignment=assignment_token(service, TASK)
            )
            if current.item_status != "in_progress":
                current = service.next(TASK, caller_role="manager")
            assert (current.item_name, current.item_status) == (stage, "in_progress")
            after = _finish_stage(service, item_id, stage)
            route.append((f"{item_id}/{stage}", after.next_role, after.item_name))
    return route


def test_per_step_hands_every_stage_back_to_the_manager(tmp_path: Path) -> None:
    service = _service(tmp_path, "per_step")
    _collect(service)

    assert _boundaries(service) == [
        ("c1/analyze", "manager", "fix"),
        ("c1/fix", "manager", "analyze"),
        ("c2/analyze", "manager", "fix"),
        ("c2/fix", "worker", "update-workflow-summary"),
    ]


def test_per_item_keeps_one_worker_for_all_stages_of_an_item(tmp_path: Path) -> None:
    service = _service(tmp_path, "per_item")
    _collect(service)

    assert _boundaries(service) == [
        ("c1/analyze", "worker", "fix"),
        ("c1/fix", "manager", "analyze"),
        ("c2/analyze", "worker", "fix"),
        ("c2/fix", "worker", "update-workflow-summary"),
    ]


def test_together_keeps_one_worker_for_every_item(tmp_path: Path) -> None:
    service = _service(tmp_path, "together")
    _collect(service)

    assert _boundaries(service) == [
        ("c1/analyze", "worker", "fix"),
        ("c1/fix", "worker", "analyze"),
        ("c2/analyze", "worker", "fix"),
        ("c2/fix", "worker", "update-workflow-summary"),
    ]


def test_single_runtime_keeps_per_step_boundaries(tmp_path: Path) -> None:
    service = _service(tmp_path, "together")
    _collect(service, runtime="single")
    service.next(TASK, caller_role="manager")

    after = _finish_stage(service, "c1", "analyze")

    # The single runtime opens the next stage on completion; it is still its
    # own step, not a continued assignment.
    assert (after.next_role, after.item_name, after.item_status) == (
        "worker",
        "fix",
        "in_progress",
    )
    assert not after.continues_assignment


def test_span_survives_a_reload_between_stages(tmp_path: Path) -> None:
    service = _service(tmp_path, "per_item")
    _collect(service)
    service.next(TASK, caller_role="manager")
    _finish_stage(service, "c1", "analyze")

    reloaded = WorkflowService(Storage(tmp_path))
    current = reloaded.status(
        TASK, caller_role="worker", assignment=assignment_token(reloaded, TASK)
    )
    assert (current.item_name, current.item_status, current.control) == (
        "fix",
        "in_progress",
        "continue_worker",
    )
    after = _finish_stage(reloaded, "c1", "fix")
    assert (after.next_role, after.item_name) == ("manager", "analyze")


def test_interrupted_stage_hook_recovers_inside_the_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = """              hooks:
                before_start:
                  - argv: [touch, prepared.txt]
"""
    # The hook belongs to ``fix``, the last stage in the configuration.
    service = _service(tmp_path, "per_item", stage_hook=hook)
    _collect(service)
    service.next(TASK, caller_role="manager")
    service.update_item(TASK, "c1", processed_item="analysis")

    def interrupted(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", interrupted)
        with pytest.raises(KeyboardInterrupt):
            service.complete(
                TASK,
                artifact="analyze done",
                caller_role="worker",
                assignment=assignment_token(service, TASK),
                summary_for_next="Done.",
            )

    resumed = WorkflowService(Storage(tmp_path))
    uncertain = resumed.status(
        TASK, caller_role="worker", assignment=assignment_token(resumed, TASK)
    )
    assert (uncertain.control, uncertain.next_role) == ("blocked", "manager")
    assert resumed.next(TASK, caller_role="manager").status == "interrupted"

    recovered = resumed.recover(TASK, retry=True, caller_role="manager")

    assert (recovered.item_name, recovered.item_status, recovered.control) == (
        "fix",
        "in_progress",
        "continue_worker",
    )
    assert (tmp_path / "prepared.txt").exists()
    after = _finish_stage(resumed, "c1", "fix")
    assert (after.next_role, after.item_name) == ("manager", "analyze")


def test_span_instructions_describe_scope_then_stay_compact(tmp_path: Path) -> None:
    service = _service(tmp_path, "per_item")
    md = MarkdownOutputAdapter()
    _collect(service)

    preview = md.render_instruction(service.status(TASK, caller_role="manager"))
    assert (
        "Scope: one worker performs every stage (`analyze`, `fix`) of item `c1`."
        in preview
    )

    service.next(TASK, caller_role="manager")
    first = md.render_instruction(
        service.status(
            TASK, caller_role="worker", assignment=assignment_token(service, TASK)
        )
    )
    assert "### Assignment scope" in first
    assert "Do not start a later stage early." in first

    service.update_item(TASK, "c1", processed_item="analysis")
    compact = service.complete(
        TASK,
        artifact="analyze done",
        caller_role="worker",
        assignment=assignment_token(service, TASK),
        summary_for_next="Done.",
    )
    assert compact.continues_assignment
    rendered = md.render_instruction(compact)
    assert "## Worker: next stage, `fix`" in rendered
    assert "Fix this comment." in rendered
    assert "./ww complete TASK-1 --role worker" in rendered
    for repeated in (
        "You are the worker for this assignment",
        "Requested worker:",
        "### Assignment scope",
        "### Next steps",
        "### Previous artifacts",
        "return control to the manager",
    ):
        assert repeated not in rendered

    # A fresh look at the same stage, for example after losing context,
    # gets the full instruction again.
    full = md.render_instruction(
        service.status(
            TASK, caller_role="worker", assignment=assignment_token(service, TASK)
        )
    )
    assert "You are the worker for this assignment" in full


def test_together_scope_names_every_item(tmp_path: Path) -> None:
    service = _service(tmp_path, "together")
    _collect(service)
    service.next(TASK, caller_role="manager")

    first = service.status(
        TASK, caller_role="worker", assignment=assignment_token(service, TASK)
    )

    assert first.assignment_scope == {
        "item_assignment": "together",
        "item_ids": ["c1", "c2"],
        "stages": ["analyze", "fix"],
    }
    assert "of all 2 items." in MarkdownOutputAdapter().render_instruction(first)


def test_collection_stage_carries_the_splitting_guidance(tmp_path: Path) -> None:
    service = _service(tmp_path, "per_step")
    service.start("review", TASK, agent="codex")
    collection = service.next(TASK)

    assert collection.item_name == "review"
    assert (collection.action_text or "").startswith("Review the pull request.")
    assert "How to split: Split based on the review comments." in (
        collection.action_text or ""
    )


def test_bare_items_run_one_built_in_stage_per_item(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: review
    steps:
      - review: Review the pull request.
        items: ~
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("review", TASK, agent="codex")
    service.next(TASK)
    service.add_item(TASK, WorkItem("c1", "First comment"))
    service.complete(TASK, artifact="collected", summary_for_next="Done.")

    stage = service.next(TASK)

    assert stage.item_name == "handle-item"
    assert (stage.action_text or "").startswith(
        "Handle this item end to end: analyze it, resolve it, and report the outcome."
    )
    assert (
        "./ww update-item TASK-1 --id c1 "
        '--processed-item="<agent-friendly analysis>" '
        '--actual-solution="<actual solution>" --resolved=true --reported=true'
    ) in (stage.action_text or "")
    service.update_item(
        TASK,
        "c1",
        processed_item="analysis",
        actual_solution="fixed",
        resolved=True,
        reported=True,
    )
    summary = service.complete(TASK, artifact="handled", summary_for_next="Done.")
    assert summary.item_name == "update-workflow-summary"


def test_collect_only_items_expand_nothing(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: review
    steps:
      - review: Record the findings.
        items:
          steps: []
      - report: Report the recorded findings.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("review", TASK, agent="codex")
    service.next(TASK)
    service.add_item(TASK, WorkItem("c1", "First comment"))

    after = service.complete(TASK, artifact="collected", summary_for_next="Done.")

    assert after.item_name == "report"


def _spans(service: WorkflowService) -> list[list[str]]:
    """Every worker assignment over the materialized per-item stages."""
    _state, snapshot = service.load(TASK)
    plan = snapshot.plan
    stages = [index for index, item in enumerate(plan.items) if item.item_id]
    spans: list[list[str]] = []
    index = stages[0]
    while index <= stages[-1]:
        assignment = assignment_at(plan, index, runtime="auto")
        assert assignment is not None
        spans.append(
            [
                f"{item.item_id}/{item.name}"
                for item in plan.items[assignment.start : assignment.stop]
                if item.item_id
            ]
        )
        index = assignment.stop
    return spans


@pytest.mark.parametrize(
    ("assignment", "expected"),
    [
        (
            "together",
            [
                ["c1/analyze"],
                ["c1/fix"],
                ["c1/reply", "c2/analyze"],
                ["c2/fix"],
                ["c2/reply"],
            ],
        ),
        (
            "per_item",
            [
                ["c1/analyze"],
                ["c1/fix"],
                ["c1/reply"],
                ["c2/analyze"],
                ["c2/fix"],
                ["c2/reply"],
            ],
        ),
    ],
)
def test_a_stage_with_other_worker_settings_starts_a_new_assignment(
    tmp_path: Path, assignment: str, expected: list[list[str]]
) -> None:
    (tmp_path / "ww.yaml").write_text(
        f"""workflows:
  - name: review
    steps:
      - review: Review the pull request.
        items:
          assignment: {assignment}
          steps:
            - analyze: Analyze this comment.
              item_phase: analyze
            - fix: Fix this comment.
              item_phase: resolve
              model: opus
            - reply: Reply.
              item_phase: report
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _collect(service)

    assert _spans(service) == expected


def test_bare_items_default_to_one_worker_together(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: review
    steps:
      - review: Review the pull request.
        items: Split by comment.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _collect(service)

    assert _spans(service) == [["c1/handle-item", "c2/handle-item"]]
