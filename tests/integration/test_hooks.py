# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent hooks as an agent calls them: context, a stop reminder, interruptions."""

from __future__ import annotations

import io
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.errors import StateError
from ww.hooks import HookRecords, answer_hook, hook_agent
from ww.hooks.transcripts import recover_conversation
from ww.open_work import open_work
from ww.project_config import AgentHooks
from ww.service import WorkflowService
from ww.storage import Storage
from ww.storage_adapters.memory import MemoryTaskStorageAdapter

WORKFLOWS = """workflows:
  - name: task
    steps:
      - develop: Implement it.
      - review: Review it.
"""


class _Payload(io.StringIO):
    def isatty(self) -> bool:
        return False


def _root(tmp_path: Path, settings: dict | None = None) -> Path:
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (tmp_path / "ww.json").write_text(json.dumps(settings or {}), encoding="utf-8")
    return tmp_path


def _hook(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    event: str,
    agent: str = "claudecode",
    payload: dict | None = None,
) -> str:
    monkeypatch.setattr("sys.stdin", _Payload(json.dumps(payload or {})))
    capsys.readouterr()
    assert main(["--root", str(root), "hook", event, "--agent", agent]) == 0
    return capsys.readouterr().out


def _context(output: str) -> Any:
    return json.loads(output)["hookSpecificOutput"]["additionalContext"]


def _in_progress(root: Path, task_id: str = "T1", **options: object) -> WorkflowService:
    service = WorkflowService(Storage(root))
    start_after_init(service, "task", task_id, agent="claudecode", **options)
    service.next(task_id)
    return service


def _hook_log(root: Path) -> list[dict]:
    lines = (root / ".ww/executions.jsonl").read_text(encoding="utf-8").splitlines()
    return [
        record for line in lines if (record := json.loads(line))["command"] == "hook"
    ]


# session-start


def test_session_start_without_tasks_prints_only_the_reminder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))

    assert context == (
        "This project coordinates work through ww: `./ww discover` lists its workflows."
    )


def test_session_start_under_on_request_says_ww_is_used_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, {"enabled": "on_request"})
    _in_progress(root)

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))
    reminder = json.loads(_hook(root, monkeypatch, capsys, "stop"))

    assert context.startswith(
        "This project has ww available on request only: use it only when the "
        "user explicitly asks for ww; otherwise work without it and do not ask. "
        "`./ww discover` lists its workflows."
    )
    assert "coordinates work through ww" not in context
    # Tasks already open are still listed and still reminded about.
    assert "- T1 (task, claudecode) develop" in context
    assert "T1 step `develop` is still in progress" in reminder["reason"]


def test_session_start_lists_unfinished_tasks_with_resume_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root)

    lines = _context(
        _hook(root, monkeypatch, capsys, "session-start", payload={"source": "startup"})
    ).splitlines()

    assert lines[1] == "Unfinished ww tasks, newest first:"
    assert lines[2] == (
        "- T1 (task, claudecode) develop: in progress · in the root · resume: "
        "`./ww instruction T1 --role manager` · worker: "
        "`./ww instruction T1 --run 01-task --role worker`"
    )


def test_session_start_after_compaction_says_state_is_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)

    context = _context(
        _hook(root, monkeypatch, capsys, "session-start", payload={"source": "compact"})
    )

    assert context.startswith(
        "Context was compacted; ww's task state is authoritative."
    )


def test_session_start_caps_the_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    service = WorkflowService(Storage(root))
    for index in range(7):
        start_after_init(service, "task", f"T{index}", agent="claudecode")

    lines = _context(_hook(root, monkeypatch, capsys, "session-start")).splitlines()

    assert sum(line.startswith("- T") for line in lines) == 5
    assert lines[-2:] == [
        "… and 2 more; `./ww status <task-id>` shows one.",
        "`./ww discover` lists every unfinished task.",
    ]


def test_a_task_awaiting_the_operator_stays_visible_and_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    service = _in_progress(root)
    service.fail("T1", "could not build")

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))

    assert (
        "- T1 (task, claudecode) develop: awaiting the operator: the work failed"
        in context
    )
    assert "worker:" not in context


def test_antigravity_gets_context_on_the_first_invocation_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)

    first = _hook(
        root, monkeypatch, capsys, "session-start", "antigravity", {"invocationNum": 0}
    )
    later = _hook(
        root, monkeypatch, capsys, "session-start", "antigravity", {"invocationNum": 2}
    )

    assert json.loads(first)["injectSteps"][0]["ephemeralMessage"].startswith(
        "This project coordinates work through ww"
    )
    assert later == ""


# stop


def test_stop_reminds_once_per_step_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root)

    first = json.loads(_hook(root, monkeypatch, capsys, "stop"))
    second = _hook(root, monkeypatch, capsys, "stop")

    assert first["decision"] == "block"
    assert "T1 step `develop` is still in progress" in first["reason"]
    assert "./ww complete T1 --role worker" in first["reason"]
    assert "./ww fail T1 --role worker" in first["reason"]
    assert second == ""


