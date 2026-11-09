# SPDX-License-Identifier: GPL-3.0-or-later
"""ww starting a step's agent headless, waiting for it, and what follows.

A shell script stands in for Codex: it answers the checks, and when started
on a step it reads its page and completes it with ww, or misbehaves as the
test asks through environment variables.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ww.cli import main
from ww.headless import LAUNCH_VARIABLE, LaunchStore, worker_prompt

WORKFLOW = """workflows:
  - name: task
    steps:
      - review: Review the change.
        agent: {name: codex, run: headless}
        model: gpt-6.1-sol
        reasoning: high
      - fix: Fix the findings.
"""

FAKE_CODEX = """#!/bin/sh
case "$1 $2" in
  "--version "*) echo 'codex-cli 0.164.0'; exit 0 ;;
  "login status") echo 'Logged in using ChatGPT'; exit 0 ;;
  "debug models")
    printf '%s%s\n' '{"models":[{"slug":"gpt-6.1-sol",' \\
      '"supported_reasoning_levels":[{"effort":"high"}]}]}'
    exit 0 ;;
esac
for last; do :; done
echo "argv: $*" >&2
echo "host session: ${CLAUDE_CODE_SESSION_ID:-none}" >&2
echo "launch: ${WW_HEADLESS_LAUNCH:-none}" >&2
echo '{"type":"thread.started","thread_id":"thread-1"}'
if [ -n "$FAKE_SLEEP" ]; then sleep "$FAKE_SLEEP"; exit 0; fi
if [ -n "$FAKE_FAIL" ]; then echo 'something broke' >&2; exit 3; fi
page=$(printf '%s' "$last" | sed -n 's/.*[Rr]un `[^ ]* \\([^`]*\\)`.*/\\1/p')
task=$(printf '%s' "$page" | cut -d' ' -f2)
assignment=$(printf '%s' "$page" | sed -n 's/.*--assignment \\([^ ]*\\).*/\\1/p')
"$PYTHON" -m ww.cli $page >/dev/null
"$PYTHON" -m ww.cli complete "$task" --role worker \\
  ${assignment:+--assignment "$assignment"} \\
  --artifact "Reviewed headless." --summary "Reviewed." >&2
printf '%s%s\n' '{"type":"turn.completed","usage":{"input_tokens":100,' \\
  '"cached_input_tokens":40,"output_tokens":7}}'
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "ww.yaml").write_text(WORKFLOW, encoding="utf-8")
    codex = tmp_path / "codex"
    codex.write_text(FAKE_CODEX, encoding="utf-8")
    codex.chmod(0o755)
    _settings(root, codex)
    monkeypatch.chdir(root)
    monkeypatch.setenv("PYTHON", sys.executable)
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [
                str(Path(__file__).resolve().parents[2] / "src"),
                os.environ.get("PYTHONPATH", ""),
            ]
        ),
    )
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-session")
    monkeypatch.setenv("WW_OPERATOR_WAIT", "30")
    monkeypatch.delenv(LAUNCH_VARIABLE, raising=False)
    for name in ("FAKE_SLEEP", "FAKE_FAIL"):
        monkeypatch.delenv(name, raising=False)
    return root


def _settings(root: Path, codex: Path, **headless: object) -> None:
    (root / "ww.local.json").write_text(
        json.dumps(
            {"agents": {"codex": {"headless": {"binary": str(codex), **headless}}}}
        ),
        encoding="utf-8",
    )


