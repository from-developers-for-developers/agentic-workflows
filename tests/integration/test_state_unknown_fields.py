# SPDX-License-Identifier: GPL-3.0-or-later
"""Task state written by another ww version loads: unknown fields are left alone."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.plan import PlanItem
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """workflows:
  - name: task
    steps:
      - work: Work.
      - check: Check.
"""


def _started_by_an_other_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> WorkflowService:
    """Start a task while plan items serialize a field this ww does not read."""
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    original = PlanItem.to_dict

    def with_retired_field(item: PlanItem) -> dict[str, object]:
        return {**original(item), "retired_field": "set by another ww"}

    with monkeypatch.context() as patch:
        patch.setattr(PlanItem, "to_dict", with_retired_field)
        WorkflowService(Storage(tmp_path)).start(
            "task", "TASK-1", agent="codex", init_artifact="Do it."
        )
    return WorkflowService(Storage(tmp_path))


def test_a_plan_with_a_field_ww_does_not_know_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _started_by_an_other_version(tmp_path, monkeypatch)
    stored = (tmp_path / ".ww/tasks/TASK-1/state.json").read_text(encoding="utf-8")
    assert '"retired_field"' in stored

    assert service.status("TASK-1").workflow == "task"
    # It keeps working: advance, save, and load again.
    service.next("TASK-1")
    done = service.complete("TASK-1", artifact="Done.", summary_for_next="Done.")
    assert done.item_name == "check"
    assert WorkflowService(Storage(tmp_path)).status("TASK-1").item_name == "check"


def test_unknown_fields_elsewhere_in_the_state_are_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _started_by_an_other_version(tmp_path, monkeypatch)
    path = tmp_path / ".ww/tasks/TASK-1/state.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["written_by"] = "a future ww"
    path.write_text(json.dumps(document), encoding="utf-8")
    metadata = tmp_path / ".ww/tasks/TASK-1/metadata.json"
    values = json.loads(metadata.read_text(encoding="utf-8"))
    values["note"] = "unknown"
    metadata.write_text(json.dumps(values), encoding="utf-8")

    assert WorkflowService(Storage(tmp_path)).status("TASK-1").workflow == "task"


def test_a_digest_another_version_computed_is_re_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A default only the other version filled in changes the digest without
    # leaving anything in the stored, compacted plan.
    _started_by_an_other_version(tmp_path, monkeypatch)
    path = tmp_path / ".ww/tasks/TASK-1/state.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["runs"][0]["state"]["plan_digest"] = "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")

    service = WorkflowService(Storage(tmp_path))
    service.next("TASK-1")
    service.complete("TASK-1", artifact="Done.", summary_for_next="Done.")
    assert WorkflowService(Storage(tmp_path)).status("TASK-1").item_name == "check"