def test_stop_allows_once_the_agent_continued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root)

    assert (
        _hook(root, monkeypatch, capsys, "stop", payload={"stop_hook_active": True})
        == ""
    )
    # The loop guard answered, so the one reminder is still owed.
    assert _hook(root, monkeypatch, capsys, "stop") != ""


@pytest.mark.parametrize("state", ["not dispatched", "completed", "awaiting operator"])
def test_stop_allows_when_no_agent_step_is_in_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    root = _root(tmp_path)
    service = WorkflowService(Storage(root))
    start_after_init(service, "task", "T1", agent="claudecode")
    if state == "completed":
        instruction = service.next("T1")
        while instruction.status != "completed":
            if instruction.item_status == "pending":
                instruction = service.next("T1")
                continue
            instruction = service.complete(
                "T1",
                variables=tuple(
                    (value.name, "done") for value in instruction.required_values
                ),
                artifact="done",
                summary_for_next="Done.",
            )
    elif state == "awaiting operator":
        service.next("T1")
        service.fail("T1", "broken")

    assert _hook(root, monkeypatch, capsys, "stop") == ""


def test_a_new_attempt_is_reminded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    service = _in_progress(root)
    assert _hook(root, monkeypatch, capsys, "stop") != ""
    service.complete("T1", artifact="done", summary_for_next="Done.")
    service.next("T1")

    reply = json.loads(_hook(root, monkeypatch, capsys, "stop"))

    assert "T1 step `review`" in reply["reason"]


def test_cursor_is_reminded_through_a_follow_up_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _agent_task(root, "T1", "cursor")

    reply = json.loads(
        _hook(
            root,
            monkeypatch,
            capsys,
            "stop",
            "cursor",
            {"status": "completed", "loop_count": 0, "workspace_roots": [str(root)]},
        )
    )

    assert "is still in progress" in reply["followup_message"]


# Interruptions


def test_an_interrupt_marks_the_task_and_every_task_command_shows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    service = _in_progress(root)

    assert (
        _hook(
            root,
            monkeypatch,
            capsys,
            "interrupt",
            payload={"reason": "prompt_input_exit"},
        )
        == ""
    )

    marker = json.loads((root / ".ww/tasks/T1/interrupted.json").read_text())
    assert marker["step"] == "develop"
    assert marker["attempt"] == 1
    assert (marker["agent"], marker["reason"]) == ("claudecode", "prompt_input_exit")
    for command in (["status", "T1"], ["instruction", "T1", "--role", "manager"]):
        assert main(["--root", str(root), *command]) == 0
        output = capsys.readouterr().out
        assert output.startswith(
            "> Interrupted: the previous session (claudecode, prompt_input_exit)"
        )
        assert "check `git status` in the task workspace" in output
    context = _context(_hook(root, monkeypatch, capsys, "session-start"))
    assert "  Interrupted: the previous session" in context

    # Shown several times, the notice stays until the attempt is closed.
    service.complete("T1", artifact="done", summary_for_next="Done.")
    assert main(["--root", str(root), "status", "T1"]) == 0
    assert "Interrupted" not in capsys.readouterr().out
    assert not (root / ".ww/tasks/T1/interrupted.json").exists()


def test_an_interrupt_without_a_step_in_progress_marks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    start_after_init(WorkflowService(Storage(root)), "task", "T1", agent="claudecode")

    _hook(root, monkeypatch, capsys, "interrupt")

    assert not (root / ".ww/tasks/T1/interrupted.json").exists()


def test_an_aborted_cursor_stop_is_an_interruption_not_a_reminder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _agent_task(root, "T1", "cursor")

    reply = _hook(root, monkeypatch, capsys, "stop", "cursor", {"status": "aborted"})

    assert reply == ""
    marker = json.loads((root / ".ww/tasks/T1/interrupted.json").read_text())
    assert (marker["agent"], marker["reason"]) == ("cursor", "aborted")


def test_a_session_in_a_task_workspace_marks_only_that_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, {"projects": [{"name": "backend", "path": "./backend"}]})
    (root / "backend").mkdir()
    _in_progress(root, "IN-ROOT")
    _in_progress(root, "IN-BACKEND", project="backend")

    _hook(
        root, monkeypatch, capsys, "interrupt", payload={"cwd": str(root / "backend")}
    )
    assert (root / ".ww/tasks/IN-BACKEND/interrupted.json").exists()
    assert not (root / ".ww/tasks/IN-ROOT/interrupted.json").exists()

    # A session nowhere in particular concerns its own agent's tasks.
    _hook(root, monkeypatch, capsys, "interrupt", payload={"cwd": "/elsewhere"})
    assert (root / ".ww/tasks/IN-ROOT/interrupted.json").exists()
    _hook(root, monkeypatch, capsys, "interrupt", "codex", {"cwd": "/elsewhere"})
    marker = json.loads((root / ".ww/tasks/IN-ROOT/interrupted.json").read_text())
    assert marker["agent"] == "claudecode"