def _ww(root: Path, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    code = main(["--root", str(root), *args])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _start(
    root: Path, capsys: pytest.CaptureFixture[str], runtime: str = "single"
) -> str:
    code, page = _ww(
        root,
        capsys,
        "start",
        "TASK-1",
        "--workflow",
        "task",
        "--agent",
        "claudecode",
        "--role",
        "manager",
        "--requirements",
        "Review headless.",
        "--description",
        "Review headless",
        "--runtime",
        runtime,
    )
    assert code == 0, page
    return page


def _launches(root: Path) -> list[dict[str, object]]:
    return [record.to_dict() for record in LaunchStore(root).runs("TASK-1", "01-task")]


def _error_log(root: Path, launch: dict[str, object]) -> str:
    path = root / ".ww/tasks/TASK-1/runs/01-task/headless" / f"{launch['id']}.err.log"
    return path.read_text(encoding="utf-8")


def test_ww_starts_the_steps_agent_and_wait_returns_the_next_page(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys)

    code, page = _ww(project, capsys, "next", "TASK-1", "--role", "manager")
    assert code == 0, page
    assert "## Manager: ww runs `codex` headless for `review`" in page
    assert "wait TASK-1" in page

    code, page = _ww(project, capsys, "wait", "TASK-1")
    assert code == 0, page
    assert "perform `fix`" in page
    (launch,) = _launches(project)
    assert launch["status"] == "exited"
    assert launch["session_id"] == "thread-1"
    assert launch["usage"] == {
        "input": 60,
        "cache_write": 0,
        "cache_read": 40,
        "output": 7,
        "reasoning": 0,
    }
    argv = launch["argv"]
    assert isinstance(argv, list)
    assert argv[1:9] == [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-m",
        "gpt-6.1-sol",
        "-c",
        "model_reasoning_effort=high",
        "-C",
    ]
    errors = _error_log(project, launch)
    # The host's session is not handed to the agent; its launch is.
    assert "host session: none" in errors
    assert f"launch: {launch['id']}" in errors


def test_in_auto_the_preview_selects_no_worker_and_the_agent_gets_the_token(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    page = _start(project, capsys, runtime="auto")
    assert "ww runs `codex` headless for `review`, so no worker is selected" in page
    assert "--selected-agent" not in page

    _ww(project, capsys, "next", "TASK-1", "--role", "manager")
    code, page = _ww(project, capsys, "wait", "TASK-1")

    assert code == 0, page
    assert "dispatch the `fix` assignment" in page
    (launch,) = _launches(project)
    assert launch["assignment"]
    argv = launch["argv"]
    assert isinstance(argv, list)
    assert f"--assignment {launch['assignment']}" in argv[-1]


def test_a_run_that_ends_without_completing_shows_why_and_the_choices(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(project, capsys)
    monkeypatch.setenv("FAKE_FAIL", "1")
    _ww(project, capsys, "next", "TASK-1", "--role", "manager")

    code, page = _ww(project, capsys, "wait", "TASK-1")

    assert code == 0, page
    assert (
        "## Manager: the headless run of `review` ended without completing it" in page
    )
    assert "it exited with code 3 without completing the step" in page
    assert "- Agent session: `thread-1`" in page
    assert "something broke" in page
    assert "launch TASK-1 --resume" in page
    assert "launch TASK-1 --session" in page
    assert "git status" in page

    # Waiting again, or showing the page, starts nothing new.
    _ww(project, capsys, "instruction", "TASK-1", "--role", "manager")
    assert len(_launches(project)) == 1

    monkeypatch.delenv("FAKE_FAIL")
    code, page = _ww(project, capsys, "launch", "TASK-1", "--resume")
    assert code == 0, page
    code, page = _ww(project, capsys, "wait", "TASK-1")
    assert "perform `fix`" in page
    first, resumed = _launches(project)
    assert resumed["resumes"] == first["id"]
    argv = resumed["argv"]
    assert isinstance(argv, list)
    assert argv[1:3] == ["exec", "resume"]
    assert argv[-2] == "thread-1"


def test_starting_again_and_giving_the_step_back_to_a_session(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(project, capsys, runtime="auto")
    monkeypatch.setenv("FAKE_FAIL", "1")
    _ww(project, capsys, "next", "TASK-1", "--role", "manager")
    _ww(project, capsys, "wait", "TASK-1")

    code, page = _ww(project, capsys, "launch", "TASK-1")
    assert code == 0, page
    _ww(project, capsys, "wait", "TASK-1")
    assert len(_launches(project)) == 2

    code, page = _ww(project, capsys, "launch", "TASK-1", "--session")
    assert code == 0, page
    assert "### Worker bootstrap" in page
    assert "ww runs `codex` headless" not in page
    code, page = _ww(project, capsys, "launch", "TASK-1", "--session")
    assert code == 1
    assert "already given back to a session" in page


def test_a_program_that_fails_the_checks_is_not_started(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _settings(project, tmp_path / "missing-codex")
    _start(project, capsys)

    code, page = _ww(project, capsys, "next", "TASK-1", "--role", "manager")

    assert code == 0, page
    assert "## Manager: ww did not start `codex` for `review`" in page
    assert "- Error: " in page and "missing-codex" in page
    assert _launches(project) == []


def test_a_silent_agent_is_stopped_after_its_idle_timeout(
    project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(project, tmp_path / "codex", idle_timeout_minutes=0.03)
    _start(project, capsys)
    monkeypatch.setenv("FAKE_SLEEP", "60")
    _ww(project, capsys, "next", "TASK-1", "--role", "manager")

    code, page = _ww(project, capsys, "wait", "TASK-1")

    assert "printed nothing for 0.03 minutes and was stopped" in page
    (launch,) = _launches(project)
    assert launch["status"] == "idle"


def test_a_wait_that_ends_while_the_run_goes_on_shows_the_waiting_page(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(project, capsys)
    monkeypatch.setenv("FAKE_SLEEP", "60")
    _ww(project, capsys, "next", "TASK-1", "--role", "manager")
    monkeypatch.setenv("WW_OPERATOR_WAIT", "1")

    code, page = _ww(project, capsys, "wait", "TASK-1")
    assert code == 0, page
    assert "## Manager: ww runs `codex` headless for `review`" in page

    code, page = _ww(project, capsys, "reset", "TASK-1", "--yes")
    assert code == 0, page
    assert not list((project / ".ww").rglob("headless/*.json"))


def test_reset_stops_a_run_that_is_still_going(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(project, capsys)
    monkeypatch.setenv("FAKE_SLEEP", "60")
    _ww(project, capsys, "next", "TASK-1", "--role", "manager")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        (launch,) = _launches(project)
        if launch["agent_pid"]:
            break
        time.sleep(0.1)
    pid = launch["agent_pid"]
    assert isinstance(pid, int)

    _ww(project, capsys, "reset", "TASK-1", "--yes")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the headless agent was not stopped")


def test_a_headless_agent_runs_no_manager_command(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(project, capsys)
    monkeypatch.setenv(LAUNCH_VARIABLE, "abc")

    code, page = _ww(project, capsys, "next", "TASK-1", "--role", "manager")
    assert code == 1
    assert "started headless by ww for one step" in page
    code, page = _ww(project, capsys, "wait", "TASK-1")
    assert code == 1


def test_the_prompt_names_the_page_and_forbids_manager_commands(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _start(project, capsys)
    _ww(project, capsys, "next", "TASK-1", "--role", "manager")
    (launch,) = _launches(project)
    argv = launch["argv"]
    assert isinstance(argv, list)
    prompt = argv[-1]

    assert prompt.startswith("You are a ww worker. Run `")
    assert "instruction TASK-1 --run 01-task --role worker`" in prompt
    assert "never run a command with `--role manager`" in prompt
    assert worker_prompt.__module__ == "ww.headless.runs"
