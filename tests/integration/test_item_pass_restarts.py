# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-pass runs across restarts: trailing outcomes, gate stops, and resumes.

Every check goes through the service and the CLI as a new process would: the
task is reloaded, ``status`` and ``instruction`` render, and the saved state
and plan round-trip, at each boundary of each pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.errors import StateError
from ww.execution_models import ExecutionState, PlanSnapshot
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"


def _service(root: Path, workflows: str) -> WorkflowService:
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    service = WorkflowService(Storage(root))
    start_after_init(service, "review", TASK, agent="codex", workflow_runtime="single")
    return service


def _reloaded(root: Path) -> WorkflowService:
    """A new process over the task: it loads, round-trips, and renders."""
    service = WorkflowService(Storage(root))
    state, snapshot = service.load(TASK)
    assert ExecutionState.from_dict(state.to_dict()) == state
    assert PlanSnapshot.from_dict(snapshot.to_dict()) == snapshot
    stopped = state.status == "failed"
    assert main(["--root", str(root), "status", TASK]) == 0
    assert main(["--root", str(root), "instruction", TASK]) == int(stopped)
    service.status(TASK)
    service.instruction(TASK, caller_role="manager")
    return service


def _cli(root: Path, *args: str) -> int:
    return main(["--root", str(root), *args])


def _step(service: WorkflowService, expected: str) -> None:
    assert service.next(TASK).item_name == expected
    service.complete(TASK, artifact="Done.", summary_for_next="Done.")


def _stopped_at_gate(service: WorkflowService, lacking: str) -> None:
    state, _ = service.load(TASK)
    assert (state.status, state.failure_kind) == ("failed", "pass_incomplete")
    assert state.last_error is not None
    assert lacking in state.last_error
    assert service.status(TASK).operator_reason == "pass_incomplete"


def _current(service: WorkflowService) -> str:
    state, snapshot = service.load(TASK)
    return snapshot.plan.items[state.cursor].name


# --- the gate runs after a trailing assessment's chosen outcome -------------


def _trailing(phase: str) -> str:
    return f"""workflows:
  - name: review
    steps:
      - collect: Collect.
        items:
          steps:
            - assess:
                question: Does this comment need work?
                outcomes:
                  positive:
                    steps:
                      - work: Do the work for this comment.
                        item_phase: {phase}
                  negative:
                    stop_workflow: true
      - wrap: Wrap up.
"""


def _collect_one(service: WorkflowService) -> None:
    assert service.next(TASK).item_name == "collect"
    service.add_item(TASK, WorkItem("c1", "A comment"))
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")


@pytest.mark.parametrize(
    ("phase", "lacking", "record"),
    [
        ("analyze", "c1 (work): processed_item", {"processed_item": "Analysis."}),
        (
            "resolve",
            "c1 (work): actual_solution, resolved=true",
            {"actual_solution": "Fixed.", "resolved": True},
        ),
        ("report", "c1 (work): reported=true", {"reported": True}),
    ],
)
def test_a_trailing_outcome_is_part_of_its_pass(
    tmp_path: Path, phase: str, lacking: str, record: dict[str, object]
) -> None:
    service = _service(tmp_path, _trailing(phase))
    _collect_one(service)
    _step(service, "assess")
    service = _reloaded(tmp_path)
    assert service.status(TASK).choosing_outcome_of == "assess"
    assert service.load(TASK)[0].status != "failed"

    assert service.next(TASK, outcome="positive").item_name == "work"
    service = _reloaded(tmp_path)
    service.complete(TASK, artifact="Worked.", summary_for_next="Done.")

    _stopped_at_gate(service, lacking)
    service = _reloaded(tmp_path)
    service.update_item(TASK, "c1", **record)  # type: ignore[arg-type]
    assert service.next(TASK, retry=True).item_name == "wrap"


def test_a_compact_assessment_ends_its_pass_only_once_answered(
    tmp_path: Path,
) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: review
    steps:
      - collect: Collect.
        items:
          steps:
            - analyze: Analyze this comment.
              item_phase: analyze
            - assess: Is the analysis complete?
      - wrap: Wrap up.