def test_a_later_interrupt_overwrites_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root)

    _hook(
        root,
        monkeypatch,
        capsys,
        "interrupt",
        "codex",
        {"hook_event_name": "Interrupt"},
    )
    _hook(root, monkeypatch, capsys, "interrupt", payload={"reason": "logout"})

    marker = json.loads((root / ".ww/tasks/T1/interrupted.json").read_text())
    assert (marker["agent"], marker["reason"]) == ("claudecode", "logout")


def test_reset_forgets_the_hook_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    service = _in_progress(root)
    _hook(root, monkeypatch, capsys, "stop")
    _hook(root, monkeypatch, capsys, "interrupt")

    service.reset("T1")

    assert not (root / ".ww/tasks/T1").exists()


def test_interrupted_lists_recent_interruptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root, "T1")
    _hook(root, monkeypatch, capsys, "interrupt", payload={"reason": "other"})
    _in_progress(root, "OLD")
    old = root / ".ww/tasks/OLD/interrupted.json"
    old.write_text(
        json.dumps(
            {
                "at": "2020-01-01T00:00:00Z",
                "run_id": "01-task",
                "step": "develop",
                "item_id": json.loads(
                    (root / ".ww/tasks/T1/interrupted.json").read_text()
                )["item_id"],
                "attempt": 1,
                "agent": "codex",
            }
        ),
        encoding="utf-8",
    )

    assert main(["--root", str(root), "interrupted"]) == 0
    recent = capsys.readouterr().out
    assert main(["--root", str(root), "interrupted", "--all", "--json"]) == 0
    everything = json.loads(capsys.readouterr().out)

    assert recent.startswith("- T1 · develop (attempt 1) · ")
    assert "OLD" not in recent
    assert [entry["task_id"] for entry in everything] == ["T1", "OLD"]


def test_discover_and_lookup_point_at_recent_interruptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    pointer = "run `./ww interrupted` before starting new work."
    assert main(["--root", str(root), "discover"]) == 0
    assert pointer not in capsys.readouterr().out

    _in_progress(root)
    _hook(root, monkeypatch, capsys, "interrupt")

    assert main(["--root", str(root), "discover"]) == 0
    assert (
        "1 task was interrupted in the last 3 days; " + pointer
        in capsys.readouterr().out
    )
    assert main(["--root", str(root), "discover", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["interrupted_recently"] == 1
    assert main(["--root", str(root), "lookup", "--agent", "claudecode"]) == 0
    assert pointer in capsys.readouterr().out


# Robustness and the audit log


def test_every_hook_call_is_logged_with_ww_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root)
    _hook(root, monkeypatch, capsys, "session-start")
    _hook(root, monkeypatch, capsys, "stop")
    _hook(root, monkeypatch, capsys, "stop")

    decisions = [
        (record["hook_event"], record["hook_agent"], record["hook_decision"])
        for record in _hook_log(root)
    ]

    assert decisions == [
        ("session-start", "claudecode", "context with 1 unfinished task(s)"),
        ("stop", "claudecode", "reminded: T1"),
        ("stop", "claudecode", "allowed"),
    ]


def test_a_hook_never_fails_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    # Outside a ww root, and with arguments ww does not know, a hook is silent.
    monkeypatch.setattr("sys.stdin", _Payload("{not json"))
    assert (
        main(["--root", str(tmp_path / "nowhere"), "hook", "stop", "--agent", "codex"])
        == 0
    )
    assert main(["--root", str(root), "hook", "stop", "--agent", "someone"]) == 0
    (root / "ww.json").write_text(json.dumps({"enabled": "maybe"}), encoding="utf-8")
    assert _hook(root, monkeypatch, capsys, "session-start") == ""
    assert _hook_log(root)[-1]["outcome"] == "error"
    assert capsys.readouterr().err == ""


# Unreadable tasks


def _unreadable(root: Path, task_id: str = "BAD") -> None:
    """Leave ``task_id`` with a state file ww cannot read, marked interrupted."""
    start_after_init(
        WorkflowService(Storage(root)), "task", task_id, agent="claudecode"
    )
    directory = root / ".ww/tasks" / task_id
    (directory / "state.json").write_text("{broken", encoding="utf-8")
    (directory / "interrupted.json").write_text(
        json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "run_id": "01-task",
                "step": "develop",
                "item_id": "develop",
                "attempt": 1,
                "agent": "claudecode",
            }
        ),
        encoding="utf-8",
    )


