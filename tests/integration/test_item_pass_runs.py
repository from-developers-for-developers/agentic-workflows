# SPDX-License-Identifier: GPL-3.0-or-later
"""Real runs of `items` passes: scoped expansion, pass gates, loops."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.errors import StateError
from ww.items import WorkItem
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"


def _expanded(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: review
    steps:
      - collect: Record one item per comment.
        items:
          steps:
            - analyze: Analyze this comment.
              item_phase: analyze
            - fix: Fix this comment.
              item_phase: resolve
      - wrap: Wrap up.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("review", TASK, agent="codex", workflow_runtime="single")
    service.next(TASK)
    service.add_item(TASK, WorkItem("c1", "First comment"))
    service.add_item(TASK, WorkItem("c2", "Second comment"))
    service.complete(TASK, artifact="collected", summary_for_next="Done.")
    return service


def test_expansion_keeps_the_pass_on_every_concrete_stage(tmp_path: Path) -> None:
    _, snapshot = _expanded(tmp_path).load(TASK)

    concrete = [item for item in snapshot.plan.items if item.item_id]
    assert [item.item_id for item in concrete] == ["c1", "c1", "c2", "c2"]
    assert {item.item_pass for item in concrete} == {"collect"}
    assert not any(item.item_template for item in snapshot.plan.items)
    assert [i.item_pass for i in snapshot.plan.items if i.name == "wrap"] == [None]
    # The template the run started from still holds the unexpanded stages.
    assert {
        item.item_pass for item in snapshot.template_plan.items if item.item_template
    } == {"collect"}


# --- sequential passes over one collection ---------------------------------

REVIEW = """workflows:
  - name: review
    steps:
      - collect: Record one item per comment with its stable source ID.
        items:
          steps: []
      - analyze-together: Analyze all collected comments together.
      - confirm-analysis: Reuse the collected items.
        items:
          steps:
            - analyze: Reuse the shared analysis; confirm and fill gaps.
              item_phase: analyze
      - fix-together: Implement and verify the fixes for all analyzed items.
      - finish: Reuse the collected items.
        items:
          steps:
            - verify-resolution: Verify the result and record actual_solution.
              item_phase: resolve
            - report: Report the result for this original comment.
              item_phase: report
