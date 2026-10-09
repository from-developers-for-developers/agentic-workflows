# SPDX-License-Identifier: GPL-3.0-or-later
"""Unknown fields outside plan items are left alone; inside them they fail."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import run_state_path
from ww.errors import StateError
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """workflows:
  - name: task
    steps:
      - work: Work.
      - check: Check.
"""


def _started(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    WorkflowService(Storage(tmp_path)).start(
        "task", "TASK-1", agent="codex", init_artifact="Do it."
    )
    return WorkflowService(Storage(tmp_path))


def test_a_plan_item_with_a_field_ww_does_not_know_fails_loudly(
    tmp_path: Path,
) -> None:
    _started(tmp_path)
    path = run_state_path(tmp_path, "TASK-1")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["run"]["snapshot"]["plan"]["items"][0]["extra_field"] = "stale"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(StateError, match="unknown key.*extra_field"):
        WorkflowService(Storage(tmp_path)).instruction("TASK-1")


def test_unknown_fields_elsewhere_in_the_state_are_left_alone(
    tmp_path: Path,
) -> None:
    _started(tmp_path)
    for path in (
        tmp_path / ".ww/tasks/TASK-1/state.json",
        run_state_path(tmp_path, "TASK-1"),
    ):
        document = json.loads(path.read_text(encoding="utf-8"))
        document["written_by"] = "a future ww"
        path.write_text(json.dumps(document), encoding="utf-8")
    metadata = tmp_path / ".ww/tasks/TASK-1/metadata.json"
    values = json.loads(metadata.read_text(encoding="utf-8"))
    values["note"] = "unknown"
    metadata.write_text(json.dumps(values), encoding="utf-8")

    assert WorkflowService(Storage(tmp_path)).instruction("TASK-1").workflow == "task"


def test_a_digest_another_version_computed_is_re_derived(
    tmp_path: Path,
) -> None:
    # A default only the other version filled in changes the digest without
    # leaving anything in the stored, compacted plan.
    _started(tmp_path)
    path = run_state_path(tmp_path, "TASK-1")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["run"]["state"]["plan_digest"] = "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")

    service = WorkflowService(Storage(tmp_path))
    service.next("TASK-1")
    service.complete("TASK-1", artifact="Done.", summary_for_next="Done.")
    assert WorkflowService(Storage(tmp_path)).instruction("TASK-1").item_name == "check"