def test_an_unreadable_task_leaves_discover_and_hooks_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root)
    _hook(root, monkeypatch, capsys, "interrupt")
    _unreadable(root)

    assert main(["--root", str(root), "discover"]) == 0
    output = capsys.readouterr().out
    assert "## Unreadable tasks" in output
    assert "- `BAD` — invalid task state " in output
    assert "Other tasks and new work are unaffected." in output
    assert "1 task was interrupted in the last 3 days" in output
    assert main(["--root", str(root), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [task["task_id"] for task in report["unreadable_tasks"]] == ["BAD"]
    assert report["unreadable_tasks"][0]["reason"].startswith("invalid task state ")
    assert report["interrupted_recently"] == 1

    lines = _context(_hook(root, monkeypatch, capsys, "session-start")).splitlines()
    assert lines[2].startswith("- T1 (task, claudecode) develop: in progress")
    assert lines[3].startswith("  Interrupted: the previous session")
    assert lines[-1] == (
        "ww cannot read the state of BAD; other tasks and new work are "
        "unaffected. `./ww discover` shows why; ask the operator before "
        "touching them."
    )

    reminder = json.loads(_hook(root, monkeypatch, capsys, "stop"))
    assert "T1 step `develop` is still in progress" in reminder["reason"]
    assert "BAD" not in reminder["reason"]
    assert main(["--root", str(root), "lookup", "--agent", "claudecode"]) == 0
    capsys.readouterr()
    assert main(["--root", str(root), "interrupted"]) == 0
    assert capsys.readouterr().out.startswith("- T1 · develop")

    # The task itself keeps failing with the exact error.
    assert main(["--root", str(root), "status", "BAD"]) == 1
    error = capsys.readouterr().err
    assert "invalid task state " in error
    assert str(root / ".ww/tasks/BAD/state.json") in error


def test_discover_without_unreadable_tasks_has_no_such_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root)

    assert main(["--root", str(root), "discover"]) == 0
    assert "Unreadable tasks" not in capsys.readouterr().out
    assert main(["--root", str(root), "discover", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["unreadable_tasks"] == []


def test_an_unreadable_child_task_is_named_with_its_full_id(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _in_progress(root)
    child = root / ".ww/tasks/T1/T1.1"
    child.mkdir(parents=True)
    (child / "state.json").write_text("{broken", encoding="utf-8")

    storage = Storage(root)
    work = open_work(storage.task_persistence, root)

    assert [task.task_id for task in work.tasks] == ["T1"]
    assert [task.task_id for task in work.unreadable] == ["T1/T1.1"]


def test_the_memory_adapter_reports_an_unreadable_task_the_same_way(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    persistence = MemoryTaskStorageAdapter()
    service = WorkflowService(Storage(root), persistence)
    for task_id in ("GOOD", "BAD"):
        start_after_init(service, "task", task_id, agent="claudecode")
    # Another task's runs: the identities no longer match, as after a bad write.
    persistence.aggregates["BAD"] = persistence.aggregates["GOOD"]

    with pytest.raises(StateError, match="^invalid task state BAD: "):
        persistence.read_task_record("BAD")
    work = open_work(persistence, root)
    assert [task.task_id for task in work.tasks] == ["GOOD"]
    assert [task.task_id for task in work.unreadable] == ["BAD"]
    assert work.unreadable[0].reason.startswith("invalid task state BAD: ")

    marker = Storage(root).runtime_path / "tasks/BAD/interrupted.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "run_id": "01-task",
                "item_id": "develop",
                "attempt": 1,
                "agent": "claudecode",
            }
        ),
        encoding="utf-8",
    )
    assert HookRecords(Storage(root), persistence).recent(3) == ()


# Setup commands and init


def test_show_install_and_uninstall_from_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)

    assert main(["--root", str(root), "hook", "show", "--agent", "codex"]) == 0
    shown = capsys.readouterr().out
    assert main(["--root", str(root), "hook", "install", "--agent", "codex"]) == 0
    installed = capsys.readouterr().out
    assert main(["--root", str(root), "hook", "install", "--agent", "codex"]) == 0
    again = capsys.readouterr().out
    assert main(["--root", str(root), "hook", "uninstall", "--agent", "codex"]) == 0
    removed = capsys.readouterr().out

    assert shown.startswith("ww's hooks for codex belong in .codex/hooks.json:")
    assert installed == "Installed ww's hooks for codex in .codex/hooks.json.\n"
    assert again == "ww's hooks are already installed for codex in .codex/hooks.json.\n"
    assert removed == "Removed ww's hooks for codex in .codex/hooks.json.\n"
    assert not (root / ".ww/executions.jsonl").exists() or all(
        record["hook_event"] for record in _hook_log(root) if "hook_event" in record
    )


def test_init_installs_hooks_for_the_agents_set_up_here(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/hooks.json").write_text("not json", encoding="utf-8")

    assert main(["--root", str(tmp_path), "init", "--no-input", "--hooks"]) == 0
    output = capsys.readouterr().out

    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert "SessionStart" in settings["hooks"]
    assert ".claude/settings.json (added ww hooks)" in output
    assert "Add ww's hooks for cursor by hand" in output
    assert "./ww hook stop --agent cursor" in output
    assert not (tmp_path / ".codex/hooks.json").exists()

    choices = json.loads((tmp_path / ".ww/init-choices.json").read_text())
    assert choices["hooks"]["claudecode"] is True
    before = (tmp_path / ".claude/settings.json").read_text()
    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    assert (tmp_path / ".claude/settings.json").read_text() == before


def test_init_leaves_hooks_alone_without_the_flag_or_a_choice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".codex").mkdir()

    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    assert not (tmp_path / ".codex/hooks.json").exists()
    assert main(["--root", str(tmp_path), "init", "--no-input", "--no-hooks"]) == 0
    assert not (tmp_path / ".codex/hooks.json").exists()
    choices = json.loads((tmp_path / ".ww/init-choices.json").read_text())
    assert choices["hooks"] == {"codex": False}


def test_init_asks_once_per_agent_and_remembers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    answers = iter(["", "n"])
    tty_type = type("Tty", (), {"isatty": lambda self: True})
    monkeypatch.setattr("ww.cli.initialization.sys.stdin", tty_type())
    monkeypatch.setattr(sys.modules["ww.cli.main"], "_initialization_options", _options)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert main(["--root", str(tmp_path), "init"]) == 0

    assert (tmp_path / ".claude/settings.json").is_file()
    assert not (tmp_path / ".codex/hooks.json").exists()
    choices = json.loads((tmp_path / ".ww/init-choices.json").read_text())
    assert choices["hooks"] == {"claudecode": True, "codex": False}


def _options(storage: Storage, args: object) -> tuple:
    return (
        "workflows:\n  - task: Task.\n    steps:\n      - work: Work.\n",
        "{}\n",
        True,
        (),
    )


def test_install_local_and_the_duplicate_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    base = ["--root", str(root), "hook"]

    assert main([*base, "install", "--agent", "claudecode"]) == 0
    capsys.readouterr()
    assert main([*base, "install", "--agent", "claudecode", "--local"]) == 0
    output = capsys.readouterr().out
    assert main([*base, "show", "--agent", "claudecode", "--local"]) == 0
    shown = capsys.readouterr().out

    assert output.startswith(
        "Installed ww's hooks for claudecode in .claude/settings.local.json.\n"
    )
    assert (
        "Notice: .claude/settings.json also registers ww's hooks, so every hook "
        "runs twice. Remove one copy with `./ww hook uninstall --agent claudecode`."
    ) in output
    assert shown.startswith(
        "ww's hooks for claudecode belong in .claude/settings.local.json:"
    )
    assert main([*base, "install", "--agent", "cursor", "--local"]) == 1
    assert "cursor has no project-local hooks file" in capsys.readouterr().err


def test_parallel_stop_calls_remind_exactly_once(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _in_progress(root)
    storage = Storage(root)
    agent = hook_agent("claudecode")

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(
            pool.map(
                lambda _: answer_hook(
                    storage, agent, "stop", "{}", settings=AgentHooks()
                ),
                range(8),
            )
        )

    assert sum(answer.text != "" for answer in answers) == 1


# Which tasks a session's stop and interrupt concern


def _agent_task(root: Path, task_id: str, agent: str, **options: object) -> None:
    service = WorkflowService(Storage(root))
    start_after_init(service, "task", task_id, agent=agent, **options)
    service.next(task_id)


def test_a_root_session_ignores_another_agents_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, {"projects": [{"name": "backend", "path": "./backend"}]})
    (root / "backend").mkdir()
    _agent_task(root, "CODEX-ROOT", "codex")
    _agent_task(root, "CODEX-BACKEND", "codex", project="backend")

    for payload in ({"cwd": str(root)}, {}, {"cwd": "/elsewhere"}):
        assert _hook(root, monkeypatch, capsys, "stop", payload=payload) == ""
        _hook(root, monkeypatch, capsys, "interrupt", payload=payload)

    assert not list((root / ".ww/tasks").rglob("interrupted.json"))


def test_a_root_session_is_reminded_about_its_own_agents_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, {"projects": [{"name": "backend", "path": "./backend"}]})
    (root / "backend").mkdir()
    _agent_task(root, "MINE", "claudecode")
    _agent_task(root, "MINE-BACKEND", "claudecode", project="backend")
    _agent_task(root, "THEIRS", "codex")

    reply = json.loads(
        _hook(root, monkeypatch, capsys, "stop", payload={"cwd": str(root)})
    )

    assert "MINE step" in reply["reason"]
    assert "MINE-BACKEND step" in reply["reason"]
    assert "THEIRS" not in reply["reason"]