"""


def _service(root: Path, workflows: str = REVIEW) -> WorkflowService:
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    service = WorkflowService(Storage(root))
    start_after_init(service, "review", TASK, agent="codex", workflow_runtime="single")
    return service


def _resumed(root: Path) -> WorkflowService:
    """A new process over the same task: every view of it still renders."""
    service = WorkflowService(Storage(root))
    stopped = service.load(TASK)[0].status == "failed"
    assert main(["--root", str(root), "status", TASK]) == 0
    # A stop for the operator exits nonzero; it still renders its page.
    assert main(["--root", str(root), "instruction", TASK]) == int(stopped)
    service.status(TASK)
    service.instruction(TASK, caller_role="manager")
    return service


def _stages(service: WorkflowService) -> list[tuple[str, str | None, str | None]]:
    """Every per-item stage of the plan: name, item, and pass; templates too."""
    _, snapshot = service.load(TASK)
    return [
        (item.name, item.item_id, item.item_pass)
        for item in snapshot.plan.items
        if item.item_template or item.item_id is not None
    ]


def _step(service: WorkflowService, artifact: str = "Done.") -> str | None:
    """Begin the next step and complete it; the name of the step it was."""
    name = service.next(TASK).item_name
    service.complete(TASK, artifact=artifact, summary_for_next="Done.")
    return name


def test_comments_pass_analysis_one_batch_fix_then_resolution_in_order(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    assert service.next(TASK).item_name == "collect"
    service.add_item(TASK, WorkItem("c1", "First comment", fields=(("source", "s1"),)))
    service.add_item(TASK, WorkItem("c2", "Second comment", fields=(("source", "s2"),)))
    service = _resumed(tmp_path)
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")

    # The collection-only pass expands nothing; no later pass expands early.
    assert _stages(service) == [
        ("analyze", None, "confirm-analysis"),
        ("verify-resolution", None, "finish"),
        ("report", None, "finish"),
    ]
    service = _resumed(tmp_path)
    assert service.next(TASK).item_name == "analyze-together"
    for item_id in ("c1", "c2"):
        service.update_item(
            TASK, item_id, processed_item=f"{item_id} analysis", proposed_solution="p"
        )
    service.complete(TASK, artifact="Analyzed.", summary_for_next="Done.")

    collector = service.next(TASK)
    assert collector.item_name == "confirm-analysis"
    assert "later pass over the items this run already collected" in (
        collector.action_text or ""
    )
    service = _resumed(tmp_path)
    service.complete(TASK, artifact="Reused.", summary_for_next="Done.")
    assert _stages(service) == [
        ("analyze", "c1", "confirm-analysis"),
        ("analyze", "c2", "confirm-analysis"),
        ("verify-resolution", None, "finish"),
        ("report", None, "finish"),
    ]

    assert _step(service) == "analyze"
    service = _resumed(tmp_path)  # during the pass
    assert _step(service) == "analyze"
    service = _resumed(tmp_path)  # after it: no resolved/reported needed yet
    assert _step(service, "Fixed.") == "fix-together"
    assert service.next(TASK).item_name == "finish"
    service = _resumed(tmp_path)
    service.complete(TASK, artifact="Reused.", summary_for_next="Done.")
    assert _stages(service) == [
        ("analyze", "c1", "confirm-analysis"),
        ("analyze", "c2", "confirm-analysis"),
        ("verify-resolution", "c1", "finish"),
        ("report", "c1", "finish"),
        ("verify-resolution", "c2", "finish"),
        ("report", "c2", "finish"),
    ]
    for item_id in ("c1", "c2"):
        assert service.next(TASK).item_name == "verify-resolution"
        service.update_item(TASK, item_id, actual_solution="Fixed.", resolved=True)
        service.complete(TASK, artifact="Verified.", summary_for_next="Done.")
        service = _resumed(tmp_path)
        assert service.next(TASK).item_name == "report"
        service.update_item(TASK, item_id, reported=True)
        service.complete(TASK, artifact="Reported.", summary_for_next="Done.")

    assert service.next(TASK).item_name == "update-workflow-summary"
    service.complete(TASK, (("summary", "Reviewed."),))
    state, snapshot = _resumed(tmp_path).load(TASK)
    assert state.status == "completed"
    assert [
        item.name
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if record.started_at is not None and item.phase == "step"
    ] == [
        "init",
        "collect",
        "analyze-together",
        "confirm-analysis",
        "analyze",
        "analyze",
        "fix-together",
        "finish",
        "verify-resolution",
        "report",
        "verify-resolution",
        "report",
    ]
    assert service.items(TASK) == tuple(
        WorkItem(
            item_id,
            text,
            processed_item=f"{item_id} analysis",
            proposed_solution="p",
            actual_solution="Fixed.",
            resolved=True,
            reported=True,
            fields=(("source", source),),
        )
        for item_id, text, source in (
            ("c1", "First comment", "s1"),
            ("c2", "Second comment", "s2"),
        )
    )


def _through_analysis(service: WorkflowService, analyzed: tuple[str, ...]) -> None:
    """Collect c1 and c2, analyze ``analyzed`` together, and run the analyze pass."""
    service.next(TASK)
    service.add_item(TASK, WorkItem("c1", "First comment"))
    service.add_item(TASK, WorkItem("c2", "Second comment"))
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    service.next(TASK)
    for item_id in analyzed:
        service.update_item(TASK, item_id, processed_item="Analysis.")
    service.complete(TASK, artifact="Analyzed.", summary_for_next="Done.")
    for _ in range(3):  # confirm-analysis, then analyze for each item
        _step(service)


def test_an_analyze_pass_requires_only_the_analysis(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _through_analysis(service, ("c1",))

    state, _ = service.load(TASK)
    assert state.status == "failed"
    assert state.last_error is not None
    assert "c2 (analyze): processed_item" in state.last_error
    assert "c1" not in state.last_error
    assert "resolved" not in state.last_error
    assert "reported" not in state.last_error

    service = _resumed(tmp_path)
    service.update_item(TASK, "c2", processed_item="Analysis.")
    assert service.next(TASK, retry=True).item_name == "fix-together"


def test_resolve_and_report_stages_gate_their_own_records(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _through_analysis(service, ("c1", "c2"))
    _step(service)  # fix-together
    _step(service)  # finish
    for item_id in ("c1", "c2"):
        service.next(TASK)
        if item_id == "c1":
            service.update_item(TASK, item_id, actual_solution="Fixed.", resolved=True)
        service.complete(TASK, artifact="Verified.", summary_for_next="Done.")
        service.next(TASK)
        if item_id == "c2":
            service.update_item(TASK, item_id, reported=True)
        service.complete(TASK, artifact="Reported.", summary_for_next="Done.")

    state, _ = service.load(TASK)
    assert state.status == "failed"
    assert state.last_error is not None
    assert "c1 (report): reported=true" in state.last_error
    assert "c2 (verify-resolution): actual_solution, resolved=true" in (
        state.last_error
    )
    # The gate marks nothing on its own.
    assert [(item.resolved, item.reported) for item in service.items(TASK)] == [
        (True, False),
        (False, True),
    ]


def test_a_linked_comment_shares_its_canonical_fix_but_is_reported_itself(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.next(TASK)
    service.add_item(TASK, WorkItem("c1", "First comment"))
    service.add_item(TASK, WorkItem("c2", "Same as c1", reference_to_id="c1"))
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    service.next(TASK)
    service.update_item(TASK, "c1", processed_item="Shared analysis.")
    service.complete(TASK, artifact="Analyzed.", summary_for_next="Done.")
    for _ in range(5):  # confirm-analysis, analyze x2, fix-together, finish
        _step(service)
    for item_id in ("c1", "c2"):
        service.next(TASK)
        if item_id == "c1":
            service.update_item(TASK, item_id, actual_solution="Fixed.", resolved=True)
        service.complete(TASK, artifact="Verified.", summary_for_next="Done.")
        service.next(TASK)
        if item_id == "c1":
            service.update_item(TASK, item_id, reported=True)
        service.complete(TASK, artifact="Reported.", summary_for_next="Done.")

    state, _ = service.load(TASK)
    assert state.last_error is not None
    assert state.last_error.endswith(
        "c2 (report): reported=true. Record it with update-item, then retry"
    )
    service.update_item(TASK, "c2", reported=True)
    assert service.next(TASK, retry=True).item_name == "update-workflow-summary"
    linked = service.item(TASK, "c2")
    assert (linked.processed_item, linked.actual_solution) == ("", "")


def test_empty_collections_finish_every_pass_without_stages(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for expected in ("collect", "analyze-together", "confirm-analysis"):
        assert _step(service) == expected
    assert _stages(service) == [
        ("verify-resolution", None, "finish"),
        ("report", None, "finish"),
    ]
    service = _resumed(tmp_path)
    assert _step(service) == "fix-together"
    assert _step(service) == "finish"
    assert _stages(service) == []
    assert service.next(TASK).item_name == "update-workflow-summary"


def test_items_added_during_a_pass_join_the_next_pass(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.next(TASK)
    service.add_item(TASK, WorkItem("c1", "First comment"))
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    _step(service)  # analyze-together
    service.next(TASK)
    service.add_item(TASK, WorkItem("c2", "Second comment"))
    service.complete(TASK, artifact="Reconciled.", summary_for_next="Done.")
    assert service.next(TASK).item_name == "analyze"
    service.update_item(TASK, "c1", processed_item="Analysis.")
    # Added mid-pass: kept, but this pass's membership is frozen.
    service.add_item(TASK, WorkItem("c3", "Late comment"))
    service.complete(TASK, artifact="Confirmed.", summary_for_next="Done.")
    assert [stage for stage in _stages(service) if stage[2] == "confirm-analysis"] == [
        ("analyze", "c1", "confirm-analysis"),
        ("analyze", "c2", "confirm-analysis"),
    ]
    service.next(TASK)
    service.update_item(TASK, "c2", processed_item="Analysis.")
    service.complete(TASK, artifact="Confirmed.", summary_for_next="Done.")
    # c3 has no analysis, but it was not part of the pass.
    assert _step(service) == "fix-together"
    _step(service)  # finish
    assert [item for _, item, pass_id in _stages(service) if pass_id == "finish"] == [
        "c1",
        "c1",
        "c2",
        "c2",
        "c3",
        "c3",
    ]


LOOPED = """workflows:
  - name: review
    steps:
      - rounds: Review in rounds.
        max_rounds: 3
        loop:
          - collect: Record new comments.
            items:
              steps: []
          - triage: Reuse the collected items.
            items:
              steps:
                - assess:
                    question: Does this comment need a fix?
                    outcomes:
                      positive:
                        steps:
                          - analyze: Analyze it.
                            item_phase: analyze
                - attempts: Try until it holds.
                  loop:
                    - attempt: Try a fix.
                      break: The fix holds.
          - decide: Decide whether another round is needed.
            break: No new comments.