""",
    )
    _collect_one(service)
    _step(service, "analyze")  # without recording the analysis
    _step(service, "assess")

    # Waiting for the answer is not leaving the pass.
    service = _reloaded(tmp_path)
    state, _ = service.load(TASK)
    assert (state.status, state.failure_kind) == ("pending", None)
    assert service.status(TASK).choosing_outcome_of == "assess"

    service.next(TASK, outcome="positive")
    _stopped_at_gate(service, "c1 (analyze): processed_item")
    service = _reloaded(tmp_path)
    # The answer is kept: the stop does not ask for it again.
    assert service.status(TASK).choosing_outcome_of is None
    assert service.instruction(TASK).choosing_outcome_of is None

    update = ("update-item", TASK, "--id", "c1", "--processed-item", "A.")
    assert _cli(tmp_path, *update) == 0
    assert _cli(tmp_path, "next", TASK, "--retry", "--yes", "--role", "manager") == 0
    service = _reloaded(tmp_path)
    state, _ = service.load(TASK)
    assert (_current(service), state.status) == ("wrap", "pending")
    assert service.status(TASK).choosing_outcome_of is None
    _step(service, "wrap")


def test_an_outcome_that_skips_the_work_requires_nothing_of_it(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, _trailing("resolve"))
    _collect_one(service)
    _step(service, "assess")
    # ``mixed`` is not declared: it skips every outcome's work.
    assert service.next(TASK, outcome="mixed").item_name == "wrap"
    assert service.load(TASK)[0].status == "in_progress"


@pytest.mark.parametrize("form", ["declared", "compact"])
def test_an_outcome_that_stops_ends_the_run_without_a_gate(
    tmp_path: Path, form: str
) -> None:
    workflows = (
        _trailing("resolve")
        if form == "declared"
        else """workflows:
  - name: review
    steps:
      - collect: Collect.
        items:
          steps:
            - fix: Fix it.
              item_phase: resolve
            - assess: Is the fix worth keeping?
      - wrap: Wrap up.
"""
    )
    service = _service(tmp_path, workflows)
    _collect_one(service)
    if form == "compact":
        _step(service, "fix")
    _step(service, "assess")
    service.next(TASK, outcome="negative")

    state = _reloaded(tmp_path).load(TASK)[0]
    assert state.status == "completed"
    assert state.failure_kind is None


# --- the pass-gate stop and its operator page -------------------------------


def test_the_gate_stop_says_what_items_lack_and_cannot_be_forced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path, _trailing("report"))
    _collect_one(service)
    _step(service, "assess")
    service.next(TASK, outcome="positive")
    service.complete(TASK, artifact="Reported.", summary_for_next="Done.")

    page = MarkdownOutputAdapter().render_instruction(
        service.instruction(TASK, caller_role="manager")
    )
    assert "## Operator decision: an items pass is missing item records" in page
    assert "c1 (work): reported=true" in page
    assert "the next step has not started" in page
    assert "update-item" in page
    assert "next TASK-1 --retry" in page
    assert "--force" not in page
    assert "handler" not in page.split("### Error", 1)[1].replace(
        "No handler failed", ""
    )

    capsys.readouterr()
    assert _cli(tmp_path, "next", TASK, "--force", "--reason", "x", "--yes") != 0
    assert "pass gate cannot be forced" in capsys.readouterr().err
    with pytest.raises(StateError, match="pass gate cannot be forced"):
        service.force_target(TASK)
    _stopped_at_gate(service, "c1 (work): reported=true")


# --- restart matrix over several passes -------------------------------------

PASSES = """workflows:
  - name: review
    steps:
      - collect: Record one item per comment.
        items:
          steps: []
      - analyze-together: Analyze all collected comments together.
      - confirm-analysis: Reuse the collected items.
        items:
          steps:
            - analyze: Confirm the analysis.
              item_phase: analyze
      - fix-together: Fix every analyzed comment.
      - finish: Reuse the collected items.
        items:
          steps:
            - verify: Verify the fix and record actual_solution.
              item_phase: resolve
            - report: Report the result.
              item_phase: report
      - wrap: Wrap up.