def test_a_session_inside_another_agents_workspace_is_reminded_and_marks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, {"projects": [{"name": "backend", "path": "./backend"}]})
    (root / "backend").mkdir()
    _agent_task(root, "CODEX-BACKEND", "codex", project="backend")
    _agent_task(root, "MINE", "claudecode")
    inside = {"cwd": str(root / "backend")}

    reply = json.loads(_hook(root, monkeypatch, capsys, "stop", payload=inside))
    _hook(root, monkeypatch, capsys, "interrupt", payload=inside)

    assert "CODEX-BACKEND step" in reply["reason"]
    assert "MINE" not in reply["reason"].replace("CODEX-BACKEND", "")
    assert (root / ".ww/tasks/CODEX-BACKEND/interrupted.json").exists()
    assert not (root / ".ww/tasks/MINE/interrupted.json").exists()


def test_session_start_names_each_tasks_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _agent_task(root, "THEIRS", "codex")

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))

    assert "- THEIRS (task, codex) develop: in progress" in context


# Delegated steps: the manager waits, the worker holds them


def _delegated(root: Path, own_step: bool = False) -> WorkflowService:
    (root / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - develop: Implement it.\n"
        + ("        role: manager\n" if own_step else ""),
        encoding="utf-8",
    )
    service = WorkflowService(Storage(root))
    start_after_init(service, "task", "T1", agent="claudecode", workflow_runtime="auto")
    service.next("T1")
    return service


