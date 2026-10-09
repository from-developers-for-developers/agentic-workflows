# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end behavior for the normalized, compiled-plan executor."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.workflow_helpers import (
    advance_init,
    configured_service,
    run_state_path,
    start_after_init,
)
from ww.cli import main
from ww.errors import StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage


def _write_workflow(root: Path) -> None:
    (root / "ww.yaml").write_text(
        """handlers:
  - name: prepare
    shell: printf start > started.txt
  - name: record
    variables:
      - name: commit_message
        description: Commit summary.
    shell: printf "%s" "$COMMIT_MESSAGE" > committed.txt
    env:
      COMMIT_MESSAGE: "{{commit_message}}"

hooks:
  before_start_workflow:
    - name: prepare
  before_complete:
    - steps: [fix]
      name: record

workflows:
  - name: task
    steps:
      - name: group
        steps:
          - name: plan
            kind: prompt
            description: Plan it.
          - name: fix
            kind: prompt
            description: Fix it.
  - name: choose
    steps:
      - name: select
        kind: prompt
        variables:
          - name: workflow
            description: Target workflow.
        hooks:
          after_complete:
            - handoff_to: "{{workflow}}"
""",
        encoding="utf-8",
    )


def test_execution_snapshot_automatic_input_nested_state_and_artifacts(
    tmp_path: Path,
) -> None:
    _write_workflow(tmp_path)
    service = WorkflowService(Storage(tmp_path))

    ready = service.start(
        "task",
        "TASK-1",
        agent="codex",
        init_artifact="Record the task requirements.",
    )
    assert ready.item_name == "plan"
    assert (tmp_path / "started.txt").read_text() == "start"
    task_path = tmp_path / ".ww/tasks/TASK-1"
    assert (task_path / "state.json").exists()
    assert (task_path / "metadata.json").exists()
    assert not (task_path / "aggregate.json").exists()
    assert not (task_path / "runs/01-task/plan.json").exists()
    assert run_state_path(tmp_path, "TASK-1").exists()
    index = json.loads((task_path / "state.json").read_text(encoding="utf-8"))
    assert index["runs"] == [{"id": "01-task", "revision": index["revision"]}]

    active = service.next("TASK-1")
    assert active.item_status == "in_progress"
    assert (
        active.continuation_command == "./ww complete TASK-1 --role worker "
        '--artifact="<whole result in Markdown>" '
        '--summary="<one or two sentences for the next step>"'
    )
    ready = service.complete("TASK-1", artifact="# plan\n", summary_for_next="Done.")
    assert ready.item_name == "fix"
    artifacts = service.artifacts("TASK-1")
    assert artifacts[0]["step"] == "init"
    assert artifacts[0]["artifact"].endswith("01-init.md")
    assert (
        tmp_path / ".ww/tasks/TASK-1/runs/01-task/steps/02-group/01-plan.md"
    ).exists()
    active = service.next("TASK-1")
    assert active.required_values[0].name == "commit_message"
    summary = service.complete(
        "TASK-1",
        (("commit_message", "Implement feature"),),
        "# fix\n",
        summary_for_next="Done.",
    )
    assert summary.item_name == "update-workflow-summary"
    service.next("TASK-1")
    done = service.complete(
        "TASK-1",
        (("summary", "Implemented feature."),),
        summary_for_next="Done.",
    )

    assert done.status == "completed"
    assert (tmp_path / "committed.txt").read_text() == "Implement feature"
    assert (
        tmp_path / ".ww/tasks/TASK-1/runs/01-task/steps/02-group/02-fix.md"
    ).read_text() == (
        "# TASK-1 — group/fix\n\n"
        "## Workflow context\n\n"
        "- Workflow: task\n"
        "- Step: 2.2 of 2\n"
        "- Skill: auto\n\n"
        "## Result\n\n"
        "# fix\n"
    )
    state = service.tasks.read_execution_state("TASK-1", "01-task")
    assert state is not None
    assert state.steps[1].status == "completed"
    assert state.steps[1].children[0].status == "completed"
    assert state.steps[1].children[1].status == "completed"