"""


def _replan(root: Path, old: str, new: str, *, refused: bool = False) -> str | None:
    """Edit ``ww.yaml`` and take the change; the step it then begins."""
    path = root / "ww.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    service = WorkflowService(Storage(root))
    stop = service.next(TASK, caller_role="manager")
    assert stop.plan_change is not None
    if refused:
        assert stop.plan_change.refusal is not None
        assert "already expanded" in stop.plan_change.refusal
        path.write_text(text, encoding="utf-8")
        return None
    assert stop.plan_change.refusal is None
    return service.next(TASK, caller_role="manager", replan=True).item_name


def _concrete(service: WorkflowService) -> list[tuple[str, str | None, str]]:
    _, snapshot = service.load(TASK)
    return [
        (item.name, item.item_id, item.description)
        for item in snapshot.plan.items
        if item.item_id is not None and item.phase == "step"
    ]


def test_every_pass_boundary_survives_reload_replan_and_gate_stops(
    tmp_path: Path,
) -> None:
    root = tmp_path
    _service(root, PASSES)

    # Before any pass expanded: every template may still change.
    service = _reloaded(root)
    assert _replan(root, "Report the result.", "Report the outcome.") == "collect"
    service = _reloaded(root)  # during the first collection
    service.add_item(TASK, WorkItem("c1", "First", fields=(("source", "s1"),)))
    service.add_item(TASK, WorkItem("c2", "Second", fields=(("source", "s2"),)))
    service = _reloaded(root)
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    assert _concrete(service) == []  # collect-only expands nothing

    # Between the collect-only pass and the analyze pass.
    service = _reloaded(root)
    assert (
        _replan(root, "Confirm the analysis.", "Confirm the shared analysis.")
        == "analyze-together"
    )
    service.update_item(TASK, "c1", processed_item="Shared analysis.")
    service.complete(TASK, artifact="Analyzed.", summary_for_next="Done.")
    service = _reloaded(root)
    assert service.next(TASK).item_name == "confirm-analysis"
    service = _reloaded(root)  # during the second pass's collection
    service.complete(TASK, artifact="Reused.", summary_for_next="Done.")
    assert _concrete(service) == [
        ("analyze", "c1", "Confirm the shared analysis."),
        ("analyze", "c2", "Confirm the shared analysis."),
    ]

    # The expanded pass is fixed; a later one may still change.
    service = _reloaded(root)
    _replan(root, "Confirm the shared analysis.", "Other.", refused=True)
    assert _replan(root, "Report the outcome.", "Report it.") == "analyze"
    service = _reloaded(root)
    service.complete(TASK, artifact="Confirmed.", summary_for_next="Done.")
    service = _reloaded(root)  # mid-pass
    _step(service, "analyze")  # c2, still without an analysis

    # Gate stop: reload, refuse force, record over the CLI, retry.
    service = _reloaded(root)
    _stopped_at_gate(service, "c2 (analyze): processed_item")
    assert _cli(root, "next", TASK, "--force", "--reason", "x", "--yes") != 0
    assert _cli(root, "update-item", TASK, "--id", "c2", "--processed-item", "A.") == 0
    assert _cli(root, "next", TASK, "--retry", "--yes", "--role", "manager") == 0
    service = _reloaded(root)
    assert _current(service) == "fix-together"
    _step(service, "fix-together")

    # The last pass: expanded with the replanned text, then the wrap may change.
    service = _reloaded(root)
    _step(service, "finish")
    assert [entry for entry in _concrete(service) if entry[0] == "report"] == [
        ("report", "c1", "Report it."),
        ("report", "c2", "Report it."),
    ]
    service = _reloaded(root)
    assert _replan(root, "Wrap up.", "Wrap everything up.") == "verify"
    service.update_item(TASK, "c1", actual_solution="Fixed.", resolved=True)
    service.complete(TASK, artifact="Verified.", summary_for_next="Done.")
    service = _reloaded(root)
    assert service.next(TASK).item_name == "report"
    service.update_item(TASK, "c1", reported=True)
    service.complete(TASK, artifact="Reported.", summary_for_next="Done.")
    assert service.next(TASK).item_name == "verify"
    service.update_item(TASK, "c2", actual_solution="Fixed.", resolved=True)
    service.complete(TASK, artifact="Verified.", summary_for_next="Done.")
    service = _reloaded(root)
    _step(service, "report")  # c2 not reported

    service = _reloaded(root)
    _stopped_at_gate(service, "c2 (report): reported=true")
    assert "c1" not in (service.load(TASK)[0].last_error or "")
    service.update_item(TASK, "c2", reported=True)
    assert service.next(TASK, retry=True).item_name == "wrap"
    _step(service, "wrap")

    # After the last pass.
    service = _reloaded(root)
    assert service.next(TASK).item_name == "update-workflow-summary"
    service.complete(TASK, (("summary", "Reviewed."),))
    service = _reloaded(root)
    assert service.load(TASK)[0].status == "completed"
    assert [
        (item.id, item.processed_item, item.resolved, item.reported, item.fields)
        for item in service.items(TASK)
    ] == [
        ("c1", "Shared analysis.", True, True, (("source", "s1"),)),
        ("c2", "A.", True, True, (("source", "s2"),)),
    ]


PERSISTENT = """workflows:
  - name: review
    restartable: true
    steps:
      - collect: Make the items match the comments.
        items:
          persistent: true
          identity: source
          steps: []
      - analyze-together: Analyze all comments.
      - check: Reuse the collected items.
        items:
          persistent: true
          steps:
            - analyze: Confirm the analysis.
              item_phase: analyze
      - finish: Reuse the collected items.
        items:
          steps:
            - report: Report it.
              item_phase: report