def test_the_managers_stop_skips_a_step_delegated_to_a_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _delegated(root)

    assert _hook(root, monkeypatch, capsys, "stop") == ""
    worker = json.loads(
        _hook(
            root,
            monkeypatch,
            capsys,
            "stop",
            payload={"hook_event_name": "SubagentStop"},
        )
    )

    assert "T1 step `develop` is still in progress" in worker["reason"]


def test_the_managers_stop_reminds_about_a_step_it_performs_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _delegated(root, own_step=True)

    assert _hook(root, monkeypatch, capsys, "stop") != ""


@pytest.mark.parametrize(
    ("agent", "worker_event"),
    [("codex", "SubagentStop"), ("cursor", "subagentStop")],
)
def test_other_agents_tell_a_workers_stop_apart(agent: str, worker_event: str) -> None:
    parsed = hook_agent(agent).parse("stop", {"hook_event_name": worker_event})
    main_stop = hook_agent(agent).parse("stop", {"hook_event_name": "Stop"})

    assert parsed.from_worker is True
    assert main_stop.from_worker is False


# interactive steps


INTERACTIVE_WORKFLOWS = """workflows:
  - name: interview
    steps:
      - name: talk
        description: Interview the operator.
        interactive: true
"""


def _in_conversation(root: Path, agent: str = "claudecode") -> WorkflowService:
    (root / "ww.yaml").write_text(INTERACTIVE_WORKFLOWS, encoding="utf-8")
    service = WorkflowService(Storage(root))
    start_after_init(service, "interview", "T1", agent=agent)
    service.next("T1")
    return service


def _jsonl(path: Path, *lines: object) -> Path:
    """A transcript file; a ``str`` line is written as it is, malformed or not."""
    path.write_text(
        "".join(
            (line if isinstance(line, str) else json.dumps(line)) + "\n"
            for line in lines
        ),
        encoding="utf-8",
    )
    return path


# Before the step's attempt started, and after it.
OLD = "2020-01-01T00:00:00.000Z"
NEW = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()


def _claude(kind: str, content: object, at: str = NEW, **flags: object) -> dict:
    return {"type": kind, "timestamp": at, "message": {"content": content}, **flags}


def test_stop_is_silent_while_an_interactive_step_waits_for_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    service = _in_conversation(root)

    # The agent stops to let the operator answer: nothing is left open.
    assert _hook(root, monkeypatch, capsys, "stop") == ""
    service.interact("T1", agent="Anything else?", caller_role="manager")
    assert _hook(root, monkeypatch, capsys, "stop") == ""

    # Once the conversation has ended, the step is open work like any other.
    service.interact("T1", end=True, caller_role="manager")
    reminder = json.loads(_hook(root, monkeypatch, capsys, "stop"))
    assert "T1 step `talk` is still in progress" in reminder["reason"]


def test_an_interrupt_without_a_transcript_says_the_conversation_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_conversation(root)

    assert (
        _hook(root, monkeypatch, capsys, "interrupt", payload={"reason": "exit"}) == ""
    )

    marker = json.loads((root / ".ww/tasks/T1/interrupted.json").read_text())
    assert (marker["in_conversation"], marker["recovered_entries"]) == (True, 0)
    assert not (root / ".ww/tasks/T1/interactions.md").exists()
    assert main(["--root", str(root), "instruction", "T1", "--role", "manager"]) == 0
    output = capsys.readouterr().out
    assert "while `talk` (attempt 1) was talking with the operator" in output
    assert "The conversation was not recorded; ask the operator where you were" in (
        output
    )
    assert "git status" not in output


