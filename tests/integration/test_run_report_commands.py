# SPDX-License-Identifier: GPL-3.0-or-later
"""Debug and feedback collection on a run, and their commands."""

from __future__ import annotations

import json
import webbrowser
from pathlib import Path

import pytest

from tests.workflow_helpers import configured_service
from ww import run_reports
from ww.cli import main
from ww.errors import StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.run_reports import record_id
from ww.service import WorkflowService

WORKFLOW = """workflows:
  - name: task
    description: Implement a change.
    steps:
      - work: Do the work.
"""
ERRORS = json.dumps(
    [
        {
            "summary": "next showed a stale page",
            "detail": "page named init",
            "step": "work",
        }
    ]
)
RECORD_1 = record_id("TASK-1", "01-task")
RECORD_2 = record_id("TASK-2", "01-task")


def _project(root: Path, settings: dict[str, object]) -> WorkflowService:
    (root / "ww.json").write_text(json.dumps(settings), encoding="utf-8")
    return configured_service(root, WORKFLOW)


def _reach_assessment(service: WorkflowService, task_id: str = "TASK-1"):
    service.start("task", task_id, agent="codex", init_artifact="Do it.")
    service.next(task_id)
    done = service.complete(task_id, artifact="Worked.", summary_for_next="Done.")
    assert done.item_name == "assess-ww"
    return done


def _finish(
    service: WorkflowService,
    task_id: str = "TASK-1",
    *,
    feedback: bool = True,
    **values: str,
):
    _reach_assessment(service, task_id)
    assessment = {
        "debug_errors": ERRORS,
        "debug_inconveniences": "[]",
        **{name: value for name, value in values.items() if name.startswith("debug_")},
    }
    summary = service.complete(task_id, variables=tuple(assessment.items()))
    assert summary.item_name == "update-workflow-summary"
    supplied = {
        "summary": "Done.",
        **(
            {"feedback_problems": "[]", "feedback_improvements": '["Add a test step"]'}
            if feedback
            else {}
        ),
        **{name: value for name, value in values.items() if name.startswith("feedb")},
    }
    return service.complete(task_id, variables=tuple(supplied.items()))


