# SPDX-License-Identifier: GPL-3.0-or-later
"""An automatic report handler saves its reply ID and reports its item."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.execution_models import ExecutionState, PlanSnapshot
from ww.items import WorkItem
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"

# A project-owned fake remote: a ledger of replies in the working directory.
# Given an existing reply ID it updates that reply, never creating another.
REPLY_SCRIPT = """import json, os, sys
comment, text, reply_id = sys.argv[1:4]
if os.environ.get("FAKE_REMOTE_DOWN") or os.path.exists("down"):
    sys.exit(7)
ledger = json.load(open("ledger.json")) if os.path.exists("ledger.json") else {}
if not reply_id:
    reply_id = "r%d" % (len(ledger) + 1)
ledger[reply_id] = {"comment": comment, "text": text}
json.dump(ledger, open("ledger.json", "w"))
if os.path.exists("silent"):
    reply_id = ""
print(reply_id)
"""

WORKFLOW = """workflows:
  - name: review
    steps:
      - collect: Record one item per comment.
        items:
          steps: []
      - answer: Answer every comment.
        items:
          steps:
            - reply: ~
              item_phase: report
              argv:
                - python3
                - reply.py
                - "{{ww.item.field.comment_id}}"
                - "{{ww.item.actual_solution}}"
                - "{{ww.item.field.reply_id}}"
              saves:
                - item.field.reply_id: The confirmed reply ID printed by the command.
      - finish: Finish.
"""


def _service(root: Path, workflow: str = WORKFLOW) -> WorkflowService:
    (root / "ww.yaml").write_text(workflow, encoding="utf-8")
    (root / "reply.py").write_text(REPLY_SCRIPT, encoding="utf-8")
    service = WorkflowService(Storage(root))
    start_after_init(service, "review", TASK, agent="codex", workflow_runtime="single")
    service.next(TASK)
    for number in (1, 2):
        service.add_item(
            TASK,
            WorkItem(
                f"c{number}",
                f"Comment {number}",
                actual_solution=f"fix {number}",
                resolved=True,
                fields=(("comment_id", str(100 + number)),),
            ),
        )
    service.complete(TASK, artifact="collected", summary_for_next="Done.")
    service.next(TASK)  # the reused collection step of the answer pass
    return service


def _answer(service: WorkflowService) -> str:
    """Finish the pass's collection step; the replies run right after it."""
    return service.complete(
        TASK, artifact="started", summary_for_next="Done."
    ).item_name


def _items(service: WorkflowService) -> dict[str, WorkItem]:
    run = service.load(TASK)[0].run_id
    return {item.id: item for item in service.tasks.read_items(TASK, run)}


def _ledger(root: Path) -> dict[str, dict[str, str]]:
    return json.loads((root / "ledger.json").read_text())


def test_success_saves_the_reply_id_and_reports_each_item(tmp_path: Path) -> None:
    service = _service(tmp_path)

    assert _answer(service) == "finish"
    items = _items(service)
    assert items["c1"].field("reply_id") == "r1"
    assert items["c2"].field("reply_id") == "r2"
    assert items["c1"].reported and items["c2"].reported
    assert _ledger(tmp_path) == {
        "r1": {"comment": "101", "text": "fix 1"},
        "r2": {"comment": "102", "text": "fix 2"},
    }
    assert service.load(TASK)[0].status != "failed"


