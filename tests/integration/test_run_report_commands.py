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


def _project(root: Path, settings: dict[str, object]) -> WorkflowService:
    (root / "ww.json").write_text(json.dumps(settings), encoding="utf-8")
    return configured_service(root, WORKFLOW)


def _reach_summary(service: WorkflowService, task_id: str = "TASK-1"):
    service.start("task", task_id, agent="codex", init_artifact="Do it.")
    service.next(task_id)
    done = service.complete(task_id, artifact="Worked.", summary_for_next="Done.")
    assert done.item_name == "update-workflow-summary"
    return done


def _finish(
    service: WorkflowService,
    task_id: str = "TASK-1",
    *,
    feedback: bool = True,
    **values: str,
):
    _reach_summary(service, task_id)
    supplied = {
        "summary": "Done.",
        "debug_errors": ERRORS,
        "debug_inconveniences": "[]",
        **(
            {"feedback_problems": "[]", "feedback_improvements": '["Add a test step"]'}
            if feedback
            else {}
        ),
        **values,
    }
    return service.complete(task_id, variables=tuple(supplied.items()))


def test_collection_is_announced_once_asked_at_the_summary_and_kept_locally(
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
    summary = service.complete("TASK-1", artifact="Worked.", summary_for_next="Done.")
    assert summary.item_name == "update-workflow-summary"
    page = MarkdownOutputAdapter().render_instruction(summary)
    assert "Also assess, from this run alone, how ww itself behaved" in page
    assert "- `debug_errors` — A JSON array of the errors, bugs and blockers" in page
    assert "- `feedback_improvements` — A JSON array of how the workflow" in page
    assert '--variable debug_inconveniences="<debug_inconveniences>"' in page

    with pytest.raises(StateError, match="missing required variable"):
        service.complete("TASK-1", variables=(("summary", "Done."),))
    with pytest.raises(StateError, match="debug_errors must be a JSON array"):
        service.complete(
            "TASK-1",
            variables=(
                ("summary", "Done."),
                ("debug_errors", "oops"),
                ("debug_inconveniences", "[]"),
                ("feedback_problems", "[]"),
                ("feedback_improvements", "[]"),
            ),
        )
    assert not (tmp_path / ".ww" / "debug").exists()

    done = service.complete(
        "TASK-1",
        variables=(
            ("summary", "Done."),
            ("debug_errors", ERRORS),
            ("debug_inconveniences", "[]"),
            ("feedback_problems", "[]"),
            ("feedback_improvements", '["Add a test step"]'),
        ),
    )
    assert done.status == "completed"
    debug = json.loads(
        (tmp_path / ".ww" / "debug" / "TASK-1--01-task.json").read_text("utf-8")
    )
    assert debug["workflow"] == "task"
    assert debug["task_id"] == "TASK-1" and debug["run_id"] == "01-task"
    assert debug["agent"] == "codex"
    assert debug["errors"][0]["summary"] == "next showed a stale page"
    assert debug["inconveniences"] == []
    assert debug["workflow_definition"]["steps"] == [{"work": "Do the work."}]
    assert debug["reported"] is None
    feedback = json.loads(
        (tmp_path / ".ww" / "feedback" / "TASK-1--01-task.json").read_text("utf-8")
    )
    assert feedback["improvements"] == [{"summary": "Add a test step"}]
    assert "errors" not in feedback


def test_nothing_is_collected_or_announced_by_default(tmp_path: Path) -> None:
    service = _project(tmp_path, {})
    started = service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    assert "collects" not in MarkdownOutputAdapter().render_instruction(started)
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
    summary = _reach_summary(service)
    page = MarkdownOutputAdapter().render_instruction(summary)
    assert "debug_errors" in page and "feedback_problems" not in page
    # Switched off meanwhile: the saved plan still asks, and the record is kept.
    (tmp_path / "ww.json").write_text("{}", encoding="utf-8")

    done = service.complete(
        "TASK-1",
        variables=(
            ("summary", "Done."),
            ("debug_errors", "[]"),
            ("debug_inconveniences", "[]"),
        ),
    )

    assert done.status == "completed"
    assert (tmp_path / ".ww" / "debug" / "TASK-1--01-task.json").is_file()
    assert not (tmp_path / ".ww" / "feedback").exists()


def _run(
    root: Path, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    code = main(["--root", str(root), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_discover_offers_unreported_records_only_with_report_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _project(tmp_path, {"debug": {"collect": True}})
    code, out, _ = _run(tmp_path, capsys, "discover")
    assert code == 0 and "Debug reports to send" not in out
    _finish(service, feedback=False)

    code, out, _ = _run(tmp_path, capsys, "discover")
    assert code == 0 and "Debug reports to send" not in out
    (tmp_path / "ww.json").write_text(
        json.dumps({"debug": {"collect": True, "report": True}}), encoding="utf-8"
    )
    code, out, _ = _run(tmp_path, capsys, "discover")
    assert code == 0
    assert "## Debug reports to send" in out
    assert "ww has 1 debug record about its own behaviour" in out
    assert "ask the operator once, yes or no" in out
    assert "`ww-debug-report` skill (it runs `./ww debug report`)" in out
    code, out, _ = _run(tmp_path, capsys, "discover", "--json")
    assert json.loads(out)["debug_reports"] == {
        "unreported": 1,
        "offer": True,
        "command": "./ww debug report",
        "skill": "ww-debug-report",
    }


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
    assert "- `TASK-1--01-task` — `task` at" in out and "not reported" in out
    code, out, _ = _run(tmp_path, capsys, "debug", "show", "TASK-1--01-task")
    assert code == 0 and out.startswith("# Debug record: `task` at ")
    assert "- **next showed a stale page** (step `work`): page named init" in out
    code, out, _ = _run(
        tmp_path, capsys, "workflow-feedback", "show", "TASK-1--01-task", "--json"
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
    assert (
        "## Report `TASK-1--01-task`" in err
        and "needs the operator's confirmation" in err
    )
    assert "- `TASK-1--01-task`: skipped; still unreported" in out

    created: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_reports,
        "gh_create_issue",
        lambda title, body: (
            created.append((title, body)) or "https://github.com/x/issues/7"
        ),
    )
    code, out, err = _run(
        tmp_path, capsys, "debug", "report", "TASK-1--01-task", "--yes"
    )
    assert code == 0
    assert (
        "will send record TASK-1--01-task to ww's GitHub issues through the gh CLI"
        in err
    )
    assert "- `TASK-1--01-task`: issue created at https://github.com/x/issues/7" in out
    assert created[0][0].startswith("Debug report: `task` run, ww ")
    assert "next showed a stale page" in created[0][1]

    # The browser path, with --yes, opens the prefilled page and marks the record.
    monkeypatch.setattr(run_reports, "gh_is_authenticated", lambda: False)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    code, out, err = _run(tmp_path, capsys, "debug", "report", "--yes", "--json")
    assert code == 0
    assert json.loads(out) == {
        "reported": [{"id": "TASK-2--01-task", "status": "opened", "url": None}]
    }
    assert "by opening the prefilled new-issue page" in err
    assert opened and opened[0].startswith(run_reports.NEW_ISSUE_URL + "?title=")
    code, out, _ = _run(tmp_path, capsys, "debug", "report")
    assert code == 0 and "No unreported debug records." in out
    code, out, _ = _run(tmp_path, capsys, "debug", "list", "--json")
    assert json.loads(out)["unreported"] == 0
    code, out, _ = _run(tmp_path, capsys, "discover")
    assert "Debug reports to send" not in out
