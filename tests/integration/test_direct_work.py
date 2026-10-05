# SPDX-License-Identifier: GPL-3.0-or-later
"""Direct work: ``record``, reconciliation, ``status`` and ``lookup``."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ww.cli import main
from ww.direct_work import unseen_commits
from ww.errors import StateError
from ww.service import WorkflowService, _run_windows
from ww.storage import Storage

WORKFLOWS = "workflows:\n  - name: task\n    steps:\n      - develop: Develop.\n"


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, name: str, message: str, when: str | None = None) -> str:
    """Commit ``name``; ``when`` (ISO 8601) sets the commit's date."""
    (root / name).write_text(name, encoding="utf-8")
    _git(root, "add", "-A")
    environment = {**os.environ, "GIT_COMMITTER_DATE": when} if when else None
    subprocess.run(
        ["git", "commit", "-qm", message],
        cwd=root,
        check=True,
        capture_output=True,
        env=environment,
    )
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A repository on ``main`` configured with the ww/git extension."""
    _git(tmp_path, "init", "-q", "-b", "main", ".")
    _git(tmp_path, "config", "user.email", "t@e.st")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (tmp_path / "ww.json").write_text(
        json.dumps(
            {
                "task_format": "TASK-{{digit}}",
                "extensions": {"ww/git": {"base_branches": {"default": "main"}}},
            }
        ),
        encoding="utf-8",
    )
    _commit(tmp_path, "seed.txt", "seed")
    return tmp_path


PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"


def _store(root: Path, name: str, *records: dict[str, object]) -> None:
    path = root / ".ww/ext/ww/git" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _branch(root: Path, task_id: str, branch: str) -> None:
    _git(root, "checkout", "-q", "-b", branch, "main")
    _store(
        root,
        "branches.jsonl",
        {"task_id": task_id, "branch": branch, "base": "main", "recorded_at": "t"},
    )


def _run(
    root: Path, capsys: pytest.CaptureFixture[str], *arguments: str, code: int = 0
) -> str:
    assert main(["--root", str(root), *arguments]) == code
    captured = capsys.readouterr()
    return captured.out if code == 0 else captured.err


def _record(
    root: Path, capsys: pytest.CaptureFixture[str], task_id: str, summary: str
) -> dict[str, object]:
    return json.loads(
        _run(root, capsys, "record", task_id, "--summary", summary, "--json")
    )


def _entries(root: Path, task_id: str) -> list[dict[str, object]]:
    path = root / ".ww/tasks" / task_id / "direct-work.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _start(root: Path, capsys: pytest.CaptureFixture[str], task_id: str) -> None:
    _run(
        root,
        capsys,
        "start",
        task_id,
        "--workflow",
        "task",
        "--agent",
        "claudecode",
        "--requirements",
        "Build it.",
        "--role",
        "manager",
    )


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #


def test_record_finds_the_commits_on_the_tasks_branch_since_its_base(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _branch(project, "TASK-7", "feature/task-7")
    seen = _commit(project, "a.txt", "TASK-7: made by ww")
    first = _commit(project, "b.txt", "Tidy the helper")
    second = _commit(project, "c.txt", "Rename a flag")
    # ww's own commit is known to ww, so it is not direct work.
    _store(project, "commits.jsonl", {"sha": seen, "task_id": "TASK-7"})

    entry = _record(project, capsys, "TASK-7", "Tidied up.")

    assert entry["task_id"] == "TASK-7"
    assert entry["source"] == "agent"
    assert entry["summary"] == "Tidied up."
    assert [commit["sha"] for commit in entry["commits"]] == [first, second]
    assert [commit["subject"] for commit in entry["commits"]] == [
        "Tidy the helper",
        "Rename a flag",
    ]
    assert _entries(project, "TASK-7")[0]["commits"] == entry["commits"]
    # Registered commits are never counted twice.
    again = _record(project, capsys, "TASK-7", "Nothing new.")
    assert again["commits"] == []
    assert len(_entries(project, "TASK-7")) == 2


def test_record_without_a_branch_uses_current_commits_naming_the_task(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(project, "checkout", "-q", "-b", "scratch")
    _commit(project, "a.txt", "Unrelated change")
    mine = _commit(project, "b.txt", "TASK-8: fix the parser")
    _commit(project, "c.txt", "TASK-80: another task")

    entry = _record(project, capsys, "TASK-8", "Fixed the parser.")

    assert [commit["sha"] for commit in entry["commits"]] == [mine]
    # No run exists, yet the task directory was created.
    assert not (project / ".ww/tasks/TASK-8/state.json").exists()
    assert len(_entries(project, "TASK-8")) == 1


def test_record_without_a_branch_or_a_mention_has_an_empty_commit_list(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(project, "a.txt", "Unrelated change")

    entry = _record(project, capsys, "TASK-9", "Edited a file.")

    assert entry["commits"] == []
    assert _entries(project, "TASK-9")[0]["summary"] == "Edited a file."


def test_record_prints_a_line_and_refuses_an_empty_summary(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _run(project, capsys, "record", "TASK-3", "--summary", "Did it.")

    assert text == (
        "Recorded direct work on TASK-3 with 0 commits. "
        "`./ww status TASK-3` lists it.\n"
    )
    assert "record requires a non-empty --summary" in _run(
        project, capsys, "record", "TASK-3", "--summary", " ", code=1
    )
    assert "invalid task ID" in _run(
        project, capsys, "record", "../x", "--summary", "Did it.", code=1
    )


def test_record_is_part_of_the_command_log(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(project, capsys, "TASK-4", "Did it.")

    log = (project / ".ww/executions.jsonl").read_text(encoding="utf-8")
    assert '"command": "record"' in log
    assert '"task_id": "TASK-4"' in log


# --------------------------------------------------------------------------- #
# reconciliation on the manager's page
# --------------------------------------------------------------------------- #


def test_the_manager_page_reconciles_commits_ww_had_not_seen_once(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _branch(project, "TASK-5", "feature/task-5")
    first = _commit(project, "a.txt", "Add the flag", PAST)
    second = _commit(project, "b.txt", "Document the flag", PAST)
    _start(project, capsys, "TASK-5")

    page = _run(project, capsys, "instruction", "TASK-5", "--role", "manager")

    assert page.startswith(
        "ww recorded 2 direct-work commits it had not seen "
        "(see `./ww status TASK-5`).\n\n"
    )
    (entry,) = _entries(project, "TASK-5")
    assert entry["source"] == "reconciled"
    assert entry["summary"] == "Add the flag; Document the flag"
    assert [commit["sha"] for commit in entry["commits"]] == [first, second]

    # Idempotent: the same commits are never recorded again, or announced.
    again = _run(project, capsys, "instruction", "TASK-5", "--role", "manager")
    assert "direct-work commit" not in again
    assert len(_entries(project, "TASK-5")) == 1

    # A commit registered by `record` before the next page is not reconciled.
    third = _commit(project, "c.txt", "Tune it")
    registered = _record(project, capsys, "TASK-5", "Tuned it.")
    assert [commit["sha"] for commit in registered["commits"]] == [third]
    after = _run(project, capsys, "instruction", "TASK-5", "--role", "manager")
    assert "direct-work commit" not in after
    assert [entry["source"] for entry in _entries(project, "TASK-5")] == [
        "reconciled",
        "agent",
    ]


def test_commits_made_during_an_unfinished_run_are_not_reconciled(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _branch(project, "TASK-9", "feature/task-9")
    before = _commit(project, "a.txt", "Before the run", PAST)
    _start(project, capsys, "TASK-9")
    _commit(project, "b.txt", "Made by a worker inside the run", FUTURE)

    page = _run(project, capsys, "instruction", "TASK-9", "--role", "manager")

    assert "1 direct-work commit " in page
    (entry,) = _entries(project, "TASK-9")
    assert [commit["sha"] for commit in entry["commits"]] == [before]


def _window(start: str, end: str | None) -> tuple[datetime, datetime | None]:
    return (
        datetime.fromisoformat(start),
        datetime.fromisoformat(end) if end else None,
    )


def test_a_completed_runs_window_ends_and_commits_between_runs_are_reconciled(
    project: Path,
) -> None:
    _branch(project, "TASK-9", "feature/task-9")
    config = WorkflowService(Storage(project)).extensions.config
    inside = _commit(project, "a.txt", "Inside the run", "2025-01-01T12:00:00+00:00")
    between = _commit(project, "b.txt", "Between runs", "2025-02-01T00:00:00+00:00")
    windows = [
        _window("2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00"),
        _window("2025-03-01T00:00:00+00:00", None),
    ]

    found = unseen_commits(project, config, "TASK-9", (), windows)

    assert [commit.sha for commit in found] == [between]
    assert inside not in [commit.sha for commit in found]


def test_a_run_is_a_window_until_it_is_completed_or_abandoned(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys, "TASK-9")
    service = WorkflowService(Storage(project))
    (run,), _, _ = service.tasks.read_task_record("TASK-9")
    start = datetime.fromisoformat(run.state.created_at)
    end = "2030-01-01T00:00:00+00:00"

    assert _run_windows((run,)) == [(start, None)]
    for status in ("completed", "abandoned"):
        closed = replace(run, state=replace(run.state, status=status, updated_at=end))
        assert _run_windows((closed,)) == [(start, datetime.fromisoformat(end))]
    assert datetime.now(timezone.utc) > start


def test_run_windows_parse_ww_z_timestamps_and_skip_commits_inside(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _branch(project, "TASK-9", "feature/task-9")
    _start(project, capsys, "TASK-9")
    service = WorkflowService(Storage(project))
    (run,), _, _ = service.tasks.read_task_record("TASK-9")
    closed = replace(
        run,
        state=replace(
            run.state,
            status="completed",
            created_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-02T00:00:00Z",
        ),
    )

    windows = _run_windows((closed,))

    assert windows == [
        (
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 1, 2, tzinfo=timezone.utc),
        )
    ]
    config = service.extensions.config
    inside = _commit(project, "a.txt", "Inside the run", "2025-01-01T12:00:00+00:00")
    found = unseen_commits(project, config, "TASK-9", (), windows)
    assert inside not in [commit.sha for commit in found]


def test_only_the_manager_page_reconciles_and_json_stays_clean(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _branch(project, "TASK-6", "feature/task-6")
    _commit(project, "a.txt", "Add the flag", PAST)
    _start(project, capsys, "TASK-6")

    operator = _run(project, capsys, "instruction", "TASK-6")
    assert "direct-work commit" not in operator
    assert not (project / ".ww/tasks/TASK-6/direct-work.json").exists()

    as_json = _run(
        project, capsys, "instruction", "TASK-6", "--role", "manager", "--json"
    )
    assert json.loads(as_json)["task_id"] == "TASK-6"
    # JSON consumers get no extra line, but the commit was recorded.
    assert len(_entries(project, "TASK-6")) == 1


def test_a_task_ww_does_not_hold_is_not_reconciled(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _branch(project, "TASK-2", "feature/task-2")
    _commit(project, "a.txt", "Add the flag")

    service = WorkflowService(Storage(project))

    assert service.reconcile_direct_work("TASK-2") == 0
    assert not (project / ".ww/tasks/TASK-2").exists()
    with pytest.raises(StateError, match="invalid task ID"):
        service.reconcile_direct_work("../x")


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def test_status_lists_the_direct_work_of_a_task_with_a_run(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _branch(project, "TASK-5", "feature/task-5")
    _start(project, capsys, "TASK-5")
    plain = _run(project, capsys, "status", "TASK-5", "--json")
    assert "direct_work" not in json.loads(plain)
    assert "direct work" not in _run(project, capsys, "status", "TASK-5")
    sha = _commit(project, "a.txt", "Add the flag")
    _record(project, capsys, "TASK-5", "Added the flag.")
    _record(project, capsys, "TASK-5", "Looked around.")

    text = _run(project, capsys, "status", "TASK-5")
    report = json.loads(_run(project, capsys, "status", "TASK-5", "--json"))

    assert "workflow: task\n" in text
    assert (
        "direct work: 2\n"
        f"- Added the flag. (agent, {sha[:7]})\n"
        "- Looked around. (agent, no commits)\n"
    ) in text
    assert [entry["summary"] for entry in report["direct_work"]] == [
        "Added the flag.",
        "Looked around.",
    ]
    assert report["direct_work"][0]["commits"][0]["sha"] == sha
    assert report["workflow"] == "task"


def test_status_of_a_task_with_only_direct_work(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(project, capsys, "TASK-11", "Edited the docs.")

    text = _run(project, capsys, "status", "TASK-11")

    assert "task id: TASK-11\n" in text
    assert "step state: direct work only\n" in text
    assert "direct work: 1\n- Edited the docs. (agent, no commits)\n" in text


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #


def _lookup(
    root: Path, capsys: pytest.CaptureFixture[str], *arguments: str
) -> dict[str, object]:
    return json.loads(_run(root, capsys, "lookup", *arguments, "--json"))


def test_lookup_answers_work_directly_then_record_for_a_known_task(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _record(project, capsys, "TASK-12", "Earlier work.")

    report = _lookup(project, capsys, "12", "--agent", "codex")

    assert report["outcome"] == "direct"
    assert report["task_id"] == "TASK-12"
    assert report["command"] == './ww record TASK-12 --summary "<what was done>"'
    assert "Work directly" in str(report["message"])
    assert "do not retry it endlessly" in str(report["message"])
    assert "choices" not in report

    page = _run(project, capsys, "lookup", "12", "--agent", "codex")
    assert "When the work is done, register it:" in page
    assert '```console\n./ww record TASK-12 --summary "<what was done>"\n```' in page


def test_lookup_sends_a_change_to_the_tasks_unfinished_run(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys, "TASK-7")

    report = _lookup(project, capsys, "7", "--agent", "codex")

    assert report["outcome"] == "continue"
    assert report["command"] == "./ww instruction TASK-7 --role manager"


def test_lookup_never_asks_before_a_never_seen_task(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _lookup(project, capsys, "99", "--agent", "codex")

    assert report["outcome"] == "direct"
    assert report["task_id"] == "TASK-99"
    assert report["command"] == './ww record TASK-99 --summary "<what was done>"'
    # lookup is read-only: nothing exists until `record` runs.
    assert not (project / ".ww/tasks/TASK-99").exists()


def test_lookup_lets_the_operator_pick_between_matching_tasks(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "ww.json").write_text(
        json.dumps({"task_format": "explicit"}), encoding="utf-8"
    )
    _record(project, capsys, "OPS-7", "One.")
    _start(project, capsys, "WEB-7")

    report = _lookup(project, capsys, "7", "--agent", "kimi")

    assert report["outcome"] == "choose"
    assert report["choice_mechanism"] == "plain text"
    assert [choice["label"] for choice in report["choices"]] == [
        "Use OPS-7",
        "Use WEB-7",
    ]
    assert [choice["command"] for choice in report["choices"]] == [
        './ww record OPS-7 --summary "<what was done>"',
        "./ww instruction WEB-7 --role manager",
    ]
    page = _run(project, capsys, "lookup", "7", "--agent", "kimi")
    assert "A `record` command comes after the work it registers." in page


def test_lookup_without_a_task_assigns_an_id_or_asks_for_the_key(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    generated = _lookup(project, capsys, "--agent", "gemini")

    assert generated["outcome"] == "direct"
    assert generated["task_id"] == "TASK-1"
    assert generated["command"] == './ww record TASK-1 --summary "<what was done>"'

    (project / "ww.json").write_text(
        json.dumps({"task_format": "explicit"}), encoding="utf-8"
    )
    explicit = _lookup(project, capsys, "--agent", "gemini")

    assert explicit["outcome"] == "direct"
    assert explicit["task_id"] == "<task-id>"
    assert explicit["command"] == './ww record <task-id> --summary "<what was done>"'
    assert "requires an explicit task ID" in str(explicit["message"])


# --------------------------------------------------------------------------- #
# a stale catchall entry
# --------------------------------------------------------------------------- #


def test_lint_warns_about_a_stale_catchall_entry_without_failing(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = _run(project, capsys, "lint")
    assert "catchall" not in clean
    (project / "ww.json").write_text(
        json.dumps({"workflows": {"catchall": {"enabled": False}}}), encoding="utf-8"
    )

    output = _run(project, capsys, "lint")

    assert "ww.yaml is valid." in output
    assert (
        "Warning: ww.json lists the workflow 'catchall' under workflows, which "
        "ww no longer provides; the entry is ignored and can be removed.\n"
    ) in output
    # Every other command keeps working with the stale entry.
    assert "## Direct work" in _run(project, capsys, "discover")
    assert _lookup(project, capsys, "--agent", "codex")["outcome"] == "direct"
