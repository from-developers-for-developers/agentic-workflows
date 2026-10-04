# SPDX-License-Identifier: GPL-3.0-or-later
"""Item values reach automatic commands and instructions from one mapping."""

from __future__ import annotations

import json
from pathlib import Path

from tests.workflow_helpers import start_after_init
from ww.items import WorkItem
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"

# A project-owned script stand-in: records its argv as JSON, one file per item.
DUMP = (
    "import json, sys; "
    "open('out-' + sys.argv[1] + '.json', 'a').write(json.dumps(sys.argv[1:]) + '\\n')"
)

WORKFLOW = f"""workflows:
  - name: review
    steps:
      - collect: Record one item per comment.
        items:
          steps: []
      - finish: Reuse the collected items.
        items:
          steps:
            - reply: ~
              item_phase: report
              argv:
                - python3
                - -c
                - {json.dumps(DUMP)}
                - "{{{{ww.item.id}}}}"
                - "{{{{ww.item.actual_solution}}}}"
                - "{{{{ww.item.field.comment_id}}}}"
                - "{{{{ww.item.field.reply_id}}}}"
                - "{{{{ww.item.resolved}}}}"
                - "{{{{ww.item.reported}}}}"
                - "{{{{ww.item.reference_to_id}}}}"
                - "{{{{ww.item.proposed_solution}}}}"
                - "{{{{ww.item.processed_item}}}}"
                - "{{{{ww.item.text}}}}"
            - note: Note {{{{ww.item.id}}}} resolved={{{{ww.item.resolved}}}}.
"""

NASTY = "say \"hi\"\nline two $HOME `uname` 'q' é中 \\ {{ww.item.id}}"


def _service(root: Path, workflow: str = WORKFLOW) -> WorkflowService:
    (root / "ww.yaml").write_text(workflow, encoding="utf-8")
    service = WorkflowService(Storage(root))
    start_after_init(service, "review", TASK, agent="codex", workflow_runtime="single")
    return service


def _collected(root: Path, workflow: str = WORKFLOW) -> WorkflowService:
    service = _service(root, workflow)
    service.next(TASK)
    service.add_item(TASK, WorkItem("c0", "Original"))
    service.add_item(
        TASK,
        WorkItem(
            "c1",
            "First",
            processed_item="p1",
            proposed_solution="plan 1",
            actual_solution=NASTY,
            resolved=True,
            reference_to_id="c0",
            fields=(("comment_id", "100"),),
        ),
    )
    service.add_item(TASK, WorkItem("c2", "Second", actual_solution="plain", fields=()))
    service.complete(TASK, artifact="collected", summary_for_next="Done.")
    return service


def _note(service: WorkflowService, item_id: str) -> str:
    """Advance to the agent note for ``item_id``; the replies before it ran."""
    while True:
        page = service.next(TASK, caller_role="manager")
        if page.item_name == "note" and f"Note {item_id} " in page.action_text:
            return page.action_text
        service.complete(TASK, artifact="done", summary_for_next="Done.")


def _argv(root: Path, item_id: str) -> list[list[str]]:
    path = root / f"out-{item_id}.json"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_automatic_argv_gets_each_items_values_as_data(tmp_path: Path) -> None:
    service = _collected(tmp_path)

    _note(service, "c2")

    assert _argv(tmp_path, "c1") == [
        [
            "c1",
            NASTY,
            "100",
            "",
            "true",
            "false",
            "c0",
            "plan 1",
            "p1",
            "First",
        ]
    ]
    assert _argv(tmp_path, "c2") == [
        ["c2", "plain", "", "", "false", "false", "", "", "", "Second"]
    ]


def test_instructions_render_the_same_mapping(tmp_path: Path) -> None:
    service = _collected(tmp_path)

    assert "Note c1 resolved=true." in _note(service, "c1")
    assert "Note c2 resolved=false." in _note(service, "c2")


def test_values_follow_updates_made_before_the_command_runs(tmp_path: Path) -> None:
    service = _collected(tmp_path)
    _note(service, "c1")
    service.update_item(TASK, "c2", actual_solution="revised", resolved=True)

    assert "Note c2 resolved=true." in _note(service, "c2")

    assert _argv(tmp_path, "c2")[0][1] == "revised"
    assert _argv(tmp_path, "c2")[0][4] == "true"


def test_an_item_value_without_a_bound_item_is_a_context_error(
    tmp_path: Path,
) -> None:
    workflow = """handlers:
  - say: ~
    argv: [echo, "{{ww.item.actual_solution}}"]
workflows:
  - name: review
    steps:
      - say: ~
      - done: Done.
"""
    service = _service(tmp_path, workflow)

    service.next(TASK, caller_role="manager")

    state, _ = service.load(TASK)
    assert state.status == "failed"
    assert state.last_error is not None
    assert "no work item is bound" in state.last_error
    assert "ww.item.actual_solution" in state.last_error
    assert "missing variable" not in state.last_error


TWO_PASSES = f"""workflows:
  - name: review
    steps:
      - collect: Record one item per comment.
        items:
          steps: []
      - analyze-all: Analyze the comments.
        items:
          steps:
            - analyze: Analyze this comment.
              item_phase: analyze
      - finish: Reuse the collected items.
        items:
          steps:
            - reply: ~
              argv:
                - python3
                - -c
                - {json.dumps(DUMP)}
                - "{{{{ww.item.id}}}}"
                - "{{{{ww.item.processed_item}}}}"
            - settle: Settle {{{{ww.item.id}}}}.
"""


def test_values_follow_a_pass_gate_stop_update_item_and_retry(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, TWO_PASSES)
    service.next(TASK)
    service.add_item(TASK, WorkItem("c1", "First"))
    service.add_item(TASK, WorkItem("c2", "Second"))
    service.complete(TASK, artifact="collected", summary_for_next="Done.")
    service.next(TASK)  # analyze-all, reusing the collection
    service.complete(TASK, artifact="started", summary_for_next="Done.")
    service.next(TASK)  # analyze c1
    service.update_item(TASK, "c1", processed_item="analysis one")
    service.complete(TASK, artifact="analyzed", summary_for_next="Done.")
    service.next(TASK)  # analyze c2, left unrecorded
    service.complete(TASK, artifact="analyzed", summary_for_next="Done.")
    assert service.load(TASK)[0].status == "failed"

    service.update_item(TASK, "c2", processed_item="analysis two")
    assert service.next(TASK, retry=True).item_name == "finish"
    service.next(TASK)
    service.complete(TASK, artifact="started", summary_for_next="Done.")
    while service.next(TASK, caller_role="manager").item_name == "settle":
        service.complete(TASK, artifact="settled", summary_for_next="Done.")

    assert _argv(tmp_path, "c1") == [["c1", "analysis one"]]
    assert _argv(tmp_path, "c2") == [["c2", "analysis two"]]