def test_collection_is_announced_once_asked_at_the_end_and_kept_locally(
    tmp_path: Path,
) -> None:
    service = _project(
        tmp_path, {"debug": {"collect": True}, "feedback": {"collect": True}}
    )
    started = service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    page = MarkdownOutputAdapter().render_instruction(started)
    assert "> Tell the operator once, now, that ww collects debug info" in page
    assert "`.ww/debug/`" in page and "`.ww/feedback/`" in page

    # The work page says nothing about it.
    work = service.next("TASK-1")
    assert "collects" not in MarkdownOutputAdapter().render_instruction(work)
    # ww's own assessment is an item of its own before the summary; in the
    # single runtime the one session performs it like every other item.
    assessment = service.complete(
        "TASK-1", artifact="Worked.", summary_for_next="Done."
    )
    assert assessment.item_name == "assess-ww"
    page = MarkdownOutputAdapter().render_instruction(assessment)
    assert "## Manager and worker: perform `assess-ww`" in page
    assert "Assess, from this run alone, how ww itself behaved" in page
    assert "- Which ww commands errored or were refused" in page
    assert "- `debug_errors` — A JSON array of the errors, bugs and blockers" in page
    assert '--variable debug_inconveniences="<debug_inconveniences>"' in page
    assert "> ww recorded no events of its own for this run" in page
    assert "feedback_problems" not in page and "summary=" not in page

    with pytest.raises(StateError, match="missing required variable"):
        service.complete("TASK-1", variables=(("debug_errors", "[]"),))
    with pytest.raises(StateError, match="debug_errors must be a JSON array"):
        service.complete(
            "TASK-1",
            variables=(("debug_errors", "oops"), ("debug_inconveniences", "[]")),
        )
    assert not (tmp_path / ".ww" / "debug").exists()

    summary = service.complete(
        "TASK-1",
        variables=(("debug_errors", ERRORS), ("debug_inconveniences", "[]")),
    )
    assert summary.item_name == "update-workflow-summary"
    page = MarkdownOutputAdapter().render_instruction(summary)
    assert "Also assess, from this run alone, how well the workflow" in page
    assert "how ww itself behaved" not in page and "debug_errors" not in page
    assert "- `feedback_improvements` — A JSON array of how the workflow" in page
    assert "ww recorded no events" not in page

    done = service.complete(
        "TASK-1",
        variables=(
            ("summary", "Done."),
            ("feedback_problems", "[]"),
            ("feedback_improvements", '["Add a test step"]'),
        ),
    )
    assert done.status == "completed"
    debug = json.loads(
        (tmp_path / ".ww" / "debug" / f"{RECORD_1}.json").read_text("utf-8")
    )
    assert debug["workflow"] == "task"
    assert debug["id"] == RECORD_1 and debug["run_id"] == "01-task"
    assert "task_id" not in debug and "TASK-1" not in json.dumps(debug)
    assert debug["agent"] == "codex"
    assert debug["errors"][0]["summary"] == "next showed a stale page"
    assert debug["inconveniences"] == []
    assert debug["reported"] is None
    # The workflow appears as its shape, never as the text of ``ww.yaml``.
    assert "workflow_definition" not in debug and "Do the work." not in (
        json.dumps(debug)
    )
    shape = debug["workflow_shape"]
    assert shape["workflow"] == "task" and shape["handoff"] is False
    assert shape["counts"] == {"items": 4, "steps": 2, "hooks": 2}
    assert [(item["name"], item["phase"], item["role"]) for item in shape["items"]] == [
        ("init", "step", "worker"),
        ("work", "step", "worker"),
        ("assess-ww", "before_complete_workflow", "manager"),
        ("update-workflow-summary", "before_complete_workflow", "worker"),
    ]
    assert shape["items"][1] == {
        "name": "work",
        "step": "work",
        "phase": "step",
        "source": "step",
        "kind": "prompt",
        "owner": "agent",
        "role": "worker",
    }
    feedback = json.loads(
        (tmp_path / ".ww" / "feedback" / f"{RECORD_1}.json").read_text("utf-8")
    )
    assert feedback["improvements"] == [{"summary": "Add a test step"}]
    assert "errors" not in feedback


def test_nothing_is_collected_or_announced_by_default(tmp_path: Path) -> None:
    service = _project(tmp_path, {})
    started = service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    assert "collects" not in MarkdownOutputAdapter().render_instruction(started)
    _, snapshot = service.load("TASK-1")
    assert [item.name for item in snapshot.plan.items] == [
        "init",
        "work",
        "update-workflow-summary",
    ]
    service.next("TASK-1")
    summary = service.complete("TASK-1", artifact="Worked.", summary_for_next="Done.")
    assert summary.item_name == "update-workflow-summary"
    page = MarkdownOutputAdapter().render_instruction(summary)
    assert "Also assess" not in page and "debug_errors" not in page

    done = service.complete("TASK-1", variables=(("summary", "Done."),))

    assert done.status == "completed"
    assert not (tmp_path / ".ww" / "debug").exists()
    assert not (tmp_path / ".ww" / "feedback").exists()


def test_debug_only_collects_debug_and_a_started_run_keeps_asking(
    tmp_path: Path,
) -> None:
    service = _project(tmp_path, {"debug": {"collect": True}})
    assessment = _reach_assessment(service)
    page = MarkdownOutputAdapter().render_instruction(assessment)
    assert "debug_errors" in page and "feedback_problems" not in page
    # Switched off meanwhile: the saved plan still asks, and the record is kept.
    (tmp_path / "ww.json").write_text("{}", encoding="utf-8")

    summary = service.complete(
        "TASK-1", variables=(("debug_errors", "[]"), ("debug_inconveniences", "[]"))
    )
    assert summary.item_name == "update-workflow-summary"
    assert "Also assess" not in MarkdownOutputAdapter().render_instruction(summary)
    done = service.complete("TASK-1", variables=(("summary", "Done."),))

    assert done.status == "completed"
    assert (tmp_path / ".ww" / "debug" / f"{RECORD_1}.json").is_file()
    assert not (tmp_path / ".ww" / "feedback").exists()


