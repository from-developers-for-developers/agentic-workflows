# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent hooks as an agent calls them: one line of context, nothing else."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.errors import StateError
from ww.open_work import unreadable_tasks
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


def test_session_start_prints_only_the_reminder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))

    assert context == (
        "This project coordinates work through ww: `./ww discover` lists its workflows."
    )


def test_session_start_lists_no_task_however_many_are_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    service = _in_progress(root)
    start_after_init(service, "task", "T2", agent="claudecode")
    service.fail("T1", "could not build")

    context = _context(
        _hook(root, monkeypatch, capsys, "session-start", payload={"source": "startup"})
    )

    assert context == (
        "This project coordinates work through ww: `./ww discover` lists its workflows."
    )
    assert "T1" not in context
    assert "unfinished" not in context


def test_session_start_under_on_request_says_ww_is_used_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path, {"enabled": "on_request"})
    _in_progress(root)

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))

    assert context == (
        "This project has ww available on request only: use it only when the "
        "user explicitly asks for ww; otherwise work without it and do not ask. "
        "`./ww discover` lists its workflows."
    )


def test_session_start_after_compaction_says_state_is_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)

    context = _context(
        _hook(root, monkeypatch, capsys, "session-start", payload={"source": "compact"})
    )

    assert context.splitlines()[0] == (
        "Context was compacted; ww's task state is authoritative."
    )
    assert len(context.splitlines()) == 2


@pytest.mark.parametrize(
    "settings",
    [
        {"agent_hooks": {"check_unfinished": False}},
        {"agent_hooks": {"recent_days": 30}},
    ],
)
def test_the_agent_hooks_keys_are_accepted_and_change_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: dict,
) -> None:
    root = _root(tmp_path, settings)
    _in_progress(root)

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))

    assert context.startswith("This project coordinates work through ww")
    assert len(context.splitlines()) == 1


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


# stop and interrupt


@pytest.mark.parametrize("event", ["stop", "interrupt"])
@pytest.mark.parametrize("agent", ["claudecode", "codex", "cursor", "antigravity"])
def test_stop_and_interrupt_from_an_older_installation_print_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    event: str,
    agent: str,
) -> None:
    root = _root(tmp_path)
    _in_progress(root)

    output = _hook(root, monkeypatch, capsys, event, agent, {"cwd": str(root)})

    assert output == ""
    task_directory = root / ".ww/tasks/T1"
    assert not (task_directory / "interrupted.json").exists()
    assert not (task_directory / "stop-reminders.json").exists()


def test_an_interrupted_session_leaves_every_task_command_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root)
    _hook(root, monkeypatch, capsys, "interrupt")

    assert main(["--root", str(root), "instruction", "T1", "--role", "manager"]) == 0
    page = capsys.readouterr().out
    assert main(["--root", str(root), "status", "T1"]) == 0
    status = capsys.readouterr().out

    for output in (page, status):
        assert "Interrupted" not in output
        assert "previous session" not in output


def test_the_interrupted_command_is_gone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)

    with pytest.raises(SystemExit):
        main(["--root", str(root), "interrupted"])
    capsys.readouterr()


def test_reset_still_removes_the_task(tmp_path: Path) -> None:
    root = _root(tmp_path)
    service = _in_progress(root)
    legacy = root / ".ww/tasks/T1/interrupted.json"
    legacy.write_text("{}", encoding="utf-8")

    service.reset("T1")

    assert not service.tasks.task_exists("T1")


# Robustness and the audit log