def test_an_interrupt_recovers_the_conversation_from_a_claude_code_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_conversation(root)
    transcript = _jsonl(
        tmp_path / "session.jsonl",
        _claude("user", "An earlier step's request.", at=OLD),
        {"type": "file-history-snapshot", "timestamp": NEW},
        _claude("user", "Hook output.", isMeta=True),
        _claude("user", "<command-name>/clear</command-name>"),
        _claude("assistant", [{"type": "text", "text": "How much may I decide?"}]),
        "{not json",
        _claude("user", [{"type": "text", "text": "Small things only."}]),
        _claude(
            "assistant",
            [
                {"type": "thinking", "thinking": "Hmm."},
                {"type": "tool_use", "name": "Read", "input": {}},
            ],
        ),
        _claude("user", [{"type": "tool_result", "content": "file text"}]),
        _claude(
            "assistant", [{"type": "text", "text": "A subagent."}], isSidechain=True
        ),
        _claude("assistant", [{"type": "text", "text": "Noted: small things."}]),
        _claude("user", "And ask before deleting."),
    )

    _hook(
        root,
        monkeypatch,
        capsys,
        "interrupt",
        payload={
            "transcript_path": str(transcript),
            "last_assistant_message": "Agreed, I will ask first.",
        },
    )

    marker = json.loads((root / ".ww/tasks/T1/interrupted.json").read_text())
    assert marker["recovered_entries"] == 5
    entries = WorkflowService(Storage(root)).interactions.entries("T1")
    spoken = [(entry.speaker, entry.text) for entry in entries]
    assert spoken == [
        ("agent (recovered)", "How much may I decide?"),
        ("operator (recovered)", "Small things only."),
        ("agent (recovered)", "Noted: small things."),
        ("operator (recovered)", "And ask before deleting."),
        ("agent (recovered)", "Agreed, I will ask first."),
    ]
    assert main(["--root", str(root), "instruction", "T1", "--role", "manager"]) == 0
    output = capsys.readouterr().out
    assert "5 entries of the conversation were recovered from the session" in output
    assert "5 entries recovered from the previous session's transcript" in output
    assert "**operator (recovered)** · " in output
    assert "> And ask before deleting." in output


def test_an_interrupt_recovers_the_conversation_from_a_codex_rollout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_conversation(root, agent="codex")

    def line(kind: str, at: str = NEW, **payload: object) -> dict:
        return {"timestamp": at, "type": kind, "payload": payload}

    def item(role: str, text: str) -> dict:
        kind = "output_text" if role == "assistant" else "input_text"
        return line(
            "response_item",
            type="message",
            role=role,
            content=[{"type": kind, "text": text}],
        )

    events = _jsonl(
        tmp_path / "rollout.jsonl",
        line("event_msg", at=OLD, type="user_message", message="Earlier."),
        item("user", "<environment_context>cwd</environment_context>"),
        line("event_msg", type="agent_message", message="Which checks matter?"),
        item("assistant", "Which checks matter?"),
        line("response_item", type="function_call", name="shell"),
        line("event_msg", type="exec_command_end", stdout="ok"),
        line("event_msg", type="user_message", message="Tests and lint."),
    )
    _hook(
        root,
        monkeypatch,
        capsys,
        "interrupt",
        agent="codex",
        payload={"hook_event_name": "Interrupt", "transcript_path": str(events)},
    )
    entries = WorkflowService(Storage(root)).interactions.entries("T1")
    assert [(entry.speaker, entry.text) for entry in entries] == [
        ("agent (recovered)", "Which checks matter?"),
        ("operator (recovered)", "Tests and lint."),
    ]

    # A rollout without event lines falls back to its message items.
    (root / ".ww/tasks/T1/interactions.md").unlink()
    items = _jsonl(
        tmp_path / "items.jsonl",
        item("developer", "Instructions."),
        item("assistant", "Which checks matter?"),
        item("user", "Tests."),
    )
    _hook(
        root,
        monkeypatch,
        capsys,
        "interrupt",
        agent="codex",
        payload={"transcript_path": str(items)},
    )
    entries = WorkflowService(Storage(root)).interactions.entries("T1")
    assert [(entry.speaker, entry.text) for entry in entries] == [
        ("agent (recovered)", "Which checks matter?"),
        ("operator (recovered)", "Tests."),
    ]


def test_recovery_reads_only_the_formats_it_knows(tmp_path: Path) -> None:
    transcript = _jsonl(
        tmp_path / "session.jsonl", _claude("user", "Small things only.")
    )
    since = datetime.now(timezone.utc) - timedelta(days=1)

    assert recover_conversation("claudecode", transcript, since) == (
        ("operator", "Small things only."),
    )
    assert recover_conversation("cursor", transcript, since) == ()
    assert recover_conversation("claudecode", tmp_path / "missing.jsonl", since) == ()
    parsed = hook_agent("cursor").parse(
        "interrupt", {"transcript_path": str(transcript)}
    )
    assert parsed.transcript_path is None


# Sessions that ended without running a hook, and the scan's window


def _age(root: Path, task_id: str, days: int) -> None:
    """Make the task's state look last written ``days`` ago."""
    moment = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    os.utime(root / ".ww/tasks" / task_id / "state.json", (moment, moment))


def test_a_step_left_in_progress_without_a_marker_is_noted_as_a_closed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root)

    lines = _context(_hook(root, monkeypatch, capsys, "session-start")).splitlines()

    assert lines[3].startswith("  Left in progress at ")
    assert lines[3].endswith(
        " by claudecode with no recorded end, probably a closed session: check "
        "its page (`./ww instruction T1 --role manager`) before continuing."
    )
    assert lines[-1] == "`./ww discover` lists every unfinished task."
    assert not any("Interrupted" in line for line in lines)

    _hook(root, monkeypatch, capsys, "interrupt")
    context = _context(_hook(root, monkeypatch, capsys, "session-start"))
    assert "  Interrupted: the previous session" in context
    assert "Left in progress" not in context

    # After a compaction the session holding the step is the one carrying on.
    (root / ".ww/tasks/T1/interrupted.json").unlink()
    compacted = _context(
        _hook(root, monkeypatch, capsys, "session-start", payload={"source": "compact"})
    )
    assert "Left in progress" not in compacted


