# SPDX-License-Identifier: GPL-3.0-or-later
"""Run reports: value validation, the record store, and GitHub reporting."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from ww.errors import StateError
from ww.project_config import ProjectConfig, load_project_config
from ww.run_reports import (
    DEBUG,
    FEEDBACK,
    MAX_ISSUE_URL_LENGTH,
    NEW_ISSUE_URL,
    RunReportStore,
    issue_body,
    issue_title,
    new_issue_url,
    record_id,
    render_listing,
    render_record,
    report_records,
    start_notice,
    summary_prompt,
    summary_variables,
    validate_report_values,
    workflow_source,
)
from ww.storage import Storage

ERRORS = json.dumps(
    [
        {
            "summary": "complete refused a valid artifact",
            "detail": "ww error: artifact is required",
            "step": "work",
        }
    ]
)


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
        workflow_definition={"task": "Implement.", "steps": [{"work": "Work."}]},
        now="2026-10-06T07:45:13Z",
    )
    assert record is not None
    return record


def test_the_summary_asks_only_for_what_is_switched_on(tmp_path: Path) -> None:
    off = _config(tmp_path, {})
    assert summary_variables(off) == ()
    assert summary_prompt("Summarize.", off) == "Summarize."
    assert start_notice(off) is None

    both = _config(
        tmp_path, {"debug": {"collect": True}, "feedback": {"collect": True}}
    )
    names = [variable.name for variable in summary_variables(both)]
    assert names == [
        "debug_errors",
        "debug_inconveniences",
        "feedback_problems",
        "feedback_improvements",
    ]
    assert summary_prompt("Summarize.", both).startswith("Summarize. Also assess")
    notice = start_notice(both)
    assert notice is not None
    assert "`.ww/debug/`" in notice and "`.ww/feedback/`" in notice
    assert "stays on this machine only" in notice

    debug_only = _config(tmp_path, {"debug": {"collect": True, "report": True}})
    assert [v.name for v in summary_variables(debug_only)] == [
        "debug_errors",
        "debug_inconveniences",
    ]
    assert ".ww/feedback/" not in (start_notice(debug_only) or "")


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
            workflow_definition=None,
        )
        is None
    )
    assert not (tmp_path / ".ww" / "debug").exists()

    record = _record(tmp_path)
    path = tmp_path / ".ww" / "debug" / "TASK-1--01-task.json"
    assert path.is_file()
    assert record["id"] == "TASK-1--01-task"
    assert record["workflow"] == "task"
    assert record["recorded_at"] == "2026-10-06T07:45:13Z"
    assert record["modes"] == ["economy"]
    assert record["plan_schema_version"] == 2
    assert record["task_state_schema_version"] == 2
    assert record["workflow_definition"]["steps"] == [{"work": "Work."}]
    assert record["errors"][0]["step"] == "work"
    assert record["inconveniences"] == [
        {"summary": "The page repeats the requirements"}
    ]
    assert "problems" not in record
    assert record["reported"] is None
    assert json.loads(path.read_text(encoding="utf-8")) == record

    # The same run again replaces its record; another run adds one.
    _record(tmp_path, debug_errors="[]")
    assert [r["errors"] for r in store.records()] == [[]]
    other = store.record(
        task_id="TASK-42/TASK-42-01A",
        run_id="02-task",
        workflow="task",
        agent="codex",
        runtime=None,
        modes=(),
        values=_values(),
        workflow_definition=None,
        now="2026-10-07T00:00:00Z",
    )
    assert other is not None and other["id"] == "TASK-42-TASK-42-01A--02-task"
    assert [r["id"] for r in store.unreported()] == [
        "TASK-1--01-task",
        "TASK-42-TASK-42-01A--02-task",
    ]

    marked = store.mark_reported(
        "TASK-1--01-task",
        method="gh",
        url="https://example/1",
        now="2026-10-08T00:00:00Z",
    )
    assert marked["reported"] == {
        "at": "2026-10-08T00:00:00Z",
        "method": "gh",
        "url": "https://example/1",
    }
    assert [r["id"] for r in store.unreported()] == ["TASK-42-TASK-42-01A--02-task"]
    assert store.get("TASK-1--01-task")["reported"]["method"] == "gh"
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

    assert (tmp_path / ".ww" / "feedback" / "TASK-1--01-task.json").is_file()
    assert record["problems"] == []
    assert record["improvements"] == [{"summary": "Add a test step"}]
    assert "errors" not in record
    rendered = render_record(record, FEEDBACK)
    assert rendered.startswith("# Workflow feedback record: `task` at 2026-10-06")
    assert "## Workflow improvements\n\n- **Add a test step**" in rendered
    assert "## Application and code problems met during the run\n\nNone." in rendered


def test_the_workflow_definition_comes_from_the_composed_file(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        "workflows:\n"
        "  - task: Implement.\n"
        "    steps:\n"
        "      - work: Work.\n"
        "  - name: review\n"
        "    steps:\n"
        "      - look: Look.\n",
        encoding="utf-8",
    )

    assert workflow_source(tmp_path / "ww.yaml", "task") == {
        "task": "Implement.",
        "steps": [{"work": "Work."}],
    }
    assert workflow_source(tmp_path / "ww.yaml", "review") == {
        "name": "review",
        "steps": [{"look": "Look."}],
    }
    assert workflow_source(tmp_path / "ww.yaml", "missing") is None
    assert workflow_source(tmp_path / "absent.yaml", "task") is None


def test_the_issue_carries_the_record_and_fits_a_prefilled_url(tmp_path: Path) -> None:
    record = _record(tmp_path)

    assert issue_title(record) == "Debug report: `task` run, ww 0.1.0, 1 error"
    body = issue_body(record)
    assert "| Workflow | `task` |" in body
    assert "- **complete refused a valid artifact** (step `work`): ww error" in body
    assert "<details><summary>Workflow definition</summary>" in body
    assert "```yaml\ntask: Implement.\nsteps:\n- work: Work.\n```" in body
    assert body.rstrip().endswith("with `ww debug report`._")

    url = new_issue_url(record)
    assert url.startswith(NEW_ISSUE_URL + "?")
    query = parse_qs(urlparse(url).query)
    assert query["title"] == [issue_title(record)]
    assert query["body"] == [body]

    # Too long with the definition: the definition goes first, then the body.
    long = {**record, "workflow_definition": {"steps": ["x" * 9000]}}
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
        workflow_definition=None,
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
        confirm=lambda record, method: record["id"] == "TASK-1--01-task",
        confirm_submitted=lambda identifier: True,
        show=shown.append,
        gh_available=lambda: True,
        create_issue=create,
        open_browser=lambda url: pytest.fail("no browser with gh"),
    )

    assert [o.to_dict() for o in outcomes] == [
        {
            "id": "TASK-1--01-task",
            "status": "created",
            "url": "https://github.com/x/issues/1",
        },
        {"id": "TASK-2--01-task", "status": "skipped", "url": None},
    ]
    assert len(shown) == 2
    assert shown[0].startswith("## Report `TASK-1--01-task`")
    assert "as a new issue titled `Debug report:" in shown[0]
    assert issue_body(first) in shown[0]
    assert created == [(issue_title(first), issue_body(first))]
    assert (
        store.get("TASK-1--01-task")["reported"]["url"]
        == "https://github.com/x/issues/1"
    )
    assert [r["id"] for r in store.unreported()] == ["TASK-2--01-task"]


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


def test_record_ids_name_the_task_and_run() -> None:
    assert record_id("TASK-1", "01-task") == "TASK-1--01-task"
    # A child's slash becomes a dash, as ``{{ww.task.slug}}`` spells it.
    assert record_id("TASK-42/TASK-42-01A", "03-task") == "TASK-42-TASK-42-01A--03-task"
