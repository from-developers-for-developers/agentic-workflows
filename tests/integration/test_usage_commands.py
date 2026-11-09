# SPDX-License-Identifier: GPL-3.0-or-later
"""Token usage through the CLI: recorded sessions, collection and display."""

from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.usage_logs import (
    SESSION,
    claude_assistant,
    claude_session,
    claude_usage,
)
from ww.cli import main

WORKFLOWS = """workflows:
  - name: task
    steps:
      - develop: Develop.
"""


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main", ".")
    _git(root, "config", "user.email", "t@e.st")
    _git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    (root / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (root / "ww.json").write_text(
        json.dumps({"extensions": {"ww/git": {"base_branches": {"default": "main"}}}}),
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Claude Code session running ww, with its own log directory."""
    claude = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    return claude


def _run(root: Path, capsys: pytest.CaptureFixture[str], *arguments: str) -> str:
    code = main(["--root", str(root), *arguments])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return captured.out


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _start(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(
        root,
        capsys,
        "start",
        "TASK-1",
        "--workflow",
        "task",
        "--agent",
        "claudecode",
        "--requirements",
        "Build it.",
        "--description",
        "Build it",
        "--role",
        "manager",
    )


def _log(root: Path) -> list[dict]:
    path = root / ".ww" / "executions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_every_command_records_the_agent_session_and_host(
    project: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys)

    record = _log(project)[-1]
    assert record["agent"] == "claudecode"
    assert record["agent_session"] == SESSION
    assert record["host"]


def test_complete_collects_the_step_tokens_shown_by_usage_and_tasks(
    project: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys)
    _run(project, capsys, "next", "TASK-1", "--role", "manager")
    claude_session(
        home,
        [
            claude_assistant(
                _now(),
                "m1",
                usage=claude_usage(input=10, write=1000, read=20000, output=500),
                bash="./ww instruction TASK-1 --role manager",
            ),
            claude_assistant(
                _now(),
                "m2",
                usage=claude_usage(input=5, output=100),
                bash='./ww complete TASK-1 --role manager --summary "done"',
            ),
        ],
    )

    _run(
        project,
        capsys,
        "complete",
        "TASK-1",
        "--role",
        "manager",
        "--artifact",
        "Built.",
        "--summary",
        "Built.",
    )
    usage = json.loads(_run(project, capsys, "usage", "TASK-1", "--json"))
    listed = _run(project, capsys, "tasks")

    rows = [(row["step"], row["role"], row["total"]) for row in usage["rows"]]
    assert rows == [("develop", "single", 21615)]
    assert usage["unreadable"] == []
    assert "21.6k" in listed
    assert "develop" in _run(project, capsys, "usage", "TASK-1")
    assert "TASK-1" in _run(project, capsys, "usage")


def test_usage_collect_reports_what_it_read(
    project: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys)

    collected = json.loads(_run(project, capsys, "usage", "collect", "--json"))

    assert collected == {
        "files": 0,
        "stretches": 0,
        "unreadable": [f"claudecode:{SESSION}"],
    }
    assert "no logs on this machine" in _run(project, capsys, "usage", "collect")


def test_rescan_needs_collect(
    project: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(project), "usage", "--rescan"]) == 1
    assert "--rescan applies to `usage collect` only" in capsys.readouterr().err


def test_switched_off_usage_says_so(
    project: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = json.loads((project / "ww.json").read_text(encoding="utf-8"))
    (project / "ww.json").write_text(
        json.dumps({**settings, "usage": False}), encoding="utf-8"
    )

    assert "switched off" in _run(project, capsys, "usage", "collect")


def test_a_cursor_stop_hook_records_the_turn(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _start(project, capsys)
    payload = {
        "conversation_id": "conv-1",
        "generation_id": "gen-1",
        "model": "gpt-x",
        "input_tokens": 300,
        "output_tokens": 40,
        "cache_read_tokens": 200,
        "workspace_roots": [str(project)],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert main(["--root", str(project), "hook", "stop", "--agent", "cursor"]) == 0
    capsys.readouterr()

    usage = json.loads(_run(project, capsys, "usage", "TASK-1", "--json"))
    assert usage["total"]["total"] == 340
    assert _log(project)[-1]["hook_decision"] == "turn usage recorded"