def test_a_conversation_left_open_points_at_the_recorded_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_conversation(root)

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))

    assert (
        "by claudecode with no recorded end, probably a closed session: the step's "
        "page shows the conversation so far; pick it up at the last unanswered "
        "question." in context
    )
    assert "check its page" not in context


def test_session_start_skips_tasks_written_before_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root, "OLD")
    _age(root, "OLD", 10)

    lines = _context(_hook(root, monkeypatch, capsys, "session-start")).splitlines()
    assert lines == [
        "This project coordinates work through ww: `./ww discover` lists its "
        "workflows.",
        "`./ww discover` lists every unfinished task.",
    ]

    _in_progress(root, "T1")
    context = _context(_hook(root, monkeypatch, capsys, "session-start"))
    assert "- T1 (task, claudecode)" in context
    assert "OLD" not in context

    (root / "ww.json").write_text(
        json.dumps({"agent_hooks": {"recent_days": 30}}), encoding="utf-8"
    )
    assert "- OLD (task, claudecode)" in _context(
        _hook(root, monkeypatch, capsys, "session-start")
    )


def test_check_unfinished_false_prints_only_the_reminder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, {"agent_hooks": {"check_unfinished": False}})
    _in_progress(root)

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))
    reminder = json.loads(_hook(root, monkeypatch, capsys, "stop"))

    assert context == (
        "This project coordinates work through ww: `./ww discover` lists its workflows."
    )
    assert "T1 step `develop` is still in progress" in reminder["reason"]


def test_recent_days_bounds_interrupted_and_the_discover_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, {"agent_hooks": {"recent_days": 10}})
    _in_progress(root)
    _hook(root, monkeypatch, capsys, "interrupt")
    marker = root / ".ww/tasks/T1/interrupted.json"
    record = json.loads(marker.read_text())
    moment = datetime.now(timezone.utc) - timedelta(days=5)
    record["at"] = moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    marker.write_text(json.dumps(record), encoding="utf-8")

    assert main(["--root", str(root), "interrupted"]) == 0
    assert capsys.readouterr().out.startswith("- T1 · develop (attempt 1) · ")
    assert main(["--root", str(root), "interrupted", "--since", "3"]) == 0
    assert capsys.readouterr().out == "No task was interrupted in the last 3 day(s).\n"
    assert main(["--root", str(root), "discover"]) == 0
    assert "1 task was interrupted in the last 10 days; " in capsys.readouterr().out

    (root / "ww.json").write_text(
        json.dumps({"agent_hooks": {"recent_days": 1}}), encoding="utf-8"
    )
    assert main(["--root", str(root), "lookup", "--agent", "claudecode"]) == 0
    assert "interrupted in the last" not in capsys.readouterr().out


def test_both_adapters_tell_when_a_task_was_last_written(tmp_path: Path) -> None:
    root = _root(tmp_path)
    memory = MemoryTaskStorageAdapter()
    service = WorkflowService(Storage(root), memory)
    start_after_init(service, "task", "T1", agent="claudecode")
    file_service = WorkflowService(Storage(root))
    start_after_init(file_service, "task", "T1", agent="claudecode")
    before = datetime.now(timezone.utc) - timedelta(minutes=1)

    for adapter in (memory, file_service.tasks):
        written = adapter.task_written_at("T1")
        assert written is not None and written > before
        assert adapter.task_written_at("NONE") is None

    memory.written_at["T1"] = before - timedelta(days=10)
    work = open_work(memory, root, since=before - timedelta(days=3))
    assert (work.tasks, work.skipped) == ((), 1)


def _parent_with_child(root: Path, *, start: bool) -> WorkflowService:
    (root / "ww.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - split: Collect children.
        children:
          workflow: child
  - name: child
    steps:
      - work: Do the child's work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(root))
    start_after_init(service, "parent", "P", agent="claudecode")
    service.next("P")
    service.add_child("P", "A", "Child requirements.")
    if start:
        service.complete("P", artifact="Collected.", summary_for_next="Ready.")
        service.next("P")
        start_after_init_child = service.start_child("P", "A")
        assert start_after_init_child.task_id == "P/A"
        service.next("P/A")
    return service


def test_the_managers_stop_is_silent_while_a_child_task_is_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _parent_with_child(root, start=True)
    open_ids = {
        task.task_id for task in open_work(Storage(root).task_persistence, root).tasks
    }
    assert {"P", "P/A"} <= open_ids

    assert _hook(root, monkeypatch, capsys, "stop") == ""
    log = _hook_log(root)[-1]
    assert log["hook_decision"] == (
        "allowed: the manager is waiting on a child or worker"
    )


def test_the_stop_still_reminds_in_the_ordinary_in_progress_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _parent_with_child(root, start=False)

    reminder = json.loads(_hook(root, monkeypatch, capsys, "stop"))

    assert "P step `split` is still in progress" in reminder["reason"]