"""


def _persistent_run(
    root: Path, service: WorkflowService, new: tuple[str, ...]
) -> WorkflowService:
    """One run: add ``new`` items, analyze all, then report each one."""
    assert service.next(TASK).item_name == "collect"
    for item_id in new:
        service.add_item(
            TASK, WorkItem(item_id, item_id, fields=(("source", item_id),))
        )
    service.complete(TASK, artifact="Reconciled.", summary_for_next="Done.")
    service = _reloaded(root)
    assert service.next(TASK).item_name == "analyze-together"
    for item in service.items(TASK):
        service.update_item(TASK, item.id, processed_item="Analysis.")
    service.complete(TASK, artifact="Analyzed.", summary_for_next="Done.")
    ids = [item.id for item in service.items(TASK)]
    _step(service, "check")
    service = _reloaded(root)
    for _ in ids:
        _step(service, "analyze")
    _step(service, "finish")
    for item_id in ids:
        service = _reloaded(root)
        assert service.next(TASK).item_name == "report"
        service.update_item(TASK, item_id, reported=True)
        service.complete(TASK, artifact="Reported.", summary_for_next="Done.")
    assert service.next(TASK).item_name == "update-workflow-summary"
    service.complete(TASK, (("summary", "Done."),))
    return _reloaded(root)


def test_a_persistent_collection_runs_every_pass_again_in_the_next_run(
    tmp_path: Path,
) -> None:
    service = _persistent_run(tmp_path, _service(tmp_path, PERSISTENT), ("c1", "c2"))
    assert service.load(TASK)[0].status == "completed"

    start_after_init(service, "review", TASK, agent="codex", workflow_runtime="single")
    service = _reloaded(tmp_path)
    # Seeded from the store with outcomes cleared; the settings still hold.
    assert [(i.id, i.processed_item, i.reported) for i in service.items(TASK)] == [
        ("c1", "", False),
        ("c2", "", False),
    ]
    service = _persistent_run(tmp_path, service, ("c3",))
    state, snapshot = service.load(TASK)
    assert (state.run_id, state.status) == ("02-review", "completed")
    assert [
        (item.item_pass, item.item_id)
        for item in snapshot.plan.items
        if item.name == "report"
    ] == [("finish", "c1"), ("finish", "c2"), ("finish", "c3")]
    assert [
        (item.id, item.reported) for item in service.tasks.read_shared_items(TASK)
    ] == [("c1", True), ("c2", True), ("c3", True)]


NESTED = """workflows:
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
                - analyze: Analyze it.
                  item_phase: analyze
                - attempts: Try until it holds.
                  loop:
                    - attempt: Try a fix.
                      break: The fix holds.
          - decide: Decide whether another round is needed.
            break: No new comments.
      - wrap: Wrap up.
"""


def test_nested_loops_gate_each_round_and_break_the_nearest_loop(
    tmp_path: Path,
) -> None:
    root = tmp_path
    service = _service(root, NESTED)
    assert service.next(TASK).item_name == "collect"
    service.add_item(TASK, WorkItem("c1", "First"))
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    _step(service, "triage")
    _step(service, "analyze")  # without the analysis
    assert service.next(TASK).item_name == "attempt"
    service = _reloaded(root)
    # The break leaves only the per-item loop, which ends the pass.
    service.loop(TASK, artifact="Holds.", summary_for_next="Done.")
    _stopped_at_gate(service, "c1 (analyze): processed_item")
    service = _reloaded(root)
    service.update_item(TASK, "c1", processed_item="Analysis.")
    assert service.next(TASK, retry=True).item_name == "decide"
    _step(service, "decide")  # another round

    # Round two: the pass expands anew, for both items, with fresh records.
    service = _reloaded(root)
    assert service.next(TASK).item_name == "collect"
    service.add_item(TASK, WorkItem("c2", "Second"))
    service = _reloaded(root)
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    _step(service, "triage")
    for item_id in ("c1", "c2"):
        service = _reloaded(root)
        assert service.next(TASK).item_name == "analyze"
        if item_id == "c2":
            service.update_item(TASK, item_id, processed_item="Analysis.")
        service.complete(TASK, artifact="Analyzed.", summary_for_next="Done.")
        assert service.next(TASK).item_name == "attempt"
        service.loop(TASK, artifact="Holds.", summary_for_next="Done.")
    # c1 kept its analysis from round one, so the gate passes.
    service = _reloaded(root)
    assert service.next(TASK).item_name == "decide"
    service.loop(TASK, artifact="No new comments.", summary_for_next="Done.")
    service = _reloaded(root)
    assert service.next(TASK).item_name == "wrap"
    state, _ = service.load(TASK)
    assert dict(state.loop_iterations)["rounds"] == 2