def test_nonzero_exit_reports_nothing_and_retry_creates_no_duplicate(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    (tmp_path / "down").write_text("")
    _answer(service)
    state, _ = service.load(TASK)
    assert state.status == "failed"
    assert not any(item.reported for item in _items(service).values())

    (tmp_path / "down").unlink()
    service.next(TASK, retry=True)

    items = _items(service)
    assert [items[i].field("reply_id") for i in ("c1", "c2")] == ["r1", "r2"]
    assert all(item.reported for item in items.values())
    assert len(_ledger(tmp_path)) == 2


def test_an_empty_reply_id_fails_without_reporting(tmp_path: Path) -> None:
    service = _service(tmp_path)
    (tmp_path / "silent").write_text("")

    _answer(service)

    state, _ = service.load(TASK)
    assert state.status == "failed"
    assert state.last_error is not None
    assert "item.field.reply_id" in state.last_error
    items = _items(service)
    assert not items["c1"].reported
    assert items["c1"].field("reply_id") is None


def test_a_saved_reply_id_is_reused_by_the_handler(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.update_item(TASK, "c1", fields={"comment_id": "101", "reply_id": "r9"})

    _answer(service)

    assert _items(service)["c1"].field("reply_id") == "r9"
    assert set(_ledger(tmp_path)) == {"r9", "r2"}


def test_interrupted_publication_resumes_without_replaying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = WORKFLOW.replace(
        "              saves:\n",
        "              saves:\n                - metadata.last_reply: Last reply.\n",
    )
    service = _service(tmp_path, workflow)
    reconcile = service.metadata_publisher.reconcile

    def interrupted(
        state: ExecutionState, snapshot: PlanSnapshot
    ) -> tuple[ExecutionState, PlanSnapshot]:
        if state.pending_task_metadata:
            raise RuntimeError("publication interrupted")
        return reconcile(state, snapshot)

    monkeypatch.setattr(service.metadata_publisher, "reconcile", interrupted)
    with pytest.raises(RuntimeError, match="publication interrupted"):
        _answer(service)

    # The completion is durable: the item carries its reply and is reported,
    # while the metadata is still only an intent.
    assert service.load(TASK)[0].pending_task_metadata
    assert _items(service)["c1"].field("reply_id") == "r1"
    assert _items(service)["c1"].reported
    assert not _items(service)["c2"].reported

    monkeypatch.setattr(service.metadata_publisher, "reconcile", reconcile)
    resumed = WorkflowService(Storage(tmp_path))
    assert resumed.next(TASK, caller_role="manager").item_name == "finish"

    assert set(_ledger(tmp_path)) == {"r1", "r2"}
    assert all(item.reported for item in _items(resumed).values())


def test_an_automatic_collection_save_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text("""workflows:
  - name: review
    steps:
      - collect: ~
        argv: [echo, one]
        saves:
          - item.field.reply_id: One reply.
        items:
          steps: []
""")

    with pytest.raises(ConfigurationError, match="one output cannot be distributed"):
        load_configuration(path)


HOOKED = WORKFLOW.replace(
    "              saves:\n",
    "              hooks:\n"
    "                before_complete:\n"
    "                  - argv: [python3, check.py]\n"
    "              saves:\n",
)


def _hooked(root: Path) -> WorkflowService:
    (root / "check.py").write_text(
        "import os, sys\nsys.exit(1 if os.path.exists('hookfail') else 0)\n"
    )
    return _service(root, HOOKED)


def test_a_failing_completion_hook_leaves_the_item_unreported(
    tmp_path: Path,
) -> None:
    service = _hooked(tmp_path)
    (tmp_path / "hookfail").write_text("")

    _answer(service)

    state, _ = service.load(TASK)
    assert state.status == "failed"
    item = _items(service)["c1"]
    assert item.field("reply_id") == "r1"
    assert not item.reported

    (tmp_path / "hookfail").unlink()
    service.next(TASK, retry=True)

    assert all(entry.reported for entry in _items(service).values())
    assert len(_ledger(tmp_path)) == 2


def test_a_passing_completion_hook_reports_each_item_once(tmp_path: Path) -> None:
    service = _hooked(tmp_path)

    assert _answer(service) == "finish"

    assert all(entry.reported for entry in _items(service).values())
    assert len(_ledger(tmp_path)) == 2


def test_a_failed_assertion_saves_and_reports_nothing(tmp_path: Path) -> None:
    workflow = WORKFLOW.replace(
        "              saves:\n",
        "              assert:\n"
        "                - equals: never\n"
        "              saves:\n",
    )
    service = _service(tmp_path, workflow)

    _answer(service)

    state, _ = service.load(TASK)
    assert state.status == "failed"
    item = _items(service)["c1"]
    assert item.field("reply_id") is None
    assert not item.reported