def test_every_hook_call_is_logged_with_ww_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root)
    _hook(root, monkeypatch, capsys, "session-start")
    _hook(root, monkeypatch, capsys, "stop")
    _hook(root, monkeypatch, capsys, "interrupt")

    decisions = [
        (record["hook_event"], record["hook_agent"], record["hook_decision"])
        for record in _hook_log(root)
    ]

    assert decisions == [
        ("session-start", "claudecode", "context"),
        ("stop", "claudecode", "nothing to answer: stop and interrupt are retired"),
        (
            "interrupt",
            "claudecode",
            "nothing to answer: stop and interrupt are retired",
        ),
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
    """Leave ``task_id`` with a state file ww cannot read."""
    start_after_init(
        WorkflowService(Storage(root)), "task", task_id, agent="claudecode"
    )
    directory = root / ".ww/tasks" / task_id
    (directory / "state.json").write_text("{broken", encoding="utf-8")


def test_an_unreadable_task_is_named_by_discover_and_nowhere_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    _in_progress(root)
    _unreadable(root)

    assert main(["--root", str(root), "discover"]) == 0
    output = capsys.readouterr().out
    assert "## Unreadable tasks" in output
    assert "- `BAD` — invalid task state " in output
    assert "Other tasks and new work are unaffected." in output
    assert "Unfinished tasks" not in output
    assert main(["--root", str(root), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [task["task_id"] for task in report["unreadable_tasks"]] == ["BAD"]
    assert report["unreadable_tasks"][0]["reason"].startswith("invalid task state ")
    assert "unfinished_tasks" not in report
    assert "interrupted_recently" not in report

    context = _context(_hook(root, monkeypatch, capsys, "session-start"))
    assert "BAD" not in context
    assert main(["--root", str(root), "lookup", "--agent", "claudecode"]) == 0
    assert "BAD" not in capsys.readouterr().out

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
    output = capsys.readouterr().out
    assert "Unreadable tasks" not in output
    assert "Unfinished tasks" not in output
    assert main(["--root", str(root), "discover", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["unreadable_tasks"] == []


def test_an_unreadable_child_task_is_named_with_its_full_id(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _in_progress(root)
    child = root / ".ww/tasks/T1/T1.1"
    child.mkdir(parents=True)
    (child / "state.json").write_text("{broken", encoding="utf-8")

    unreadable = unreadable_tasks(Storage(root).task_persistence)

    assert [task.task_id for task in unreadable] == ["T1/T1.1"]


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
    unreadable = unreadable_tasks(persistence)
    assert [task.task_id for task in unreadable] == ["BAD"]
    assert unreadable[0].reason.startswith("invalid task state BAD: ")


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
    assert "./ww hook session-start --agent cursor" in output
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
    # Hooks for claudecode (default yes), for codex (no), then the opt-in
    # permissions question (default no).
    answers = iter(["", "n", ""])
    tty_type = type("Tty", (), {"isatty": lambda self: True})
    monkeypatch.setattr("ww.cli.initialization.sys.stdin", tty_type())
    monkeypatch.setattr(sys.modules["ww.cli.main"], "_initialization_options", _options)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert main(["--root", str(tmp_path), "init"]) == 0

    explanation = capsys.readouterr().out
    assert "ww's session-start hook tells an agent session that ww coordinates" in (
        explanation.replace("\n", " ")
    )
    assert "It only adds one line of context; nothing is blocked." in (
        explanation.replace("\n", " ")
    )
    assert "unfinished" not in explanation
    assert (tmp_path / ".claude/settings.json").is_file()
    assert not (tmp_path / ".codex/hooks.json").exists()
    choices = json.loads((tmp_path / ".ww/init-choices.json").read_text())
    assert choices["hooks"] == {"claudecode": True, "codex": False}
    assert choices["permissions"] is False
    assert not (tmp_path / ".claude/settings.local.json").exists()


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


def test_install_registers_only_session_start_and_uninstall_removes_older_entries(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    path = root / ".claude/settings.json"
    path.parent.mkdir()
    older = {
        "hooks": {
            native: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'"$CLAUDE_PROJECT_DIR"/ww hook {event} '
                            "--agent claudecode",
                        }
                    ]
                }
            ]
            for native, event in (
                ("SessionStart", "session-start"),
                ("Stop", "stop"),
                ("SubagentStop", "stop"),
                ("SessionEnd", "interrupt"),
            )
        }
    }
    path.write_text(json.dumps(older), encoding="utf-8")

    assert main(["--root", str(root), "hook", "install", "--agent", "claudecode"]) == 0
    assert set(json.loads(path.read_text())["hooks"]) == {"SessionStart"}
    path.write_text(json.dumps(older), encoding="utf-8")
    assert (
        main(["--root", str(root), "hook", "uninstall", "--agent", "claudecode"]) == 0
    )
    assert not json.loads(path.read_text()).get("hooks")
