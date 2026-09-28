# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent hooks as an agent calls them: context, a stop reminder, interruptions."""

from __future__ import annotations

import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.hooks import answer_hook, hook_agent
from ww.service import WorkflowService
from ww.storage import Storage

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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (tmp_path / "ww-agentic-workflows.json").write_text(
        json.dumps(settings or {}), encoding="utf-8"
    )
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


def _context(output: str) -> str:
    return json.loads(output)["hookSpecificOutput"]["additionalContext"]


def _in_progress(root: Path, task_id: str = "T1", **options: object) -> WorkflowService:
    service = WorkflowService(Storage(root))
    start_after_init(service, "task", task_id, agent="claudecode", **options)
    service.next(task_id)
    return service


def _hook_log(root: Path) -> list[dict]:
    lines = (root / ".ww/executions.jsonl").read_text(encoding="utf-8").splitlines()
    return [
        record
        for line in lines
        if (record := json.loads(line))["command"] == "hook"
    ]


# session-start


def test_session_start_without_tasks_prints_only_the_reminder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))

    assert context == (
        "This project coordinates work through ww: `./ww discover` lists its "
        "workflows."
    )


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
        "- T1 (task) develop: in progress · in the root · resume: "
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
    assert lines[-1] == "… and 2 more; `./ww status <task-id>` shows one."


def test_a_task_awaiting_the_operator_stays_visible_and_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    service = _in_progress(root)
    service.fail("T1", "could not build")

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))

    assert "- T1 (task) develop: awaiting the operator: the work failed" in context
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
    _in_progress(root)

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
    _in_progress(root)

    reply = _hook(root, monkeypatch, capsys, "stop", "cursor", {"status": "aborted"})

    assert reply == ""
    marker = json.loads((root / ".ww/tasks/T1/interrupted.json").read_text())
    assert (marker["agent"], marker["reason"]) == ("cursor", "aborted")


def test_a_session_in_a_task_workspace_marks_only_that_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(
        tmp_path, {"projects": [{"name": "backend", "path": "./backend"}]}
    )
    (root / "backend").mkdir()
    _in_progress(root, "IN-ROOT")
    _in_progress(root, "IN-BACKEND", project="backend")

    _hook(
        root, monkeypatch, capsys, "interrupt", payload={"cwd": str(root / "backend")}
    )
    assert (root / ".ww/tasks/IN-BACKEND/interrupted.json").exists()
    assert not (root / ".ww/tasks/IN-ROOT/interrupted.json").exists()

    # A session nowhere in particular may be working on any of them.
    _hook(root, monkeypatch, capsys, "interrupt", payload={"cwd": "/elsewhere"})
    assert (root / ".ww/tasks/IN-ROOT/interrupted.json").exists()


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
    (root / ".ww/tasks/T9").mkdir(parents=True)
    (root / ".ww/tasks/T9/state.json").write_text("{broken", encoding="utf-8")
    assert _hook(root, monkeypatch, capsys, "session-start") == ""
    assert _hook_log(root)[-1]["outcome"] == "error"
    assert capsys.readouterr().err == ""


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
    monkeypatch.setattr(
        "ww.cli.initialization.sys.stdin", tty_type()
    )
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
            pool.map(lambda _: answer_hook(storage, agent, "stop", "{}"), range(8))
        )

    assert sum(answer.text != "" for answer in answers) == 1
