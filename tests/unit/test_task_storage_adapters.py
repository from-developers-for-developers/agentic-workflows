# SPDX-License-Identifier: GPL-3.0-or-later
"""Behavioral contract shared by all task storage adapters."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from ww.actions import PlannedAction, Prompt
from ww.amendments import Amendment
from ww.direct_work import DirectCommit, DirectWork
from ww.errors import StateError
from ww.execution_models import (
    PLAN_SCHEMA_VERSION,
    CommandExecution,
    PlanSnapshot,
    TaskRunAggregate,
    initial_state,
)
from ww.plan import PlanItem, WorkflowPlan
from ww.storage_adapters import (
    ArtifactAddress,
    CommandOutputAddress,
    FileTaskStorageAdapter,
    MemoryTaskStorageAdapter,
    TaskMetadata,
    TaskStorageAdapter,
)


@pytest.fixture(params=("memory", "filesystem"))
def adapter(request: pytest.FixtureRequest, tmp_path: Path) -> TaskStorageAdapter:
    factories: dict[str, Callable[[], TaskStorageAdapter]] = {
        "memory": MemoryTaskStorageAdapter,
        "filesystem": lambda: FileTaskStorageAdapter(tmp_path),
    }
    return factories[request.param]()


def test_public_storage_adapter_requires_every_executor_write_capability() -> None:
    assert TaskStorageAdapter.__abstractmethods__ == {
        "commit_task_aggregate",
        "read_task_record",
        "task_written_at",
        "write_execution_artifact",
        "read_execution_artifact",
        "write_command_output",
        "read_command_output",
        "read_task_metadata",
        "read_shared_items",
        "write_shared_items",
        "read_amendments",
        "append_amendment",
        "read_direct_work",
        "append_direct_work",
        "write_task_metadata",
        "remove_task",
        "task_ids",
    }


def test_amendments_append_in_order_and_go_with_the_task(
    adapter: TaskStorageAdapter,
) -> None:
    first = Amendment("2026-10-05T10:00:00+00:00", "manager", "Also cover the CLI.")
    second = Amendment("2026-10-05T11:00:00+00:00", "operator", "Skip the docs.")

    assert adapter.read_amendments("PROJ-1") == ()
    adapter.append_amendment("PROJ-1", first)
    adapter.append_amendment("PROJ-1", second)
    assert adapter.read_amendments("PROJ-1") == (first, second)
    assert adapter.read_amendments("PROJ-2") == ()
    adapter.remove_task("PROJ-1")
    assert adapter.read_amendments("PROJ-1") == ()


def test_direct_work_appends_in_order_and_creates_the_task(
    adapter: TaskStorageAdapter,
) -> None:
    first = DirectWork(
        "2026-10-05T10:00:00+00:00",
        "Fixed a typo.",
        "agent",
        (DirectCommit("a" * 40, "Fix a typo", "2026-10-05T09:59:00+00:00"),),
    )
    second = DirectWork("2026-10-05T11:00:00+00:00", "Renamed it.", "reconciled")

    assert adapter.read_direct_work("PROJ-1") == ()
    assert adapter.task_exists("PROJ-1") is False
    adapter.append_direct_work("PROJ-1", first)
    adapter.append_direct_work("PROJ-1", second)
    assert adapter.read_direct_work("PROJ-1") == (first, second)
    assert adapter.read_direct_work("PROJ-2") == ()
    # A task with only direct work is a task, with no run.
    assert adapter.task_exists("PROJ-1") is True
    assert adapter.task_ids() == ("PROJ-1",)
    assert adapter.read_task_record("PROJ-1")[0] == ()
    assert adapter.remove_task("PROJ-1") is True
    assert adapter.read_direct_work("PROJ-1") == ()
    assert adapter.task_exists("PROJ-1") is False


def test_the_filesystem_keeps_direct_work_as_a_json_array(tmp_path: Path) -> None:
    adapter = FileTaskStorageAdapter(tmp_path)
    entry = DirectWork("2026-10-05T10:00:00+00:00", "Did it.", "agent")

    adapter.append_direct_work("PROJ-1", entry)

    path = tmp_path / ".ww/tasks/PROJ-1/direct-work.json"
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {
            "recorded_at": "2026-10-05T10:00:00+00:00",
            "summary": "Did it.",
            "source": "agent",
            "commits": [],
        }
    ]
    path.write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(StateError, match="cannot read direct work of 'PROJ-1'"):
        adapter.read_direct_work("PROJ-1")


def test_task_ids_lists_top_level_tasks_without_requests(
    adapter: TaskStorageAdapter,
) -> None:
    assert adapter.task_ids() == ()
    adapter.write_task_metadata(TaskMetadata("PROJ-2"))
    adapter.write_task_metadata(TaskMetadata("PROJ-1"))
    adapter.write_task_metadata(TaskMetadata("PROJ-1/child"))

    assert adapter.task_ids() == ("PROJ-1", "PROJ-2")


def test_a_child_with_only_direct_work_is_a_child_task(
    adapter: TaskStorageAdapter,
) -> None:
    assert adapter.child_task_ids("PROJ-1") == ()

    adapter.append_direct_work("PROJ-1/child", DirectWork("t", "Did it.", "agent", ()))

    assert adapter.child_task_ids("PROJ-1") == ("PROJ-1/child",)


def test_metadata_round_trips_and_missing_record_is_none(
    adapter: TaskStorageAdapter,
) -> None:
    metadata = TaskMetadata("PROJ-1", (("github.owner", "openai"),))

    assert adapter.read_task_metadata("PROJ-1") is None
    adapter.write_task_metadata(metadata)
    assert adapter.read_task_metadata("PROJ-1") == metadata
    assert adapter.task_exists("PROJ-1") is True


def test_metadata_rejects_conflicting_leaf_and_object_paths() -> None:
    with pytest.raises(ValueError, match="conflicting task metadata key"):
        TaskMetadata("PROJ-1", (("jira", "value"), ("jira.issue_id", "PROJ-1")))


def test_execution_artifacts_have_stable_run_aware_references(
    adapter: TaskStorageAdapter,
) -> None:
    work = ArtifactAddress("PROJ-1", "task", "work", 1, "work", "step", "01-task")
    first = adapter.write_execution_artifact(work, "first\n")
    second = adapter.write_execution_artifact(work, "changed\n")
    next_run = adapter.write_execution_artifact(
        replace(work, run_id="02-task"), "next\n"
    )

    assert first == second
    assert first != next_run
    assert adapter.read_execution_artifact(first) == "changed\n"
    assert adapter.read_execution_artifact(next_run) == "next\n"


def test_command_outputs_are_namespaced_by_operation_and_attempt(
    adapter: TaskStorageAdapter,
) -> None:
    first_attempt = CommandOutputAddress(
        "PROJ-1", "01-task", "task:work", "operation-1", 1, 1, "stdout"
    )
    reference = adapter.write_command_output(first_attempt, "first output\n")
    retry = adapter.write_command_output(
        replace(first_attempt, attempt=2), "retried output\n"
    )
    later_operation = adapter.write_command_output(
        replace(first_attempt, operation_id="operation-2"), "later output\n"
    )

    assert len({reference, retry, later_operation}) == 3
    assert adapter.read_command_output(reference) == "first output\n"
    assert adapter.read_command_output(retry) == "retried output\n"
    assert adapter.read_command_output(later_operation) == "later output\n"


def _aggregate(task_id: str = "PROJ-1") -> TaskRunAggregate:
    item = PlanItem(
        id="task:work",
        position=1,
        name="work",
        description="Work",
        operation=PlannedAction("prompt", Prompt("Work")),
        owner="agent",
        execution="agent_instruction",
        requires_agent_input=False,
        workflow="task",
        step="work",
        parent=None,
        phase="step",
        source="step",
        registered_handler=None,
    )
    plan = WorkflowPlan(
        workflow="task",
        workflow_description="",
        agent="codex",
        task_id=task_id,
        modes=(),
        handoff=False,
        items=(item,),
    )
    snapshot = PlanSnapshot(
        schema_version=PLAN_SCHEMA_VERSION,
        compiler_version="plan-v1",
        configuration_digest="configuration",
        compiled_at="2026-01-01T00:00:00Z",
        plan=plan,
    )
    state = initial_state(snapshot, (), "2026-01-01T00:00:00Z", run_id="01-task")
    return TaskRunAggregate(
        run_id="01-task",
        workflow="task",
        snapshot=snapshot,
        state=state,
    )


def test_aggregate_contract_is_authoritative_and_compare_and_swap(
    adapter: TaskStorageAdapter,
) -> None:
    aggregate = _aggregate()

    assert adapter.task_aggregate_revision("PROJ-1") == 0
    with adapter.lock_task("PROJ-1"):
        assert (
            adapter.commit_task_aggregate("PROJ-1", (aggregate,), expected_revision=0)
            == 1
        )
    runs, handoff = adapter.read_task_aggregate("PROJ-1")
    assert handoff is None
    assert runs == (aggregate,)
    assert adapter.task_aggregate_revision("PROJ-1") == 1
    assert adapter.execution_runs("PROJ-1")[0].run_id == "01-task"
    assert adapter.active_execution_run("PROJ-1") == "01-task"
    assert adapter.read_execution_state("PROJ-1", "01-task") == aggregate.state
    assert adapter.read_plan_snapshot("PROJ-1", "01-task") == aggregate.snapshot
    assert adapter.read_items("PROJ-1", "missing") == ()
    assert adapter.read_children("PROJ-1", "missing") == ()

    with (
        adapter.lock_task("PROJ-1"),
        pytest.raises(StateError, match="revision conflict"),
    ):
        adapter.commit_task_aggregate("PROJ-1", (aggregate,), expected_revision=0)


@pytest.mark.parametrize(
    ("corrupt", "message"),
    (
        (
            lambda run: replace(run, state=replace(run.state, plan_revision=2)),
            "plan revision",
        ),
        (
            lambda run: replace(run, state=replace(run.state, snapshot_digest="other")),
            "plan identity",
        ),
        (
            lambda run: replace(run, state=replace(run.state, cursor=2)),
            "cursor",
        ),
        (
            lambda run: replace(
                run,
                state=replace(
                    run.state,
                    item_executions=(
                        replace(run.state.item_executions[0], plan_item_id="other"),
                    ),
                ),
            ),
            "item identity",
        ),
        (
            lambda run: replace(
                run,
                state=replace(
                    run.state,
                    item_executions=(
                        replace(run.state.item_executions[0], position=2),
                    ),
                ),
            ),
            "item identity",
        ),
        (
            lambda run: replace(
                run,
                state=replace(
                    run.state,
                    item_executions=(
                        replace(
                            run.state.item_executions[0],
                            commands=(CommandExecution(index=1),),
                        ),
                    ),
                ),
            ),
            "commands",
        ),
    ),
)
def test_aggregate_rejects_plan_state_identity_mismatches(
    adapter: TaskStorageAdapter,
    corrupt: Callable[[TaskRunAggregate], TaskRunAggregate],
    message: str,
) -> None:
    with (
        adapter.lock_task("PROJ-1"),
        pytest.raises(StateError, match=message),
    ):
        adapter.commit_task_aggregate(
            "PROJ-1", (corrupt(_aggregate()),), expected_revision=0
        )


def test_aggregate_reset_removes_all_run_scoped_records(
    adapter: TaskStorageAdapter,
) -> None:
    aggregate = _aggregate("PROJ-1")
    adapter.write_task_metadata(TaskMetadata("PROJ-1", (("github.repo", "ww"),)))
    artifact = adapter.write_execution_artifact(
        ArtifactAddress("PROJ-1", "task", "work", 1, "work", "step", "01-task"),
        "result\n",
    )
    kept = adapter.write_execution_artifact(
        ArtifactAddress("PROJ-2", "task", "work", 1, "work", "step", "01-task"),
        "kept\n",
    )
    adapter.write_task_metadata(TaskMetadata("PROJ-2"))
    with adapter.lock_task("PROJ-1"):
        adapter.commit_task_aggregate("PROJ-1", (aggregate,), expected_revision=0)

    assert adapter.remove_task("PROJ-1") is True
    assert adapter.read_task_aggregate("PROJ-1") == ((), None)
    assert adapter.read_task_metadata("PROJ-1") is None
    assert adapter.task_aggregate_revision("PROJ-1") == 0
    assert adapter.remove_task("PROJ-1") is False
    with pytest.raises(StateError, match="execution artifact"):
        adapter.read_execution_artifact(artifact)
    assert adapter.read_execution_artifact(kept) == "kept\n"
    assert adapter.read_task_metadata("PROJ-2") == TaskMetadata("PROJ-2")


def test_corrupt_task_metadata_raises_state_error(tmp_path: Path) -> None:
    adapter = FileTaskStorageAdapter(tmp_path)
    task_dir = tmp_path / ".ww" / "tasks" / "PROJ-1"
    task_dir.mkdir(parents=True)
    metadata_file = task_dir / "metadata.json"
    (task_dir / "task.json").write_text(
        json.dumps({"task_id": "PROJ-1", "metadata": {"existing": "kept"}}),
        encoding="utf-8",
    )

    metadata_file.write_text("not json", encoding="utf-8")
    with pytest.raises(StateError, match="invalid task metadata"):
        adapter.read_task_metadata("PROJ-1")

    metadata_file.write_text(json.dumps({"task_id": "WRONG"}), encoding="utf-8")
    with pytest.raises(StateError, match="mismatched task ID"):
        adapter.read_task_metadata("PROJ-1")

    metadata_file.write_text(
        json.dumps({"task_id": "PROJ-1", "metadata": "bad"}), encoding="utf-8"
    )
    with pytest.raises(StateError, match="metadata value must be a mapping"):
        adapter.read_task_metadata("PROJ-1")

    metadata_file.write_text(
        json.dumps({"task_id": "PROJ-1", "metadata": {"github": {}}}),
        encoding="utf-8",
    )
    with pytest.raises(StateError, match="mappings cannot be empty"):
        adapter.read_task_metadata("PROJ-1")

    metadata_file.write_text(
        json.dumps({"task_id": "PROJ-1", "metadata": {"github": {"owner": 123}}}),
        encoding="utf-8",
    )
    with pytest.raises(StateError, match="field values must be strings"):
        adapter.read_task_metadata("PROJ-1")
