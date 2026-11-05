# SPDX-License-Identifier: GPL-3.0-or-later
"""A plan construct ww removed refuses only the runs whose plan holds it."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ww.cli import main
from ww.migrations import removals
from ww.migrations.removals import (
    RemovedConstruct,
    RemovedConstructError,
    plan_items,
    removed_in_plan,
)
from ww.open_work import runs_refused_by, unreadable_tasks
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """workflows:
  - name: task
    steps:
      - work: Do it.
  - name: legacy
    steps:
      - old-ways: Do it the old way.
"""


def _old_ways(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    """A test-only detector: the item of a step named ``old-ways``."""
    return tuple(
        f"{path} ({item['name']})"
        for path, item in plan_items(snapshot)
        if item.get("name") == "old-ways"
    )


# A test-only removal: ww has removed no plan construct yet.
OLD_WAYS = RemovedConstruct(
    id="old-ways",
    what="the `old-ways` step",
    deprecated_in="1.2.0",
    removed_in="2.0.0",
    last_version="1.9.3",
    detect=_old_ways,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two tasks in progress, one of them on the plan with the construct."""
    monkeypatch.setattr(removals, "REMOVED_CONSTRUCTS", (OLD_WAYS,))
    monkeypatch.setattr(removals, "__version__", "1.9.3")
    root = tmp_path / "project"
    root.mkdir()
    (root / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    service = WorkflowService(Storage(root))
    for workflow, task_id in (("task", "T-1"), ("legacy", "L-1")):
        service.start(
            workflow,
            task_id,
            agent="codex",
            workflow_runtime="single",
            init_artifact="Do.",
        )
    return root


def test_only_the_run_holding_a_removed_construct_is_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(removals, "__version__", "2.0.0")
    service = WorkflowService(Storage(project))

    with pytest.raises(RemovedConstructError) as refused:
        service.load("L-1")

    run_id = refused.value.run_id
    assert refused.value.task_id == "L-1" and refused.value.open
    assert str(refused.value) == (
        f"task 'L-1': its open run {run_id!r} uses the `old-ways` step "
        "(plan.items[1] (old-ways), template_plan.items[1] (old-ways)), removed "
        "in ww 2.0.0, which this ww no longer runs: finish it with ww 1.9.3 "
        "(`pipx install --force ww-agentic-workflows==1.9.3`), or discard the "
        "task with `./ww reset L-1`. Other tasks are unaffected."
    )
    # The other task carries on, and the refused one is named, not hidden.
    assert service.load("T-1")[1].plan.workflow == "task"
    assert service.next("T-1").item_name == "work"
    assert [task.task_id for task in unreadable_tasks(service.tasks)] == ["L-1"]
    # A command addressing the run says why it is refused.
    assert main(["--root", str(project), "instruction", "L-1"]) == 1
    error = capsys.readouterr().err
    assert "uses the `old-ways` step" in error
    assert "invalid task state" not in error


def test_a_removal_scheduled_for_later_still_runs(project: Path) -> None:
    service = WorkflowService(Storage(project))

    assert service.next("L-1").item_name == "old-ways"


def test_the_open_runs_a_target_version_would_refuse_are_listed(
    project: Path,
) -> None:
    tasks = Storage(project).task_persistence

    [refused] = runs_refused_by(tasks, "2.0.0")
    assert (refused.task_id, refused.open) == ("L-1", True)
    assert refused.render().startswith(f"L-1 (open run {refused.run_id}): ")
    assert "the `old-ways` step" in refused.render()
    assert runs_refused_by(tasks, "1.10.0") == ()


def test_a_run_this_ww_refuses_already_is_listed_for_the_target(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(removals, "__version__", "2.0.0")

    [refused] = runs_refused_by(Storage(project).task_persistence, "3.0.0")
    assert refused.task_id == "L-1"


def test_the_detector_reads_nested_verification_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(removals, "REMOVED_CONSTRUCTS", (OLD_WAYS,))
    snapshot = {
        "plan": {
            "items": [
                {
                    "name": "work",
                    "verification_plan": {
                        "verifier": [{"name": "old-ways"}],
                        "on_failure": [],
                    },
                }
            ]
        }
    }

    [found] = removed_in_plan(snapshot, "2.0.0")
    assert found.construct is OLD_WAYS
    assert found.locations == (
        "plan.items[0].verification_plan.verifier[0] (old-ways)",
    )
    assert removed_in_plan(snapshot, "1.9.3") == ()


def test_a_removal_names_a_last_version_before_it() -> None:
    with pytest.raises(ValueError, match="last_version must precede removed_in"):
        RemovedConstruct(
            id="x",
            what="x",
            deprecated_in="1.0.0",
            removed_in="2.0.0",
            last_version="2.0.0",
            detect=_old_ways,
        )
