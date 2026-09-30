# SPDX-License-Identifier: GPL-3.0-or-later
"""Preconditions checked before a child task starts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.errors import StateError
from ww.service import WorkflowService
from ww.storage import Storage

PARENT = "P"


def _parent(tmp_path: Path, *children: str) -> WorkflowService:
    """A parent waiting for its pending children to run."""
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - work: Do child work.
  - name: plain
    steps:
      - work: Work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "parent", PARENT, agent="codex")
    service.next(PARENT)
    for child in children:
        service.add_child(PARENT, child, f"Child {child}")
    if children:
        service.complete(PARENT, artifact="split", summary_for_next="Done.")
        service.next(PARENT)
    return service


def test_start_child_requires_the_coordinator_step(tmp_path: Path) -> None:
    service = _parent(tmp_path)

    with pytest.raises(StateError, match="parent workflow is not running its children"):
        service.start_child(PARENT, "c1")


def test_start_child_requires_a_known_pending_child(tmp_path: Path) -> None:
    service = _parent(tmp_path, "c1", "c2")

    with pytest.raises(StateError, match="child 'missing' was not found"):
        service.start_child(PARENT, "missing")

    service.start_child(PARENT, "c1")
    with pytest.raises(StateError, match="child 'c1' is not pending"):
        service.start_child(PARENT, "c1")
    with pytest.raises(StateError, match="another child is already in progress"):
        service.start_child(PARENT, "c2")


def test_start_child_rejects_a_record_without_a_start_operation(
    tmp_path: Path,
) -> None:
    service = _parent(tmp_path, "c1")
    state, snapshot = service.load(PARENT)
    children = service.tasks.read_children(PARENT, state.run_id)
    service.commit(
        state,
        snapshot,
        children=tuple(replace(child, start_operation_id=None) for child in children),
    )

    with pytest.raises(StateError, match="child 'c1' has no start operation"):
        service.start_child(PARENT, "c1")


def test_start_child_rejects_a_completed_parent(tmp_path: Path) -> None:
    service = _parent(tmp_path)
    start_after_init(service, "plain", "DONE", agent="codex")
    service.next("DONE")
    service.complete("DONE", artifact="worked", summary_for_next="Done.")
    service.next("DONE")
    service.complete("DONE", (("summary", "done"),), summary_for_next="Done.")

    with pytest.raises(StateError, match="parent workflow is already completed"):
        service.start_child("DONE", "c1")


@pytest.mark.parametrize(
    ("parent", "child", "message"),
    [
        ("bad id", "c1", "invalid task ID"),
        (PARENT, "a/b", "child ID must be a single normalized name"),
    ],
)
def test_start_child_validates_identities(
    tmp_path: Path, parent: str, child: str, message: str
) -> None:
    service = _parent(tmp_path)

    with pytest.raises(StateError, match=message):
        service.start_child(parent, child)