def test_assess_routes_to_selected_conditional_outcome(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: record-positive
    description: Positive follow-up.
workflows:
  - name: task
    steps:
      - assess:
          question: Does this warrant follow-up?
          outcomes:
            positive:
              handler: record-positive
            negative:
              steps:
                - record: Negative follow-up.
      - after: Continue normally.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-1", agent="codex")

    assessment = service.next("TASK-1")
    assert assessment.item_name == "assess"
    service.complete(
        "TASK-1",
        artifact="Assessment: positive.",
        summary_for_next="Done.",
    )
    # A plain next runs nothing; it shows the choice again.
    assert service.next("TASK-1").choosing_outcome_of == "assess"

    selected = service.next("TASK-1", outcome="positive")
    assert selected.item_name == "positive"
    service.complete("TASK-1", artifact="Done.", summary_for_next="Done.")
    assert service.next("TASK-1").item_name == "after"


def test_handoff_starts_the_target_as_its_own_run(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "choose", "TASK-2", agent="codex")
    service.next("TASK-2")
    ready = service.complete(
        "TASK-2",
        (("workflow", "task"),),
        "# selection\n",
        summary_for_next="Done.",
    )

    assert ready.workflow == "task"
    assert ready.item_name == "plan"
    assert service.tasks.read_handoff("TASK-2") == "choose=task"
    assert not (tmp_path / ".ww/tasks/TASK-2/.handoff").exists()

    # Each workflow keeps its own plan, state and artifacts.
    selection = service.tasks.read_plan_snapshot("TASK-2", "01-choose")
    target = service.tasks.read_plan_snapshot("TASK-2", "02-task")
    assert selection is not None and selection.plan.workflow == "choose"
    assert target is not None and target.plan.workflow == "task"
    assert (
        tmp_path / ".ww/tasks/TASK-2/runs/01-choose/steps/02-select.md"
    ).read_text() == (
        "# TASK-2 — select\n\n"
        "## Workflow context\n\n"
        "- Workflow: choose\n"
        "- Step: 2 of 2\n"
        "- Skill: auto\n\n"
        "## Result\n\n"
        "# selection\n"
    )
    assert (tmp_path / ".ww/tasks/TASK-2/runs/02-task/steps/01-init.md").exists()

    finished = service.tasks.read_execution_state("TASK-2", "01-choose")
    assert finished is not None and finished.status == "completed"
    assert finished.item_executions[-1].status == "completed"

    # Both runs are recorded, and the task points at the successor.
    assert [
        (run.run_id, run.workflow, run.status, run.summary)
        for run in service.tasks.execution_runs("TASK-2")
    ] == [
        ("01-choose", "choose", "completed", "handed off to task"),
        # The target's own before_start_workflow hook has already run, exactly as it
        # would have on a direct `start`.
        ("02-task", "task", "in_progress", None),
    ]
    assert service.tasks.active_execution_run("TASK-2") == "02-task"


def test_a_finished_run_is_recorded_once_in_the_ledger(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: implementation
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "implementation", "TASK-8", agent="codex")
    service.next("TASK-8")
    service.complete("TASK-8", artifact="done", summary_for_next="Done.")
    service.next("TASK-8")
    service.complete(
        "TASK-8",
        (("summary", "Did the work."),),
        summary_for_next="Done.",
    )

    # Completion is recorded by finalize_execution_run alone, so the summary
    # arrives with the row rather than in a second one that supersedes it.
    assert _ledger(tmp_path, "TASK-8") == [
        ("01-implementation", "pending", None),
        ("01-implementation", "in_progress", None),
        ("01-implementation", "completed", "Did the work."),
    ]


def test_handoff_records_each_run_once(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "choose", "TASK-9", agent="codex")
    service.next("TASK-9")
    service.complete(
        "TASK-9",
        (("workflow", "task"),),
        "# selection\n",
        summary_for_next="Done.",
    )

    rows = _ledger(tmp_path, "TASK-9")
    assert [row for row in rows if row[1] == "completed"] == [
        ("01-choose", "completed", "handed off to task")
    ]
    assert rows[-1][0] == "02-task"


def _ledger(root: Path, task_id: str) -> list[tuple[str, str, str | None]]:
    state = json.loads(
        (root / ".ww" / "tasks" / task_id / "state.json").read_text(encoding="utf-8")
    )
    return [
        (run_id, event["status"], event.get("summary"))
        for run_id, events in state.get("ledger", {}).items()
        for event in events
    ]


def test_handoff_target_run_continues_normally(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "choose", "TASK-3", agent="codex")
    service.next("TASK-3")
    service.complete(
        "TASK-3",
        (("workflow", "task"),),
        "# selection\n",
        summary_for_next="Done.",
    )

    active = service.next("TASK-3")
    assert active.item_name == "plan"
    ready = service.complete(
        "TASK-3",
        artifact="# research\n",
        summary_for_next="Done.",
    )

    assert ready.item_name == "fix"
    assert (
        tmp_path / ".ww/tasks/TASK-3/runs/02-task/steps/01-init.md"
    ).read_text() == (
        "# TASK-3 — init\n\n"
        "## Workflow context\n\n"
        "- Workflow: task\n"
        "- Step: 1 of 2\n"
        "- Skill: auto\n\n"
        "## Result\n\n"
        "Continue task requirements after handoff from choose to task.\n"
    )


def test_task_can_keep_multiple_workflow_runs_and_summaries(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: implementation
    steps:
      - name: work
        kind: prompt
  - name: code-review
    steps:
      - name: review
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "implementation", "TASK-7", agent="codex")
    service.next("TASK-7")
    summary = service.complete(
        "TASK-7",
        artifact="implementation done",
        summary_for_next="Done.",
    )
    assert summary.item_name == "update-workflow-summary"
    service.next("TASK-7")
    done = service.complete(
        "TASK-7",
        (("summary", "Implemented the feature."),),
        summary_for_next="Done.",
    )
    assert done.status == "completed"

    review = start_after_init(service, "code-review", "TASK-7", agent="codex")
    assert review.item_name == "review"
    runs = service.tasks.execution_runs("TASK-7")
    assert [(run.run_id, run.status, run.summary) for run in runs] == [
        ("01-implementation", "completed", "Implemented the feature."),
        ("02-code-review", "in_progress", None),
    ]
    assert service.tasks.read_plan_snapshot("TASK-7", "01-implementation") is not None
    assert service.tasks.read_plan_snapshot("TASK-7", "02-code-review") is not None
    assert not (tmp_path / ".ww/tasks/TASK-7/runs/01-implementation/plan.json").exists()
    assert not (tmp_path / ".ww/tasks/TASK-7/runs/02-code-review/plan.json").exists()

    overview = service.instruction("TASK-7")
    assert overview.status == "task_summary"
    rendered = MarkdownOutputAdapter().render_instruction(overview)
    assert "./ww instruction TASK-7 --run <run-id>" in rendered
    assert service.instruction("TASK-7", "01-implementation").status == "completed"


def test_aggregate_reads_ignore_a_stale_ledger_projection(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-aggregate", agent="codex")

    ledger = tmp_path / ".ww/tasks/TASK-aggregate/task.jsonl"
    ledger.write_text(
        '{"run_id":"01-task","workflow":"wrong","status":"failed"}\n',
        encoding="utf-8",
    )

    runs = service.tasks.execution_runs("TASK-aggregate")
    assert runs[0].workflow == "task"
    assert runs[0].status != "failed"


def test_ordinary_transitions_preserve_the_logical_plan_and_digest(
    tmp_path: Path,
) -> None:
    _write_workflow(tmp_path)
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-stable-plan", agent="codex")
    original = service.tasks.read_plan_snapshot("TASK-stable-plan", "01-task")
    assert original is not None

    service.next("TASK-stable-plan")

    current = service.tasks.read_plan_snapshot("TASK-stable-plan", "01-task")
    assert current == original
    assert current.plan_digest == original.plan_digest


def test_aggregate_commit_uses_compare_and_swap_revision(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-cas", agent="codex")
    runs, handoff = service.tasks.read_task_aggregate("TASK-cas")
    revision = service.tasks.task_aggregate_revision("TASK-cas")

    with pytest.raises(StateError, match="revision conflict"):
        service.tasks.commit_task_aggregate(
            "TASK-cas",
            runs,
            handoff,
            expected_revision=revision - 1,
        )


def test_leading_automatic_input_is_collected_without_creating_agent_work(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: setup
    variables:
      - name: token
        description: Setup token.
    shell: printf "%s" "$TOKEN" > token.txt
    env:
      TOKEN: "{{token}}"
hooks:
  before_start_workflow:
    - name: setup
workflows:
  - name: task
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    input_needed = start_after_init(service, "task", "TASK-3", agent="codex")
    assert input_needed.item_status == "awaiting_input"
    assert input_needed.item_name == "setup"
    assert input_needed.required_values[0].name == "token"
    ready = advance_init(
        service,
        service.complete(
            "TASK-3",
            (("token", "abc"),),
            summary_for_next="Done.",
        ),
    )

    assert ready.item_name == "work"
    assert (tmp_path / "token.txt").read_text() == "abc"


def test_a_failed_handler_asks_for_its_values_again_on_retry(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: gate
    variables:
      - name: value
        description: Must be ok.
    shell: test "$VALUE" = ok
    env:
      VALUE: "{{value}}"
hooks:
  before_complete:
    - steps: [work]
      name: gate
workflows:
  - name: task
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-REASK", agent="codex")
    service.next("TASK-REASK")

    failed = service.complete(
        "TASK-REASK", (("value", "bad"),), "# work\n", summary_for_next="Done."
    )
    assert failed.status == "failed"

    # A plain ``next`` retries the known failure and asks again rather than
    # replaying the value the handler failed with.
    asked = service.next("TASK-REASK")
    assert asked.item_status == "awaiting_input"
    assert asked.item_name == "gate"
    assert [value.name for value in asked.required_values] == ["value"]
    assert asked.previous_values == (("value", "bad"),)
    asking = service.tasks.read_execution_state("TASK-REASK", "01-task")
    assert asking is not None
    assert "value" not in dict(asking.workflow_values)
    assert asking.item_executions[2].supplied_values == (("value", "bad"),)

    failed_again = service.complete("TASK-REASK", (("value", "bad"),))
    assert failed_again.status == "failed"

    asked = service.next("TASK-REASK", retry=True)
    assert asked.item_status == "awaiting_input"
    assert asked.previous_values == (("value", "bad"),)

    passed = service.complete("TASK-REASK", (("value", "ok"),))

    assert passed.status != "failed"
    assert passed.item_name == "update-workflow-summary"
    state = service.tasks.read_execution_state("TASK-REASK", "01-task")
    assert state is not None
    assert state.item_executions[2].status == "completed"
    assert state.item_executions[2].attempts == 3
    # The accepted value stays on the handler's record; the run-wide values
    # release it so the next consumer asks afresh.
    assert state.item_executions[2].supplied_values == (("value", "ok"),)
    assert "value" not in dict(state.workflow_values)


def test_interpolated_command_values_remain_data(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: safe-write
    variables:
      - name: value
    shell: >-
      printf '%s' "$VALUE" > result.txt;
      printf '%s' "$1" > argument.txt;
      printf '%s' "$1"
    args: ["{{value}}"]
    env:
      VALUE: "{{value}}"
hooks:
  before_start_workflow:
    - name: safe-write
workflows:
  - name: task
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    payload = "$(touch injected.txt)"

    start_after_init(service, "task", "TASK-SAFE-ARGS", agent="codex")
    ready = advance_init(
        service,
        service.complete(
            "TASK-SAFE-ARGS",
            (("value", payload),),
            summary_for_next="Done.",
        ),
    )

    assert ready.item_name == "work"
    assert not (tmp_path / "injected.txt").exists()
    assert (tmp_path / "result.txt").read_text() == payload
    assert (tmp_path / "argument.txt").read_text() == payload
    state = service.tasks.read_execution_state("TASK-SAFE-ARGS", "01-task")
    assert state is not None
    assert state.item_executions[0].commands[0].stdout == payload


def test_full_command_output_is_kept_out_of_hot_execution_state(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: produce
    shell: printf '%020000d' 0
hooks:
  before_start_workflow:
    - name: produce
workflows:
  - name: task
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "task", "TASK-OUTPUT", agent="codex")

    state = service.tasks.read_execution_state("TASK-OUTPUT", "01-task")
    assert state is not None
    command = state.item_executions[0].commands[0]
    assert command.stdout_ref is not None
    assert len(command.stdout) < 2_000
    assert len(service.tasks.read_command_output(command.stdout_ref)) == 20_000
    run_state = run_state_path(tmp_path, "TASK-OUTPUT").read_text()
    assert len(run_state) < 30_000


def test_automatic_command_failure_retries_only_the_failed_handler(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """hooks:
  before_start_workflow:
    - handlers:
        - name: count-once
          shell: printf once >> counter.txt
        - name: refuse
          argv: ["false"]
workflows:
  - name: task
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    failed = start_after_init(service, "task", "TASK-4", agent="codex")
    assert failed.status == "failed"
    assert (tmp_path / "counter.txt").read_text() == "once"
    retried = service.next("TASK-4", retry=True)

    assert retried.status == "failed"
    assert (tmp_path / "counter.txt").read_text() == "once"


def test_automatic_command_failure_marks_hook_step_failed(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: setup
    argv: ["false"]
hooks:
  before_start_workflow:
    - name: setup
workflows:
  - name: task
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    failed = start_after_init(service, "task", "TASK-4", agent="codex")
    run_id = service.tasks.active_execution_run("TASK-4")
    state = service.tasks.read_execution_state("TASK-4", run_id)

    assert failed.status == "failed"
    assert failed.stage == "Before Workflow Start hook"
    assert state is not None
    assert state.item_executions[0].status == "failed"
    assert state.steps[0].status == "failed"


def test_interrupted_automatic_handler_requires_explicit_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: publish
    shell: printf published > published.txt
hooks:
  before_complete:
    - steps: [work]
      name: publish
workflows:
  - name: task
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-INTERRUPTED", agent="codex")
    service.next("TASK-INTERRUPTED")

    def interrupted(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr("ww.action_execution._PROCESS.Popen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.complete(
            "TASK-INTERRUPTED",
            artifact="# done\n",
            summary_for_next="Done.",
        )

    resumed = WorkflowService(Storage(tmp_path))
    visible = resumed.instruction("TASK-INTERRUPTED")
    assert visible.status == "in_progress"
    assert visible.item_status == "in_progress"
    assert "may still be running" in (visible.error or "")
    # Only a locked mutating command classifies the operation as interrupted.
    assert resumed.next("TASK-INTERRUPTED").status == "interrupted"
    with pytest.raises(StateError, match="require --mark-succeeded"):
        resumed.recover("TASK-INTERRUPTED", output="captured")

    done = resumed.recover("TASK-INTERRUPTED", mark_succeeded=True)
    assert done.item_name == "update-workflow-summary"
    done = resumed.next("TASK-INTERRUPTED")
    done = resumed.complete(
        "TASK-INTERRUPTED",
        (("summary", "published"),),
        summary_for_next="Done.",
    )
    assert done.status == "completed"
    assert not (tmp_path / "published.txt").exists()


def test_cli_recovery_attests_one_handler_and_continues_with_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """hooks:
  before_complete:
    - steps: [work]
      handlers:
        - name: publish-second
          shell: printf second > second.txt
        - name: publish-third
          shell: printf third > third.txt
workflows:
  - name: task
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-SEGMENTS", agent="codex")
    service.next("TASK-SEGMENTS")

    def interrupted(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    real_popen = subprocess.Popen
    monkeypatch.setattr("ww.action_execution._PROCESS.Popen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.complete("TASK-SEGMENTS", artifact="# done\n", summary_for_next="Done.")

    monkeypatch.setattr("ww.action_execution._PROCESS.Popen", real_popen)
    resumed = WorkflowService(Storage(tmp_path))
    resumed.next("TASK-SEGMENTS")
    ready = resumed.recover("TASK-SEGMENTS", mark_succeeded=True, output="first-output")
    assert ready.item_name == "update-workflow-summary"
    assert not (tmp_path / "second.txt").exists()
    assert (tmp_path / "third.txt").read_text() == "third"
    state = resumed.tasks.read_execution_state("TASK-SEGMENTS", "01-task")
    assert state is not None
    attested, following = state.item_executions[2:4]
    assert [command.status for command in attested.commands] == ["completed"]
    assert attested.commands[0].stdout == "first-output"
    assert [command.status for command in following.commands] == ["completed"]


def test_interrupted_idempotent_handler_replays_without_an_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: count
    shell: printf x >> count.txt
    idempotent: true
hooks:
  before_complete:
    - steps: [work]
      name: count
workflows:
  - name: task
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-IDEMPOTENT", agent="codex")
    service.next("TASK-IDEMPOTENT")

    def interrupted(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    real_popen = subprocess.Popen
    monkeypatch.setattr("ww.action_execution._PROCESS.Popen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.complete(
            "TASK-IDEMPOTENT", artifact="# done\n", summary_for_next="Done."
        )
    monkeypatch.setattr("ww.action_execution._PROCESS.Popen", real_popen)
    resumed = WorkflowService(Storage(tmp_path))
    interrupted_state = resumed.tasks.read_execution_state("TASK-IDEMPOTENT", "01-task")
    assert interrupted_state is not None
    stale = interrupted_state.item_executions[2].commands[0]
    assert stale.status == "in_progress"

    continued = resumed.next("TASK-IDEMPOTENT")

    # The declared-harmless replay ran inside ``next``; nobody was asked.
    assert continued.status != "interrupted"
    assert continued.item_name == "update-workflow-summary"
    assert (tmp_path / "count.txt").read_text() == "x"
    state = resumed.tasks.read_execution_state("TASK-IDEMPOTENT", "01-task")
    assert state is not None
    replayed = state.item_executions[2].commands[0]
    assert replayed.status == "completed"
    assert replayed.attempts == 2
    assert replayed.operation_id == stale.operation_id


def test_a_crash_after_a_failed_exit_is_a_known_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: check
    shell: printf boom >&2; exit 3
hooks:
  before_complete:
    - steps: [work]
      name: check
workflows:
  - name: task
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-KNOWN", agent="codex")
    service.next("TASK-KNOWN")

    def crashed(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    # The process exited and its segment was recorded; ww died before the
    # coordinator could write the item failure.
    with monkeypatch.context() as patch:
        patch.setattr("ww.action_execution.ActionExecutor._fail_item", crashed)
        with pytest.raises(KeyboardInterrupt):
            service.complete(
                "TASK-KNOWN", artifact="# done\n", summary_for_next="Done."
            )

    resumed = WorkflowService(Storage(tmp_path))
    stopped = resumed.next("TASK-KNOWN")

    assert stopped.status == "failed"
    assert stopped.error is not None
    assert "failed (3) at command 1" in stopped.error
    assert "before it recorded the failure" in stopped.error
    assert stopped.error.endswith("boom")
    state = resumed.tasks.read_execution_state("TASK-KNOWN", "01-task")
    assert state is not None
    assert state.item_executions[2].status == "failed"
    assert state.item_executions[2].commands[0].status == "failed"

    retried = resumed.next("TASK-KNOWN", retry=True)

    assert retried.status == "failed"
    assert retried.error is not None
    assert "before it recorded" not in retried.error
    state = resumed.tasks.read_execution_state("TASK-KNOWN", "01-task")
    assert state is not None
    assert state.item_executions[2].commands[0].attempts == 2


def test_automatic_command_persists_attempt_and_operation_environment(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: inspect
    shell: >-
      printf '%s|%s|%s' "$WW_OPERATION_ATTEMPT"
      "$WW_ITEM_OPERATION_ID" "$WW_OPERATION_ID" > env.txt
hooks:
  before_start_workflow:
    - name: inspect
workflows:
  - name: task
    steps:
      - name: work
        kind: prompt
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-ENV", agent="codex")
    state = service.tasks.read_execution_state("TASK-ENV", "01-task")
    assert state is not None
    command = state.item_executions[0].commands[0]
    assert command.attempts == 1
    assert command.operation_id is not None
    values = (tmp_path / "env.txt").read_text().split("|")
    assert values[0] == "1"
    assert values[1] == state.item_executions[0].operation_id
    assert values[2] == command.operation_id


def test_execution_uses_snapshot_after_configuration_changes(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-5", agent="codex")
    config_file = tmp_path / "ww.yaml"
    config_file.write_text("not: valid\n", encoding="utf-8")

    active = service.next("TASK-5")
    assert active.item_name == "plan"
    with pytest.raises(StateError, match="unexpected completion variable"):
        service.complete("TASK-5", (("unexpected", "value"),), summary_for_next="Done.")


def test_dummy_runner_drives_real_automatic_handlers(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    script = Path(__file__).parents[2] / "scripts" / "run_workflow_dummy.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "TASK-6",
            "--workflow",
            "task",
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Workflow completed: TASK-6 (task)" in result.stdout
    assert (tmp_path / "committed.txt").read_text() == "dummy-commit_message"


def test_init_named_preparation_hook_runs_before_implicit_init(tmp_path: Path) -> None:
    service = configured_service(
        tmp_path,
        """handlers:
  - name: init
    description: Approve the requirements before initialization.
workflows:
  - name: task
    hooks:
      before_start:
        - steps: [init]
          name: init
    steps:
      - name: work
        artifact: false
""",
    )

    service.start("task", "TASK-INIT", init_artifact="requirements")
    initial = service.tasks.read_execution_state("TASK-INIT", "01-task")
    assert initial is not None
    assert initial.item_executions[0].status == "pending"
    assert initial.item_executions[1].status == "pending"
    assert initial.pending_init_artifact == "requirements"

    service.next("TASK-INIT")
    service.complete("TASK-INIT", summary_for_next="Done.")
    completed = service.tasks.read_execution_state("TASK-INIT", "01-task")
    assert completed is not None
    assert completed.item_executions[1].status == "completed"
    assert completed.pending_init_artifact is None


def test_start_retains_requirements_through_init_preparation_and_stops_before_work(
    tmp_path: Path,
) -> None:
    service = configured_service(
        tmp_path,
        """handlers:
  - name: approve
    description: Approve requirements.
  - name: prepare
    variables:
      - name: note
    argv: [printf, "{{note}}"]
workflows:
  - name: task
    hooks:
      before_start:
        - steps: [init]
          name: approve
    steps:
      - name: work
        hooks:
          before_start:
            - name: prepare
""",
    )

    approval = service.start("task", "T", init_artifact="Requirements.")
    assert approval.item_name == "approve"
    service.next("T")
    ready = service.complete("T", artifact="approved", summary_for_next="Done.")
    assert ready.item_name == "prepare"
    state = service.tasks.read_execution_state("T", "01-task")
    assert state is not None
    assert state.item_executions[1].artifact is not None
    assert state.status == "pending"


def test_reset_creates_a_new_operation_identity_for_the_same_run_name(
    tmp_path: Path,
) -> None:
    service = configured_service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: work
        artifact: false
""",
    )

    start_after_init(service, "task", "TASK-1", agent="codex")
    first = service.tasks.read_execution_state("TASK-1", "01-task")
    assert first is not None

    service.reset("TASK-1")
    start_after_init(service, "task", "TASK-1", agent="codex")
    second = service.tasks.read_execution_state("TASK-1", "01-task")
    assert second is not None

    assert first.execution_instance_id
    assert second.execution_instance_id
    assert first.execution_instance_id != second.execution_instance_id
    first_operation = first.item_executions[0].operation_id
    second_operation = second.item_executions[0].operation_id
    assert first_operation != second_operation


def test_reset_does_not_recover_a_git_effect_from_the_previous_execution(
    tmp_path: Path,
) -> None:
    def git(*arguments: str) -> str:
        return subprocess.run(
            ("git", *arguments),
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "ww test")
    git("config", "user.email", "ww@example.test")
    (tmp_path / ".gitignore").write_text(".ww/\ntasks/\n", encoding="utf-8")
    service = configured_service(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: work
        artifact: false
        hooks:
          after_complete:
            - name: ext/ww/git/handlers:git-commit
""",
    )
    git("add", ".")
    git("commit", "-qm", "seed")

    start_after_init(service, "task", "T", agent="codex")
    service.next("T")
    (tmp_path / "work.txt").write_text("first", encoding="utf-8")
    service.complete("T", (("commit_message", "first"),), summary_for_next="Done.")
    first_head = git("rev-parse", "HEAD")

    service.reset("T")
    start_after_init(service, "task", "T", agent="codex")
    service.next("T")
    (tmp_path / "work.txt").write_text("second", encoding="utf-8")
    service.complete("T", (("commit_message", "second"),), summary_for_next="Done.")

    assert git("rev-parse", "HEAD") != first_head
    assert git("show", "HEAD:work.txt") == "second"
    assert git("status", "--porcelain") == ""


def test_process_creation_failure_is_a_retryable_cli_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configured_service(
        tmp_path,
        """workflows:
  - name: task
    hooks:
      before_start_workflow:
        - argv: [/nonexistent-ww-command]
    steps:
      - name: work
""",
    )

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "start",
            "T",
            "--workflow",
            "task",
            "--agent",
            "codex",
            "--requirements",
            "requirements",
        ]
    )

    assert exit_code == 1
    assert "could not launch command 1" in capsys.readouterr().out
    service = WorkflowService(Storage(tmp_path))
    state = service.tasks.read_execution_state("T", "01-task")
    assert state is not None
    assert state.status == "failed"
    assert state.item_executions[0].commands[0].status == "failed"
    assert service.next("T").status == "failed"
    retried = service.tasks.read_execution_state("T", "01-task")
    assert retried is not None
    assert retried.item_executions[0].commands[0].attempts == 2


def test_error_after_process_creation_keeps_the_outcome_unknown(
    tmp_path: Path,
) -> None:
    service = configured_service(
        tmp_path,
        """workflows:
  - name: task
    hooks:
      before_start_workflow:
        - argv: [touch, effect.txt]
    steps:
      - name: work
""",
    )
    communicate = subprocess.Popen.communicate

    def fail_after_communicate(
        process: subprocess.Popen[str], *args: object, **kwargs: object
    ) -> tuple[str, str]:
        communicate(process, *args, **kwargs)
        raise OSError("injected output collection failure")

    with (
        patch.object(subprocess.Popen, "communicate", fail_after_communicate),
        pytest.raises(OSError, match="output collection failure"),
    ):
        start_after_init(service, "task", "T", agent="codex")

    assert (tmp_path / "effect.txt").exists()
    state = service.tasks.read_execution_state("T", "01-task")
    assert state is not None
    assert state.item_executions[0].commands[0].status == "in_progress"
    recovered = service.next("T")
    assert recovered.status == "interrupted"
    assert recovered.operation_id == state.item_executions[0].operation_id


HANDOFF_SETTINGS = """workflows:
  - name: choose
    steps:
      - name: select
        kind: prompt
        variables:
          - name: workflow
            description: Target workflow.
        hooks:
          after_complete:
            - handoff_to: "{{workflow}}"
  - name: plain
    steps:
      - work: Work.
  - name: declared
    runtime: auto
    model: big-model
    reasoning: high
    steps:
      - work: Work.
  - name: model-only
    model: big-model
    steps:
      - work: Work.
  - name: same-model
    model: small-model
    steps:
      - work: Work.
"""


def _handoff_state(tmp_path: Path, target: str, task_id: str):
    (tmp_path / "ww.yaml").write_text(HANDOFF_SETTINGS, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    start_after_init(
        service,
        "choose",
        task_id,
        agent="codex",
        workflow_runtime="single",
        model="small-model",
        reasoning="medium",
    )
    service.next(task_id)
    service.complete(
        task_id, (("workflow", target),), "# selection\n", summary_for_next="Done."
    )
    run_id = service.tasks.active_execution_run(task_id)
    assert run_id is not None
    state = service.tasks.read_execution_state(task_id, run_id)
    assert state is not None
    return state


def test_handoff_target_without_settings_keeps_the_source_run_values(
    tmp_path: Path,
) -> None:
    state = _handoff_state(tmp_path, "plain", "TASK-H1")

    assert (state.workflow_runtime, state.model, state.reasoning) == (
        "single",
        "small-model",
        "medium",
    )


def test_handoff_target_declared_settings_apply_to_its_run(tmp_path: Path) -> None:
    state = _handoff_state(tmp_path, "declared", "TASK-H2")

    assert (state.workflow_runtime, state.model, state.reasoning) == (
        "auto",
        "big-model",
        "high",
    )


def test_handoff_target_model_without_reasoning_resets_reasoning(
    tmp_path: Path,
) -> None:
    state = _handoff_state(tmp_path, "model-only", "TASK-H3")

    assert (state.model, state.reasoning) == ("big-model", "auto")


def test_handoff_target_repeating_the_model_keeps_reasoning(tmp_path: Path) -> None:
    state = _handoff_state(tmp_path, "same-model", "TASK-H4")

    assert (state.model, state.reasoning) == ("small-model", "medium")
