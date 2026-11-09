# SPDX-License-Identifier: GPL-3.0-or-later
"""Each built-in agent's headless launcher: its checks and its commands.

The agents' programs are stood in for by small shell scripts, so no test
needs Claude Code, Codex or Cursor installed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from ww.agents import (
    AgentRegistry,
    CatalogCache,
    ClaudeCode,
    Codex,
    Cursor,
    CustomAgent,
    HeadlessLauncher,
    HeadlessSettings,
    LaunchRequest,
    ModelCatalog,
    Output,
    ResultRecording,
    TokenCounts,
    parse_version,
    run_probe,
)
from ww.cli import main

CODEX_MODELS = {
    "models": [
        {
            "slug": "gpt-6.1-sol",
            "visibility": "list",
            "supported_reasoning_levels": [
                {"effort": level}
                for level in ("low", "medium", "high", "xhigh", "max", "ultra")
            ],
        },
        {
            "slug": "gpt-6-luna",
            "visibility": "list",
            "supported_reasoning_levels": [
                {"effort": level} for level in ("low", "medium", "high")
            ],
        },
    ]
}


def _headless(agent: object) -> HeadlessLauncher:
    launcher = agent.launcher("headless")  # type: ignore[attr-defined]
    assert isinstance(launcher, HeadlessLauncher)
    return launcher


def _program(directory: Path, name: str, cases: dict[str, str]) -> Path:
    """A shell script answering each argument string in ``cases``."""
    branches = "".join(
        f"  {pattern!r}) {answer} ;;\n" for pattern, answer in cases.items()
    )
    path = directory / name
    path.write_text(
        f'#!/bin/sh\ncase "$*" in\n{branches}  *) echo "unexpected: $*" >&2; '
        "exit 2 ;;\nesac\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _codex(directory: Path, version: str = "codex-cli 0.164.0") -> Path:
    (directory / "models.json").write_text(json.dumps(CODEX_MODELS), "utf-8")
    return _program(
        directory,
        "codex",
        {
            "--version": f"echo '{version}'",
            "login status": "echo 'Logged in using ChatGPT'",
            "debug models": f"/bin/cat '{directory / 'models.json'}'",
        },
    )


def _claude(directory: Path, logged_in: bool = True) -> Path:
    status = json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai"})
    return _program(
        directory,
        "claude",
        {
            "--version": "echo '2.1.300 (Claude Code)'",
            "auth status": f"echo '{status}'",
        },
    )


def _cursor(directory: Path, models: str = "gpt-5 - GPT-5\nsonnet-4-thinking") -> Path:
    (directory / "models.txt").write_text(models + "\n", "utf-8")
    return _program(
        directory,
        "cursor-agent",
        {
            "--version": "echo '2026.06.11-03-15-00-241fc09'",
            "status": "echo 'Logged in as someone'",
            "models": f"/bin/cat '{directory / 'models.txt'}'",
        },
    )


def _request(**values: object) -> LaunchRequest:
    defaults: dict[str, object] = {
        "task_id": "TASK-1",
        "prompt": "./ww instruction TASK-1 --role worker",
        "working_directory": Path("/work/TASK-1"),
        "state_directory": Path("/work/.ww"),
    }
    return LaunchRequest(**{**defaults, **values})  # type: ignore[arg-type]


def _messages(findings: Sequence[object]) -> list[tuple[str, str]]:
    return [(each.level, each.subject) for each in findings]  # type: ignore[attr-defined]


def test_every_built_in_agent_runs_headless_and_custom_agents_do_not() -> None:
    for agent in (ClaudeCode(), Codex(), Cursor()):
        launcher = _headless(agent)
        assert launcher.agent == agent.name
        assert launcher.recording is ResultRecording.WORKER
        assert agent.launcher("api") is None
    assert CustomAgent("custom:aider").launcher("headless") is None


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("2.1.300 (Claude Code)", (2, 1, 300)),
        ("codex-cli 0.164.0", (0, 164, 0)),
        ("2026.06.11-03-15-00-241fc09", (2026, 6, 11)),
        ("no version here", None),
    ],
)
def test_versions_are_read_from_each_programs_own_format(
    text: str, version: tuple[int, ...] | None
) -> None:
    assert parse_version(text) == version


def test_a_missing_program_is_an_error_naming_the_setting(tmp_path: Path) -> None:
    readiness = _headless(Codex()).probe(
        HeadlessSettings(binary=str(tmp_path / "nowhere" / "codex"))
    )

    assert not readiness.ready
    assert _messages(readiness.findings) == [("error", "binary")]
    assert "agents.codex.headless.binary" in readiness.findings[0].message


def test_a_ready_program_reports_its_version_login_and_models(tmp_path: Path) -> None:
    binary = _codex(tmp_path)

    readiness = _headless(Codex()).probe(HeadlessSettings(binary=str(binary)))

    assert readiness.ready
    assert readiness.findings == ()
    assert readiness.binary == str(binary)
    assert readiness.version == "codex-cli 0.164.0"
    assert readiness.logged_in is True
    assert readiness.catalog == ModelCatalog(
        {
            "gpt-6.1-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
            "gpt-6-luna": ("low", "medium", "high"),
        }
    )


def test_an_old_version_is_refused_and_a_newer_one_only_warned(tmp_path: Path) -> None:
    old = _headless(Codex()).probe(
        HeadlessSettings(binary=str(_codex(tmp_path, "codex-cli 0.150.2")))
    )
    newer = _headless(Codex()).probe(
        HeadlessSettings(binary=str(_codex(tmp_path, "codex-cli 0.170.0")))
    )
    patched = _headless(Codex()).probe(
        HeadlessSettings(binary=str(_codex(tmp_path, "codex-cli 0.164.9")))
    )

    assert _messages(old.findings) == [("error", "version")]
    assert "older than 0.164.0" in old.findings[0].message
    assert _messages(newer.findings) == [("warning", "version")]
    assert patched.findings == ()


def test_a_logged_out_program_cannot_be_launched(tmp_path: Path) -> None:
    readiness = _headless(ClaudeCode()).probe(
        HeadlessSettings(binary=str(_claude(tmp_path, logged_in=False)))
    )

    assert readiness.logged_in is False
    assert _messages(readiness.findings) == [("error", "login")]


def test_a_model_list_is_cached_per_binary_and_version(tmp_path: Path) -> None:
    binary = str(_codex(tmp_path))
    calls: list[list[str]] = []

    def runner(argv: Sequence[str], timeout: float) -> Output | None:
        calls.append(list(argv))
        return run_probe(argv, timeout)

    cache = CatalogCache(tmp_path / "catalogs")
    launcher = _headless(Codex())
    settings = HeadlessSettings(binary=binary)
    first = launcher.probe(settings, runner=runner, cache=cache)
    second = launcher.probe(settings, runner=runner, cache=cache)

    assert first.catalog == second.catalog
    assert [call[1:] for call in calls].count(["debug", "models"]) == 1
    assert cache.get("codex", binary, "codex-cli 0.165.0") is None


def test_models_are_unchecked_when_the_list_cannot_be_read(tmp_path: Path) -> None:
    binary = _program(
        tmp_path,
        "codex",
        {
            "--version": "echo 'codex-cli 0.164.0'",
            "login status": "echo 'Logged in using ChatGPT'",
            "debug models": "echo 'unknown command' >&2; exit 2",
        },
    )
    launcher = _headless(Codex())
    readiness = launcher.probe(HeadlessSettings(binary=str(binary)))

    assert readiness.ready
    assert _messages(readiness.findings) == [("warning", "models")]
    assert launcher.selection_findings("gpt-9", "high", readiness.catalog) == []


def test_codex_checks_reasoning_against_the_chosen_models_own_levels() -> None:
    launcher = _headless(Codex())
    catalog = ModelCatalog.from_dict(
        {"models": {"gpt-6.1-sol": ["low", "ultra"], "gpt-6-luna": ["low", "high"]}}
    )

    assert launcher.selection_findings("gpt-6.1-sol", "ultra", catalog) == []
    luna = launcher.selection_findings("gpt-6-luna", "ultra", catalog)
    assert _messages(luna) == [("error", "reasoning")]
    assert "low, high" in luna[0].message
    assert _messages(launcher.selection_findings("gpt-9", "low", catalog)) == [
        ("error", "model")
    ]
    # Without the list (as linting checks), the agent's own levels apply.
    assert _messages(launcher.selection_findings(None, "extreme")) == [
        ("error", "reasoning")
    ]


def test_claude_code_takes_its_aliases_and_full_names_and_warns_on_others() -> None:
    launcher = _headless(ClaudeCode())

    assert launcher.selection_findings("opus", "xhigh") == []
    assert launcher.selection_findings("claude-opus-5-5", "max") == []
    assert launcher.selection_findings("cheapest", "low") == []
    assert _messages(launcher.selection_findings("gpt-5", None)) == [
        ("warning", "model")
    ]
    assert _messages(launcher.selection_findings("opus", "ultra")) == [
        ("error", "reasoning")
    ]


def test_cursor_takes_no_reasoning_and_defines_no_cheapest_model() -> None:
    launcher = _headless(Cursor())

    findings = launcher.selection_findings("gpt-5", "high")
    assert _messages(findings) == [("error", "reasoning")]
    assert "model variant" in findings[0].message
    assert launcher.selection_findings("gpt-5", "auto") == []
    assert _messages(launcher.selection_findings("cheapest", None)) == [
        ("error", "model")
    ]


def test_cursor_reads_its_model_lines_and_doubts_an_unconfirmed_login(
    tmp_path: Path,
) -> None:
    launcher = _headless(Cursor())
    readiness = launcher.probe(HeadlessSettings(binary=str(_cursor(tmp_path))))

    assert readiness.findings == ()
    assert readiness.catalog == ModelCatalog({"gpt-5": None, "sonnet-4-thinking": None})
    assert (
        launcher.logged_in(
            Output(0, "✓ Login successful!\nLogged in (unable to fetch user details)")
        )
        is None
    )
    empty = launcher.probe(
        HeadlessSettings(
            binary=str(_cursor(tmp_path, "No models available for this account."))
        )
    )
    assert _messages(empty.findings) == [("warning", "models")]


def test_settings_an_agent_cannot_honour_are_reported() -> None:
    codex = _headless(Codex())
    findings = codex.settings_findings(
        HeadlessSettings(
            permissions="read-only",
            fallback_model="gpt-6-luna",
            max_budget_usd=2.0,
            args=("--json", "--search"),
        )
    )

    assert _messages(findings) == [
        ("error", "permissions"),
        ("warning", "settings"),
        ("warning", "settings"),
        ("warning", "settings"),
    ]
    assert "--json" in findings[-1].message
    claude = _headless(ClaudeCode())
    assert (
        claude.settings_findings(
            HeadlessSettings(
                permissions="read-only", fallback_model="sonnet", isolated=True
            )
        )
        == []
    )


def test_claude_code_commands() -> None:
    launcher = _headless(ClaudeCode())
    request = _request(
        model="review",
        reasoning="high",
        settings=HeadlessSettings(
            models={"review": "opus"},
            fallback_model="sonnet",
            max_budget_usd=5,
            isolated=True,
            args=("--name", "ww"),
        ),
    )

    assert launcher.launch_command("claude", request) == [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "opus",
        "--effort",
        "high",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Bash(./ww *)",
        "--fallback-model",
        "sonnet",
        "--max-budget-usd",
        "5",
        "--bare",
        "--name",
        "ww",
        "--",
        "./ww instruction TASK-1 --role worker",
    ]
    resume = launcher.resume_command("claude", _request(model="cheapest"), "abc")
    assert resume[:4] == ["claude", "-p", "--resume", "abc"]
    assert resume[resume.index("--model") + 1] == "haiku"
    assert "--effort" not in resume
    full = launcher.launch_command(
        "claude", _request(settings=HeadlessSettings(permissions="full"))
    )
    assert "bypassPermissions" in full


def test_codex_commands_keep_ww_state_writable() -> None:
    launcher = _headless(Codex())
    request = _request(model="gpt-6.1-sol", reasoning="xhigh")

    assert launcher.launch_command("codex", request) == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-m",
        "gpt-6.1-sol",
        "-c",
        "model_reasoning_effort=xhigh",
        "-C",
        "/work/TASK-1",
        "-s",
        "workspace-write",
        "--add-dir",
        "/work/.ww",
        "--",
        "./ww instruction TASK-1 --role worker",
    ]
    resume = launcher.resume_command("codex", request, "thread-1")
    assert resume[:3] == ["codex", "exec", "resume"]
    assert 'sandbox_mode="workspace-write"' in resume
    assert 'sandbox_workspace_write.writable_roots=["/work/.ww"]' in resume
    assert resume[-3:] == ["--", "thread-1", "./ww instruction TASK-1 --role worker"]
    full = launcher.launch_command(
        "codex", _request(settings=HeadlessSettings(permissions="full"))
    )
    assert full[full.index("-s") + 1] == "danger-full-access"
    assert "--add-dir" not in full


def test_cursor_commands() -> None:
    launcher = _headless(Cursor())
    request = _request(model="gpt-5", settings=HeadlessSettings(permissions="full"))

    assert launcher.launch_command("cursor-agent", request) == [
        "cursor-agent",
        "-p",
        "--output-format",
        "stream-json",
        "--trust",
        "--workspace",
        "/work/TASK-1",
        "--model",
        "gpt-5",
        "--force",
        "--",
        "./ww instruction TASK-1 --role worker",
    ]
    assert launcher.resume_command("cursor-agent", _request(), "chat-1")[:4] == [
        "cursor-agent",
        "-p",
        "--resume",
        "chat-1",
    ]


def test_each_agent_names_its_session_and_usage_in_its_output() -> None:
    claude = _headless(ClaudeCode())
    init = claude.stream_facts(
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "m"}
    )
    assert (init.session_id, init.model) == ("s1", "m")
    result = claude.stream_facts(
        {
            "type": "result",
            "session_id": "s1",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 20,
                "output_tokens": 4,
            },
        }
    )
    assert result.usage == TokenCounts(input=2, cache_write=10, cache_read=20, output=4)

    codex = _headless(Codex())
    assert codex.stream_facts({"type": "thread.started", "thread_id": "t1"}).session_id
    turn = codex.stream_facts(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 60,
                "output_tokens": 5,
            },
        }
    )
    assert turn.usage == TokenCounts(input=40, cache_read=60, output=5)

    cursor = _headless(Cursor())
    assert (
        cursor.stream_facts({"type": "system", "session_id": "c1"}).session_id == "c1"
    )


def test_agents_check_reports_each_agent_and_fails_on_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    programs = tmp_path / "bin"
    programs.mkdir()
    _codex(programs)
    _claude(programs, logged_in=False)
    monkeypatch.setenv("PATH", f"{programs}:/usr/bin:/bin")
    root = tmp_path / "project"
    root.mkdir()

    assert main(["--root", str(root), "agents", "--check", "codex"]) == 0
    report = capsys.readouterr().out
    assert "## Codex (`codex`)" in report
    assert "`gpt-6-luna` (low, medium, high)" in report
    assert (root / ".ww" / "agents" / "catalogs" / "codex.json").is_file()

    assert main(["--root", str(root), "agents", "--check"]) == 1
    report = capsys.readouterr().out
    assert "- Error: not logged in" in report
    assert "## Cursor (`cursor`)\n\n- Binary: not found" in report

    assert main(["--root", str(root), "agents"]) == 0
    listed = json.loads(capsys.readouterr().out)["agents"]
    assert {agent["name"]: agent["runs"] for agent in listed} == {
        name: ["session", "headless"] for name in ("codex", "claudecode", "cursor")
    }
    assert AgentRegistry().resolve("custom:aider").launchers == ()
