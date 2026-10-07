# SPDX-License-Identifier: GPL-3.0-or-later
"""Debug records keep the events ww observed and the operator's notes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import configured_service
from ww.cli import main
from ww.errors import StateError
from ww.run_reports import DEBUG, render_listing, render_record
from ww.service import WorkflowService

WORKFLOW = """workflows:
  - name: task
    description: Implement a change.
    steps:
      - work: Do the work.
  - name: jira-task
    steps:
      - name: create-jira
        mcp: jira
        description: Create the Jira issue.
        variables:
          - name: task_id
      - name: develop
        description: Implement work for {{task_id}}.
"""
SUMMARY = (
    ("summary", "Done."),
    ("debug_errors", "[]"),
    ("debug_inconveniences", "[]"),
)


def _project(root: Path, settings: dict[str, object] | None = None) -> WorkflowService:
    (root / "ww.json").write_text(
        json.dumps({"debug": {"collect": True}} if settings is None else settings),
        encoding="utf-8",
    )
    return configured_service(root, WORKFLOW)


def _pending(root: Path, owner: str) -> list[dict]:
    path = root / ".ww" / "debug" / "pending" / f"{owner}.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def _record(root: Path, identifier: str) -> dict:
    return json.loads(
        (root / ".ww" / "debug" / f"{identifier}.json").read_text("utf-8")
    )


def _failed_work(service: WorkflowService, task_id: str = "TASK-1") -> None:
    service.start("task", task_id, agent="codex", init_artifact="Do it.")
    service.next(task_id)
    service.fail(task_id, "The build exploded.")


def _finish(service: WorkflowService, task_id: str = "TASK-1"):
    service.complete(task_id, artifact="Worked.", summary_for_next="Done.")
    return service.complete(task_id, variables=SUMMARY)


def _run(
    root: Path, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    code = main(["--root", str(root), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_fail_on_a_task_is_kept_and_reaches_the_run_record(tmp_path: Path) -> None:
    service = _project(tmp_path)
    _failed_work(service)

    events = _pending(tmp_path, "TASK-1")
    assert [event["source"] for event in events] == ["ww", "ww"]
    assert events[0]["summary"] == "Stopped for the operator (work_failed) at `work`"
    assert events[0]["detail"] == "agent item 'work' failed: The build exploded."
    assert events[1]["summary"] == "Worker reported `work` failed"
    assert events[1]["detail"] == "The build exploded."

    service.next("TASK-1")
    done = _finish(service)

    assert done.status == "completed"
    record = _record(tmp_path, "TASK-1--01-task")
    assert record["errors"] == [] and record["inconveniences"] == []
    assert [event["summary"] for event in record["events"]] == [
        "Stopped for the operator (work_failed) at `work`",
        "Worker reported `work` failed",
    ]
    assert not (tmp_path / ".ww" / "debug" / "pending" / "TASK-1.jsonl").exists()


def test_nothing_is_observed_unless_debug_is_collected(tmp_path: Path) -> None:
    service = _project(tmp_path, {})
    _failed_work(service)

    assert not (tmp_path / ".ww" / "debug").exists()


def test_a_stop_is_recorded_once_however_often_the_run_is_committed(
    tmp_path: Path,
) -> None:
    service = _project(tmp_path)
    _failed_work(service)
    service.instruction("TASK-1")
    service.instruction("TASK-1")
    state, snapshot = service.load("TASK-1")
    service.commit(state, snapshot)

    stops = [
        event
        for event in _pending(tmp_path, "TASK-1")
        if event["summary"].startswith("Stopped for the operator")
    ]
    assert len(stops) == 1


def test_next_force_records_what_the_operator_forced(tmp_path: Path) -> None:
    service = _project(tmp_path)
    _failed_work(service)

    service.next("TASK-1", force=True, force_reason="Operator resolved it by hand")

    (forced,) = [
        event
        for event in _pending(tmp_path, "TASK-1")
        if event["summary"].startswith("Operator forced next")
    ]
    assert forced == {
        "at": forced["at"],
        "source": "ww",
        "summary": (
            "Operator forced next: skip the failed item `work` without running it"
        ),
        "detail": "Operator resolved it by hand",
    }


def test_fail_on_a_request_reaches_the_record_of_the_task_it_binds(
    tmp_path: Path,
) -> None:
    service = _project(tmp_path)
    pending = service.start("jira-task", None, agent="codex")
    request_id = pending.task_id
    service.next(request_id)

    stop = service.fail(request_id, "Jira refused the issue.")

    assert stop.control == "awaiting_operator"
    (event,) = _pending(tmp_path, request_id)
    assert event["summary"] == "Worker reported the bootstrap step failed"
    assert event["detail"] == "Jira refused the issue."

    service.next(request_id, retry=True)
    bound = service.complete(
        request_id, (("task_id", "PROJ-123"),), "Created.", summary_for_next="Done."
    )
    assert bound.task_id == "PROJ-123"
    service.next("PROJ-123")
    done = _finish(service, "PROJ-123")

    assert done.status == "completed"
    record = _record(tmp_path, "PROJ-123--01-jira-task")
    assert [event["summary"] for event in record["events"]] == [
        "Worker reported the bootstrap step failed"
    ]
    assert not (tmp_path / ".ww" / "debug" / "pending" / f"{request_id}.jsonl").exists()


def test_the_operator_notes_a_task_without_a_completed_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _project(tmp_path)
    service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")

    code, out, _ = _run(
        tmp_path,
        capsys,
        "debug",
        "note",
        "TASK-1",
        "--summary",
        "The start page repeated itself",
        "--detail",
        "Twice.",
    )

    assert code == 0
    assert (
        "Note kept with the pending events of `TASK-1` and in the standalone "
        "record `TASK-1--notes` until its run completes." in out
    )
    (event,) = _pending(tmp_path, "TASK-1")
    assert event["source"] == "operator"
    assert event["summary"] == "The start page repeated itself"
    assert event["detail"] == "Twice."
    notes = _record(tmp_path, "TASK-1--notes")
    assert notes["kind"] == "debug" and notes["workflow"] == "task"
    assert notes["agent"] == "codex" and notes["run_id"] == "notes"
    assert notes["errors"] == [] and notes["events"] == [event]

    # The run's own record takes the note over and replaces the standalone one.
    service.next("TASK-1")
    _finish(service)
    record = _record(tmp_path, "TASK-1--01-task")
    assert record["events"] == [event]
    assert not (tmp_path / ".ww" / "debug" / "TASK-1--notes.json").exists()

    # A later note goes onto the completed run's record.
    code, out, _ = _run(
        tmp_path, capsys, "debug", "note", "TASK-1", "--summary", "Afterwards", "--json"
    )
    assert code == 0
    assert json.loads(out) == {
        "target": "TASK-1",
        "record": "TASK-1--01-task",
        "pending": False,
    }
    assert [
        event["summary"] for event in _record(tmp_path, "TASK-1--01-task")["events"]
    ] == ["The start page repeated itself", "Afterwards"]
    assert not _pending(tmp_path, "TASK-1")


def test_the_operator_notes_a_request_and_a_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _project(tmp_path)
    request_id = service.start("jira-task", None, agent="codex").task_id

    code, out, _ = _run(
        tmp_path, capsys, "debug", "note", request_id, "--summary", "Jira page unclear"
    )

    assert code == 0 and f"standalone record `{request_id}--notes`" in out
    notes = _record(tmp_path, f"{request_id}--notes")
    assert notes["workflow"] == "jira-task" and notes["task_id"] == request_id
    assert notes["events"][0]["summary"] == "Jira page unclear"

    code, out, _ = _run(
        tmp_path,
        capsys,
        "debug",
        "note",
        f"{request_id}--notes",
        "--summary",
        "Second thought",
    )
    assert code == 0 and f"Note added to record `{request_id}--notes`." in out
    assert [
        event["summary"]
        for event in _record(tmp_path, f"{request_id}--notes")["events"]
    ] == ["Jira page unclear", "Second thought"]
    # A note on the record itself does not wait with the pending events.
    assert len(_pending(tmp_path, request_id)) == 1

    code, _, err = _run(tmp_path, capsys, "debug", "note", request_id)
    assert code == 1 and "debug note needs --summary" in err
    code, _, err = _run(tmp_path, capsys, "debug", "note", "--summary", "x")
    assert code == 1 and "debug note needs a record, task or request ID" in err


def test_rendering_lists_the_events_under_their_own_heading(tmp_path: Path) -> None:
    service = _project(tmp_path)
    _failed_work(service)
    service.debug_note("TASK-1", summary="I saw it too", detail="Twice.")
    service.next("TASK-1")
    _finish(service)
    record = service.run_reports[DEBUG.name].get("TASK-1--01-task")

    page = render_record(record, DEBUG)

    assert "## Events ww observed and the operator noted" in page
    assert "(ww) **Worker reported `work` failed**: The build exploded." in page
    assert "(operator) **I saw it too**: Twice." in page
    assert page.index("## Inconveniences") < page.index("## Events ww observed")
    listing = render_listing(service.run_reports[DEBUG.name])
    assert "inconveniences: 0, events: 3" in listing


def test_a_feedback_store_keeps_no_events(tmp_path: Path) -> None:
    service = _project(tmp_path)
    with pytest.raises(StateError, match="carry no events"):
        service.run_reports["feedback"].note("TASK-1", source="ww", summary="x")