"""


def test_passes_in_a_loop_expand_again_each_round(tmp_path: Path) -> None:
    service = _service(tmp_path, LOOPED)
    assert service.next(TASK).item_name == "collect"
    service.add_item(TASK, WorkItem("c1", "First comment"))
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    _step(service)  # triage
    assert service.next(TASK).item_name == "assess"
    service = _resumed(tmp_path)
    service.complete(TASK, artifact="Needs a fix.", summary_for_next="Done.")
    assert service.status(TASK).choosing_outcome_of == "assess"
    service = _resumed(tmp_path)
    assert service.next(TASK, outcome="positive").item_name == "analyze"
    service.update_item(TASK, "c1", processed_item="Analysis.")
    service.complete(TASK, artifact="Analyzed.", summary_for_next="Done.")
    assert service.next(TASK).item_name == "attempt"
    # The nearest loop is the per-item one: its break leaves only it.
    service.loop(TASK, artifact="Holds.", summary_for_next="Done.")
    assert service.next(TASK).item_name == "decide"
    service.complete(TASK, artifact="More comments.", summary_for_next="Done.")

    assert service.next(TASK).item_name == "collect"  # round 2
    service = _resumed(tmp_path)
    service.add_item(TASK, WorkItem("c2", "New comment"))
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    _step(service)  # triage
    state, snapshot = service.load(TASK)
    round_two = [
        (item.name, item.item_id)
        for item in snapshot.plan.items
        if item.item_pass == "rounds/triage" and item.phase == "step" and item.item_id
    ]
    stage_names = ["assess", "analyze", "attempts", "attempt", "attempts"]
    assert round_two == [
        *((name, "c1") for name in stage_names),
        *((name, "c2") for name in stage_names),
    ]
    # Fresh records for the round: only the stage now open has begun.
    statuses = [
        record.status if record.started_at is None else "begun"
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.item_pass == "rounds/triage" and item.item_id
    ]
    assert statuses[0] == "begun"
    assert set(statuses[1:]) == {"pending"}
    assert dict(state.loop_iterations)["rounds"] == 2
    assert service.next(TASK).item_name == "assess"
    service.complete(TASK, artifact="Fine.", summary_for_next="Done.")
    assert service.next(TASK, outcome="negative").item_name == "attempt"
    assert service.load(TASK)[0].item_executions  # stays loadable


def test_a_later_pass_still_replans_while_an_earlier_one_is_expanded(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _through_analysis(service, ())
    service = WorkflowService(Storage(tmp_path))
    (tmp_path / "ww.yaml").write_text(
        REVIEW.replace("Reuse the shared analysis;", "Reuse the batch analysis;"),
        encoding="utf-8",
    )
    stop = service.next(TASK, caller_role="manager")
    assert stop.plan_change is not None
    assert stop.plan_change.refusal is not None
    assert "already expanded" in stop.plan_change.refusal

    (tmp_path / "ww.yaml").write_text(
        REVIEW.replace("Report the result", "Reply with the result"), encoding="utf-8"
    )
    stop = service.next(TASK, caller_role="manager")
    assert stop.plan_change is not None
    assert stop.plan_change.refusal is None
    service.next(TASK, caller_role="manager", replan=True)
    _, snapshot = service.load(TASK)
    assert [
        (item.item_id, item.item_template)
        for item in snapshot.plan.items
        if item.item_pass == "confirm-analysis" and item.phase == "step"
    ] == [(None, False), ("c1", False), ("c2", False)]
    assert [
        item.description
        for item in snapshot.plan.items
        if item.name == "report" and item.item_template
    ] == ["Reply with the result for this original comment."]


def test_the_first_declaration_holds_the_collection_settings(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        REVIEW.replace(
            "        items:\n          steps: []\n",
            "        items:\n          identity: source\n          steps: []\n",
            1,
        ),
    )
    service.next(TASK)
    service.add_item(TASK, WorkItem("c1", "First", fields=(("source", "s1"),)))
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    _step(service)  # analyze-together
    assert service.next(TASK).item_name == "confirm-analysis"

    # The later pass omits identity; the collection still requires it.
    with pytest.raises(StateError, match="needs the field 'source'"):
        service.add_item(TASK, WorkItem("c2", "Second"))
    with pytest.raises(StateError, match="already"):
        service.add_item(TASK, WorkItem("c2", "Second", fields=(("source", "s1"),)))