def test_under_auto_the_manager_answers_the_debug_questions(tmp_path: Path) -> None:
    service = _project(tmp_path, {"debug": {"collect": True}})
    md = MarkdownOutputAdapter()
    service.start(
        "task",
        "TASK-1",
        agent="claudecode",
        init_artifact="Do it.",
        workflow_runtime="auto",
        caller_role="manager",
    )
    _, snapshot = service.load("TASK-1")
    (assessment,) = [item for item in snapshot.plan.items if item.name == "assess-ww"]
    assert assessment.role == "manager" and assessment.owner == "agent"
    assert assessment.phase == "before_complete_workflow"
    dispatch = service.next("TASK-1", caller_role="manager")
    assert "assess-ww" not in md.render_instruction(dispatch)
    service.fail(
        "TASK-1",
        "The build exploded in src/app.py.",
        caller_role="worker",
        assignment=dispatch.assignment_token,
    )
    service.next("TASK-1", caller_role="manager", retry=True)
    handed = service.complete(
        "TASK-1",
        artifact="Done.",
        summary_for_next="Done.",
        caller_role="worker",
        assignment=dispatch.assignment_token,
    )
    assert handed.control == "handoff_manager"

    # The worker's assignment ended there: the manager performs the
    # assessment itself, and the summary with it.
    page = service.next("TASK-1", caller_role="manager")
    assert page.item_name == "assess-ww" and page.role == "manager"
    text = md.render_instruction(page)
    assert "## Manager: perform the `assess-ww` assignment" in text
    assert "This step is yours (`role: manager`)" in text
    assert "Assess, from this run alone, how ww itself behaved" in text
    for question in (
        "- Which ww commands errored or were refused although they should have passed?",
        "- Which pages misled you or lacked a command you needed?",
        "- Which steps did you retry, and which did the operator force past?",
        "- Did you edit ww's state by hand, or work around ww in another way?",
        "- Which rounds of the run were wasted on ww rather than the work?",
    ):
        assert question in text
    assert "name no project, path, ticket key, commit message, code or person" in (text)
    assert "> ww already recorded these events for this run; add what they miss" in (
        text
    )
    assert "(ww) Stopped for the operator (work_failed) at `work`" in text
    assert "(ww) Worker reported `work` failed: The build exploded in src/app.py." in (
        text
    )
    assert "./ww complete TASK-1 --role manager --variable debug_errors=" in text
    # The worker's token ended with its assignment: it cannot answer for ww.
    with pytest.raises(StateError, match="your assignment has ended"):
        service.complete(
            "TASK-1",
            variables=(("debug_errors", "[]"), ("debug_inconveniences", "[]")),
            caller_role="worker",
            assignment=dispatch.assignment_token,
        )

    summary = service.complete(
        "TASK-1",
        variables=(
            (
                "debug_errors",
                '[{"summary": "fail on TASK-1 showed the page of src/app.py"}]',
            ),
            ("debug_inconveniences", "[]"),
        ),
        caller_role="manager",
    )
    assert summary.item_name == "update-workflow-summary"
    done = service.complete(
        "TASK-1", variables=(("summary", "Done."),), caller_role="manager"
    )
    assert done.status == "completed"
    record = json.loads(
        (tmp_path / ".ww" / "debug" / f"{RECORD_1}.json").read_text("utf-8")
    )
    assert record["runtime"] == "auto"
    assert record["errors"] == [{"summary": "fail on <task> showed the page of <path>"}]
    assert [event["summary"] for event in record["events"]] == [
        "Stopped for the operator (work_failed) at <value>",
        "Worker reported <value> failed",
    ]


