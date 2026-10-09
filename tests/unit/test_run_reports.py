# SPDX-License-Identifier: GPL-3.0-or-later
"""Run reports: value validation, the record store, and GitHub reporting."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from ww.errors import StateError
from ww.execution_models.runs import PLAN_SCHEMA_VERSION
from ww.project_config import ProjectConfig, load_project_config
from ww.run_reports import (
    DEBUG,
    FEEDBACK,
    MAX_ISSUE_URL_LENGTH,
    NEW_ISSUE_URL,
    RunReportStore,
    debug_item,
    issue_body,
    issue_title,
    new_issue_url,
    record_id,
    redact,
    render_listing,
    render_record,
    report_records,
    start_notice,
    summary_prompt,
    summary_variables,
    validate_report_values,
)
from ww.storage import Storage
from ww.storage_adapters.task_document import TASK_STATE_SCHEMA_VERSION

ERRORS = json.dumps(
    [
        {
            "summary": "complete refused a valid artifact",
            "detail": "ww error: artifact is required",
            "step": "work",
        }
    ]
)
SHAPE = {"workflow": "task", "items": [{"name": "work", "kind": "prompt"}]}
RECORD_1 = record_id("TASK-1", "01-task")
RECORD_2 = record_id("TASK-2", "01-task")


def _config(tmp_path: Path, settings: dict[str, object]) -> ProjectConfig:
    path = tmp_path / "ww.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    return load_project_config(path)


def _values(**overrides: str) -> dict[str, str]:
    values = {
        "summary": "Done.",
        "debug_errors": ERRORS,
        "debug_inconveniences": '["The page repeats the requirements"]',
        "feedback_problems": "[]",
        "feedback_improvements": '[{"summary": "Add a test step"}]',
    }
    values.update(overrides)
    return values


def _record(tmp_path: Path, kind=DEBUG, **overrides: str) -> dict:
    store = RunReportStore(Storage(tmp_path), kind)
    record = store.record(
        task_id="TASK-1",
        run_id="01-task",
        workflow="task",
        agent="codex",
        runtime="single",
        modes=("economy",),
        values=_values(**overrides),
        workflow_shape=SHAPE,
        now="2026-10-06T07:45:13Z",
    )
    assert record is not None
    return record


def test_the_run_asks_only_for_what_is_switched_on(tmp_path: Path) -> None:
    off = _config(tmp_path, {})
    assert summary_variables(off) == ()
    assert summary_prompt("Summarize.", off) == "Summarize."
    assert start_notice(off) is None
    assert debug_item(off) is None

    both = _config(
        tmp_path, {"debug": {"collect": True}, "feedback": {"collect": True}}
    )
    # The summary asks for the workflow feedback; ww's own assessment has
    # an item of its own, performed by the session that drove the run.
    names = [variable.name for variable in summary_variables(both)]
    assert names == ["feedback_problems", "feedback_improvements"]
    assert summary_prompt("Summarize.", both).startswith(
        "Summarize. Also assess, from this run alone, how well the workflow"
    )
    item = debug_item(both)
    assert item is not None and item.name == "assess-ww"
    assert [value.name for value in item.provide] == [
        "debug_errors",
        "debug_inconveniences",
    ]
    assert item.action is not None and item.action.identifier == "prompt"
    notice = start_notice(both)
    assert notice is not None
    assert "`.ww/debug/`" in notice and "`.ww/feedback/`" in notice
    assert "stays on this machine only" in notice

    debug_only = _config(tmp_path, {"debug": {"collect": True, "report": True}})
    assert summary_variables(debug_only) == ()
    assert summary_prompt("Summarize.", debug_only) == "Summarize."
    assert debug_item(debug_only) is not None
    assert ".ww/feedback/" not in (start_notice(debug_only) or "")


def test_the_debug_questions_ask_about_ww_alone() -> None:
    prompt = DEBUG.prompt
    for question in (
        "Which ww commands errored or were refused",
        "Which pages misled you or lacked a command",
        "Which steps did you retry, and which did the operator force past",
        "Did you edit ww's state by hand",
        "Which rounds of the run were wasted on ww",
    ):
        assert question in prompt
    assert "name no project, path, ticket key, commit message, code or person" in (
        prompt
    )
    for array in DEBUG.arrays:
        assert "no project" in array.description


def test_redaction_hides_the_task_paths_values_emails_and_keys() -> None:
    text = (
        "`./ww complete TASK-42/TASK-42-01A --artifact docs/plan.md` refused at "
        "`work` in run 01-task: agent item 'work' failed, see /Users/me/x.py and "
        'notes.txt; mail dev@example.com about PROJ-7 or "the fix"; ww\'s page '
        "didn't say why. Record TASK-42-TASK-42-01A--01-task."
    )

    redacted = redact(text, task_id="TASK-42/TASK-42-01A", run_id="01-task")

    assert redacted == (
        "`./ww complete <task> --artifact <path>` refused at <value> in run "
        "<run>: agent item <value> failed, see <path> and <path>; mail <email> "
        "about <ticket> or <value>; ww's page didn't say why. Record <task>--<run>."
    )
    # Without the IDs, a Jira-like task ID is still a ticket key.
    assert redact("TASK-1 failed in a/b") == "<ticket> failed in <path>"
    assert redact("plain words stay") == "plain words stay"


def test_report_values_are_parsed_and_malformed_ones_refused() -> None:
    parsed = validate_report_values(_values())
    assert parsed["debug_errors"][0] == {
        "summary": "complete refused a valid artifact",
        "detail": "ww error: artifact is required",
        "step": "work",
    }
    # A bare string is a summary; empty optional fields are dropped.
    assert parsed["debug_inconveniences"] == [
        {"summary": "The page repeats the requirements"}
    ]
    assert validate_report_values({"summary": "Done."}) == {}
    assert validate_report_values(
        {"debug_errors": '[{"summary": " x ", "detail": "", "step": null}]'}
    ) == {"debug_errors": [{"summary": "x"}]}

    for value, message in (
        ("not json", "must be a JSON array"),
        ('{"summary": "x"}', "must be a JSON array of objects"),
        ("[1]", r"debug_errors\[0\] must be an object"),
        ('[{"detail": "x"}]', r"\[0\]\.summary must be a non-empty string"),
        ('[{"summary": "x", "severity": 1}]', r"unknown key\(s\): severity"),
        ('[{"summary": "x", "step": 3}]', r"\[0\]\.step must be a string or null"),
    ):
        with pytest.raises(StateError, match=message):
            validate_report_values({"debug_errors": value})


def test_the_store_keeps_one_record_per_run_and_marks_reports(tmp_path: Path) -> None:
    store = RunReportStore(Storage(tmp_path), DEBUG)
    assert store.records() == []
    assert (
        store.record(
            task_id="TASK-1",
            run_id="01-task",
            workflow="task",
            agent="codex",
            runtime="single",
            modes=(),
            values={"summary": "Done."},
            workflow_shape=None,
        )
        is None
    )
    assert not (tmp_path / ".ww" / "debug").exists()

    record = _record(tmp_path)
    path = tmp_path / ".ww" / "debug" / f"{RECORD_1}.json"
    assert path.is_file()
    assert record["id"] == RECORD_1
    assert "task_id" not in record and record["run_id"] == "01-task"
    assert record["workflow"] == "task"
    assert record["recorded_at"] == "2026-10-06T07:45:13Z"
    assert record["modes"] == ["economy"]
    assert record["plan_schema_version"] == PLAN_SCHEMA_VERSION
    assert record["task_state_schema_version"] == TASK_STATE_SCHEMA_VERSION
    assert record["workflow_shape"] == SHAPE
    assert record["errors"][0]["step"] == "work"
    assert record["inconveniences"] == [
        {"summary": "The page repeats the requirements"}
    ]
    assert "problems" not in record
    assert record["reported"] is None
    assert json.loads(path.read_text(encoding="utf-8")) == record

    # A completed run committed again keeps its first record; another run
    # adds one.
    assert _record(tmp_path, debug_errors="[]") == record
    assert [len(r["errors"]) for r in store.records()] == [1]
    other = store.record(
        task_id="TASK-42/TASK-42-01A",
        run_id="02-task",
        workflow="task",
        agent="codex",
        runtime=None,
        modes=(),
        values=_values(),
        workflow_shape=None,
        now="2026-10-07T00:00:00Z",
    )
    child = record_id("TASK-42/TASK-42-01A", "02-task")
    assert other is not None and other["id"] == child
    assert [r["id"] for r in store.unreported()] == [RECORD_1, child]

    marked = store.mark_reported(
        RECORD_1,
        method="gh",
        url="https://example/1",
        now="2026-10-08T00:00:00Z",
    )
    assert marked["reported"] == {
        "at": "2026-10-08T00:00:00Z",
        "method": "gh",
        "url": "https://example/1",
    }
    assert [r["id"] for r in store.unreported()] == [child]
    assert store.get(RECORD_1)["reported"]["method"] == "gh"
    with pytest.raises(StateError, match="unknown debug record 'nope'"):
        store.get("nope")

    listing = render_listing(store)
    assert "# Debug records" in listing
    assert "reported 2026-10-08T00:00:00Z via gh (https://example/1)" in listing
    assert "errors: 1, inconveniences: 1, events: 0; not reported" in listing


def test_a_malformed_record_file_is_reported_with_its_path(tmp_path: Path) -> None:
    store = RunReportStore(Storage(tmp_path), FEEDBACK)
    store.directory.mkdir(parents=True)
    (store.directory / "bad.json").write_text("{}", encoding="utf-8")

    with pytest.raises(StateError, match="invalid feedback record .*bad.json"):
        store.records()


def test_feedback_records_use_their_own_arrays_and_directory(tmp_path: Path) -> None:
    record = _record(tmp_path, FEEDBACK)

    assert (tmp_path / ".ww" / "feedback" / f"{RECORD_1}.json").is_file()
    assert record["problems"] == []
    assert record["improvements"] == [{"summary": "Add a test step"}]
    assert "errors" not in record
    rendered = render_record(record, FEEDBACK)
    assert rendered.startswith("# Workflow feedback record: `task` at 2026-10-06")
    assert "## Workflow improvements\n\n- **Add a test step**" in rendered
    assert "## Application and code problems met during the run\n\nNone." in rendered


def test_the_issue_carries_the_record_and_fits_a_prefilled_url(tmp_path: Path) -> None:
    record = _record(tmp_path)

    assert issue_title(record) == "Debug report: `task` run, ww 0.1.0, 1 error"
    body = issue_body(record)
    assert "| Workflow | `task` |" in body
    assert "- **complete refused a valid artifact** (step `work`): ww error" in body
    assert "<details><summary>Workflow shape</summary>" in body
    assert "```json\n" + json.dumps(SHAPE, indent=2) + "\n```" in body
    assert body.rstrip().endswith("with `ww debug report`._")

    url = new_issue_url(record)
    assert url.startswith(NEW_ISSUE_URL + "?")
    query = parse_qs(urlparse(url).query)
    assert query["title"] == [issue_title(record)]
    assert query["body"] == [body]

    # Too long with the shape: the shape goes first, then the body.
    long = {**record, "workflow_shape": {"items": ["x" * 9000]}}
    url = new_issue_url(long)
    assert len(url) <= MAX_ISSUE_URL_LENGTH
    assert "left out" in parse_qs(urlparse(url).query)["body"][0]
    longer = {**record, "errors": [{"summary": "y" * 9000}]}
    url = new_issue_url(longer)
    assert len(url) <= MAX_ISSUE_URL_LENGTH
    assert "truncated" in parse_qs(urlparse(url).query)["body"][0]


def test_reporting_shows_confirms_and_marks_each_record(tmp_path: Path) -> None:
    store = RunReportStore(Storage(tmp_path), DEBUG)
    first = _record(tmp_path)
    second = store.record(
        task_id="TASK-2",
        run_id="01-task",
        workflow="task",
        agent="codex",
        runtime=None,
        modes=(),
        values=_values(),
        workflow_shape=None,
        now="2026-10-07T00:00:00Z",
    )
    assert second is not None
    shown: list[str] = []
    created: list[tuple[str, str]] = []

    def create(title: str, body: str) -> str:
        created.append((title, body))
        return f"https://github.com/x/issues/{len(created)}"

    outcomes = report_records(
        store,
        [first, second],
        confirm=lambda record, method: record["id"] == RECORD_1,
        confirm_submitted=lambda identifier: True,
        show=shown.append,
        gh_available=lambda: True,
        create_issue=create,
        open_browser=lambda url: pytest.fail("no browser with gh"),
    )

    assert [o.to_dict() for o in outcomes] == [
        {"id": RECORD_1, "status": "created", "url": "https://github.com/x/issues/1"},
        {"id": RECORD_2, "status": "skipped", "url": None},
    ]
    assert len(shown) == 2
    assert shown[0].startswith(f"## Report `{RECORD_1}`")
    assert "as a new issue titled `Debug report:" in shown[0]
    assert issue_body(first) in shown[0]
    assert created == [(issue_title(first), issue_body(first))]
    assert store.get(RECORD_1)["reported"]["url"] == "https://github.com/x/issues/1"
    assert [r["id"] for r in store.unreported()] == [RECORD_2]


def test_without_gh_the_browser_opens_the_prefilled_issue(tmp_path: Path) -> None:
    store = RunReportStore(Storage(tmp_path), DEBUG)
    record = _record(tmp_path)
    opened: list[str] = []
    shown: list[str] = []

    outcomes = report_records(
        store,
        [record],
        confirm=lambda record, method: method == "browser",
        confirm_submitted=lambda identifier: False,
        show=shown.append,
        gh_available=lambda: False,
        create_issue=lambda title, body: pytest.fail("gh is unavailable"),
        open_browser=lambda url: opened.append(url) or True,
    )

    assert [o.status for o in outcomes] == ["cancelled"]
    assert opened == [new_issue_url(record)]
    assert "Opened the prefilled new-issue page" in shown[1]
    assert store.get(record["id"])["reported"] is None

    outcomes = report_records(
        store,
        [record],
        confirm=lambda record, method: True,
        confirm_submitted=lambda identifier: True,
        show=shown.append,
        gh_available=lambda: False,
        create_issue=lambda title, body: pytest.fail("gh is unavailable"),
        open_browser=lambda url: False,
    )

    assert [o.status for o in outcomes] == ["opened"]
    assert "Could not open a browser; open the prefilled" in shown[-1]
    assert store.get(record["id"])["reported"] == {
        "at": store.get(record["id"])["reported"]["at"],
        "method": "browser",
        "url": None,
    }


def test_record_ids_name_the_task_by_its_digest_and_the_run() -> None:
    # Twelve hex digits of the task ID's SHA-256: the ID itself stays out.
    assert record_id("TASK-1", "01-task") == "05d1ca4b1083--01-task"
    assert record_id("TASK-42/TASK-42-01A", "03-task") == "c3014dd14e2f--03-task"
    assert record_id("TASK-1", "01-task") != record_id("TASK-2", "01-task")


def test_a_record_keeps_no_project_or_task_data(tmp_path: Path) -> None:
    (tmp_path / "ww.json").write_text('{"debug": {"collect": true}}', "utf-8")
    store = RunReportStore(Storage(tmp_path), DEBUG)
    store.note(
        "PROJ-123",
        source="ww",
        summary="Stopped for the operator (work_failed) at `develop`",
        detail=(
            "agent item 'develop' failed: tests in /Users/me/acme/tests/test_x.py "
            'broke after commit "PROJ-123: add billing"'
        ),
        now="2026-10-06T07:00:00Z",
    )
    store.note(
        "PROJ-123",
        source="operator",
        summary="The fix page for PROJ-123 named the wrong step",
        now="2026-10-06T07:01:00Z",
    )

    record = store.record(
        task_id="PROJ-123",
        run_id="01-task",
        workflow="task",
        agent="codex",
        runtime="auto",
        modes=(),
        values=_values(
            debug_errors=json.dumps(
                [
                    {
                        "summary": "complete refused PROJ-123's artifact",
                        "detail": (
                            "`./ww complete PROJ-123 --artifact docs/acme/plan.md` "
                            "said 'artifact is required'; mail me@acme.com"
                        ),
                        "step": "develop",
                    }
                ]
            )
        ),
        workflow_shape=SHAPE,
        now="2026-10-06T07:45:13Z",
    )

    assert record is not None
    text = json.dumps(record)
    for private in (
        "PROJ-123",
        "/Users/me/acme",
        "test_x.py",
        "docs/acme/plan.md",
        "add billing",
        "me@acme.com",
        "'develop'",
        "`develop`",
    ):
        assert private not in text
    assert record["id"] == "1295595878aa--01-task"
    assert record["errors"] == [
        {
            "summary": "complete refused <task>'s artifact",
            "detail": (
                "`./ww complete <task> --artifact <path>` said <value>; mail <email>"
            ),
            "step": "develop",
        }
    ]
    assert [event["summary"] for event in record["events"]] == [
        "Stopped for the operator (work_failed) at <value>",
        "The fix page for <task> named the wrong step",
    ]
    assert record["events"][0]["detail"] == (
        "agent item <value> failed: tests in <path> broke after commit <value>"
    )
    page = render_record(record, DEBUG)
    assert "PROJ-123" not in page and "acme" not in page
    assert "- **complete refused <task>'s artifact** (step `develop`)" in page
    assert "(ww) **Stopped for the operator (work_failed) at <value>**" in page

    # A note added later is redacted the same way, and the task's latest
    # record is still found by its digest.
    store.add_event(
        record["id"],
        source="operator",
        summary="PROJ-123 again, in src/x.py",
        task_id="PROJ-123",
    )
    latest = store.latest_for("PROJ-123")
    assert latest is not None
    assert latest["events"][-1]["summary"] == "<task> again, in <path>"
    assert store.latest_for("PROJ-124") is None
