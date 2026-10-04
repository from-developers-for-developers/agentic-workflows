# SPDX-License-Identifier: GPL-3.0-or-later
"""A real run's expanded plan keeps its pass identity and loads from schema 1."""

from __future__ import annotations

import json
from pathlib import Path

from ww.execution_models import PlanSnapshot
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


def test_a_running_schema_1_snapshot_resumes_with_the_same_plan(
    tmp_path: Path,
) -> None:
    _, snapshot = _expanded(tmp_path).load(TASK)
    raw = json.loads(json.dumps(snapshot.to_dict()))
    raw["schema_version"] = 1
    for name in ("plan", "template_plan"):
        for item in raw[name]["items"]:
            item.pop("item_pass", None)
            item.pop("item_collect_only", None)

    legacy = PlanSnapshot.from_dict(raw)

    assert legacy.plan == snapshot.plan
    assert legacy.template_plan == snapshot.template_plan
    assert legacy.to_dict() == raw