def _run(
    root: Path, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    code = main(["--root", str(root), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_discover_never_offers_to_send_debug_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _project(tmp_path, {"debug": {"collect": True, "report": True}})
    _finish(service, feedback=False)

    code, out, _ = _run(tmp_path, capsys, "discover")
    assert code == 0
    assert "debug" not in out.lower()
    code, out, _ = _run(tmp_path, capsys, "discover", "--json")
    assert code == 0 and "debug_reports" not in json.loads(out)


def test_debug_report_is_the_operators_command_and_needs_report_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _project(tmp_path, {"debug": {"collect": True}})
    _finish(service, feedback=False)
    monkeypatch.setattr(run_reports, "gh_is_authenticated", lambda: True)
    monkeypatch.setattr(
        run_reports, "gh_create_issue", lambda title, body: pytest.fail("report off")
    )

    code, _, err = _run(tmp_path, capsys, "debug", "report", "--yes")

    assert code == 1
    assert "debug report is off: set debug.report to true in ww.json" in err
    assert (
        json.loads(
            (tmp_path / ".ww" / "debug" / f"{RECORD_1}.json").read_text("utf-8")
        )["reported"]
        is None
    )


def test_the_commands_list_show_and_report_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _project(
        tmp_path,
        {"debug": {"collect": True, "report": True}, "feedback": {"collect": True}},
    )
    code, out, _ = _run(tmp_path, capsys, "debug")
    assert code == 0 and "No records under `.ww/debug/`." in out
    _finish(service)
    _finish(service, "TASK-2", debug_errors="[]")

    code, out, _ = _run(tmp_path, capsys, "debug", "list")
    assert code == 0
    assert f"- `{RECORD_1}` — `task` at" in out and "not reported" in out
    assert "TASK-1" not in out
    code, out, _ = _run(tmp_path, capsys, "debug", "show", RECORD_1)
    assert code == 0 and out.startswith("# Debug record: `task` at ")
    assert "- **next showed a stale page** (step `work`): page named init" in out
    assert "<details><summary>Workflow shape</summary>" in out
    code, out, _ = _run(
        tmp_path, capsys, "workflow-feedback", "show", RECORD_1, "--json"
    )
    assert code == 0 and json.loads(out)["improvements"] == [
        {"summary": "Add a test step"}
    ]
    code, out, _ = _run(tmp_path, capsys, "workflow-feedback")
    assert code == 0 and "# Workflow feedback records" in out
    code, _, err = _run(tmp_path, capsys, "debug", "show")
    assert code == 1 and "debug show needs a record ID" in err

    # Without a terminal and without --yes, nothing is sent.
    monkeypatch.setattr(run_reports, "gh_is_authenticated", lambda: True)
    monkeypatch.setattr(
        run_reports, "gh_create_issue", lambda title, body: pytest.fail("not confirmed")
    )
    code, out, err = _run(tmp_path, capsys, "debug", "report")
    assert code == 1
    assert f"## Report `{RECORD_1}`" in err and "needs the operator's confirmation" in (
        err
    )
    assert f"- `{RECORD_1}`: skipped; still unreported" in out

    created: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_reports,
        "gh_create_issue",
        lambda title, body: (
            created.append((title, body)) or "https://github.com/x/issues/7"
        ),
    )
    code, out, err = _run(tmp_path, capsys, "debug", "report", RECORD_1, "--yes")
    assert code == 0
    assert (
        f"will send record {RECORD_1} to ww's GitHub issues through the gh CLI" in err
    )
    assert f"- `{RECORD_1}`: issue created at https://github.com/x/issues/7" in out
    assert created[0][0].startswith("Debug report: `task` run, ww ")
    assert "next showed a stale page" in created[0][1]
    assert "TASK-1" not in created[0][1] and "Do the work." not in created[0][1]

    # The browser path, with --yes, opens the prefilled page and marks the record.
    monkeypatch.setattr(run_reports, "gh_is_authenticated", lambda: False)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    code, out, err = _run(tmp_path, capsys, "debug", "report", "--yes", "--json")
    assert code == 0
    assert json.loads(out) == {
        "reported": [{"id": RECORD_2, "status": "opened", "url": None}]
    }
    assert "by opening the prefilled new-issue page" in err
    assert opened and opened[0].startswith(run_reports.NEW_ISSUE_URL + "?title=")
    code, out, _ = _run(tmp_path, capsys, "debug", "report")
    assert code == 0 and "No unreported debug records." in out
    code, out, _ = _run(tmp_path, capsys, "debug", "list", "--json")
    assert json.loads(out)["unreported"] == 0
