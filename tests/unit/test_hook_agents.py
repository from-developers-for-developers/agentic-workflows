# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent hook protocols and the hooks-file installer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.agents import Agent, AgentRegistry, builtin_agents
from ww.hooks import (
    HookInstallError,
    hook_protocol,
    hook_snippet,
    hooks_installed,
    install_hooks,
    registered_elsewhere,
    uninstall_hooks,
)
from ww.storage import Storage

HOOK_AGENTS = sorted(agent.name for agent in builtin_agents() if agent.hooks)


def hook_agent(name: str) -> Agent:
    return AgentRegistry().resolve(name)


# Payload parsing


def test_claude_code_reads_its_payload() -> None:
    agent = hook_protocol(hook_agent("claudecode"))

    start = agent.parse("session-start", {"cwd": "/w", "source": "compact"})

    assert (start.directory, start.source) == (Path("/w"), "compact")


def test_codex_reads_its_payload() -> None:
    agent = hook_protocol(hook_agent("codex"))

    assert agent.parse("session-start", {"source": "compact"}).source == "compact"


# Reply shapes


def test_each_agent_answers_in_its_own_shape() -> None:
    claude, codex, cursor = (
        hook_protocol(hook_agent(name)) for name in ("claudecode", "codex", "cursor")
    )

    for agent in (claude, codex):
        assert json.loads(agent.context_reply("ctx")) == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "ctx",
            }
        }
    assert json.loads(cursor.context_reply("ctx")) == {"additional_context": "ctx"}


# Registration


def _commands(document: dict) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for native, entries in document["hooks"].items():
        for entry in entries:
            handlers = entry.get("hooks", [entry])
            result.setdefault(native, []).extend(h["command"] for h in handlers)
    return result


def test_the_session_start_hook_is_registered_without_a_subagent_start() -> None:
    claude = _commands(json.loads(hook_snippet(hook_agent("claudecode"))))

    assert set(claude) == {"SessionStart"}
    assert "SubagentStart" not in claude
    snippet = json.loads(hook_snippet(hook_agent("claudecode")))
    assert "matcher" not in snippet["hooks"]["SessionStart"][0]
    assert claude["SessionStart"] == [
        '"$CLAUDE_PROJECT_DIR"/ww hook session-start --agent claudecode'
    ]


@pytest.mark.parametrize(
    ("agent", "events"),
    [
        ("codex", {"SessionStart"}),
        ("cursor", {"sessionStart"}),
    ],
)
def test_each_agent_registers_its_native_events(agent: str, events: set[str]) -> None:
    assert set(_commands(json.loads(hook_snippet(hook_agent(agent))))) == events


def test_cursor_commands_run_from_the_project_root() -> None:
    snippet = json.loads(hook_snippet(hook_agent("cursor")))

    assert snippet["version"] == 1
    assert snippet["hooks"] == {
        "sessionStart": [
            {"command": "./ww hook session-start --agent cursor", "timeout": 10}
        ]
    }


# Installing


@pytest.mark.parametrize("name", HOOK_AGENTS)
def test_install_is_idempotent_and_uninstall_removes_only_ww(
    tmp_path: Path, name: str
) -> None:
    agent = hook_agent(name)
    storage = Storage(tmp_path)
    path = tmp_path / hook_protocol(agent).settings_file
    path.parent.mkdir(parents=True)
    own = (
        {"version": 1, "hooks": {"Stop": [{"command": "echo x"}]}}
        if name == "cursor"
        else {
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo x"}]}]}
        }
    )
    path.write_text(json.dumps({**own, "other": 1}), encoding="utf-8")

    assert install_hooks(storage, agent).action == "installed"
    installed = path.read_text(encoding="utf-8")
    assert hooks_installed(storage, agent)
    assert install_hooks(storage, agent).action == "unchanged"
    assert path.read_text(encoding="utf-8") == installed
    assert json.loads(installed)["other"] == 1

    assert uninstall_hooks(storage, agent).action == "removed"
    assert json.loads(path.read_text(encoding="utf-8")) == {**own, "other": 1}
    assert uninstall_hooks(storage, agent).action == "absent"


def test_install_creates_a_missing_hooks_file(tmp_path: Path) -> None:
    storage = Storage(tmp_path)

    assert install_hooks(storage, hook_agent("codex")).action == "installed"
    assert (tmp_path / ".codex/hooks.json").is_file()
    assert uninstall_hooks(Storage(tmp_path / "none"), hook_agent("codex")).action == (
        "absent"
    )


@pytest.mark.parametrize("content", ["not json", "[1, 2]"])
def test_an_unreadable_hooks_file_fails_with_the_manual_snippet(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / ".claude/settings.json"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")

    with pytest.raises(HookInstallError) as raised:
        install_hooks(Storage(tmp_path), hook_agent("claudecode"))

    message = str(raised.value)
    assert ".claude/settings.json" in message
    assert "/ww hook session-start --agent claudecode" in message
    assert path.read_text(encoding="utf-8") == content


def test_an_unwritable_hooks_file_fails_with_the_manual_snippet(
    tmp_path: Path,
) -> None:
    directory = tmp_path / ".cursor"
    directory.mkdir()
    directory.chmod(0o500)
    try:
        with pytest.raises(HookInstallError, match="cannot write .cursor/hooks.json"):
            install_hooks(Storage(tmp_path), hook_agent("cursor"))
    finally:
        directory.chmod(0o700)


# The local scope


def test_claude_code_installs_into_its_local_file(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    agent = hook_agent("claudecode")

    installation = install_hooks(storage, agent, local=True)

    assert installation.path == ".claude/settings.local.json"
    assert (tmp_path / ".claude/settings.local.json").is_file()
    assert not (tmp_path / ".claude/settings.json").exists()
    assert hooks_installed(storage, agent, local=True)
    assert not hooks_installed(storage, agent)
    assert uninstall_hooks(storage, agent, local=True).action == "removed"


@pytest.mark.parametrize(
    ("name", "user_file"),
    [
        ("codex", "~/.codex/hooks.json"),
        ("cursor", "~/.cursor/hooks.json"),
    ],
)
def test_agents_without_a_local_file_refuse_the_local_scope(
    tmp_path: Path, name: str, user_file: str
) -> None:
    with pytest.raises(
        HookInstallError, match="has no project-local hooks file"
    ) as raised:
        install_hooks(Storage(tmp_path), hook_agent(name), local=True)

    assert user_file in str(raised.value)
    assert not any(tmp_path.iterdir())


def test_a_second_copy_in_the_other_file_is_reported(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    agent = hook_agent("claudecode")
    assert registered_elsewhere(storage, agent) is None

    install_hooks(storage, agent)

    assert registered_elsewhere(storage, agent, local=True) == ".claude/settings.json"
    assert registered_elsewhere(storage, agent) is None
    assert registered_elsewhere(storage, hook_agent("codex")) is None


# Agents without hooks


def test_an_agent_without_hooks_cannot_take_them(tmp_path: Path) -> None:
    with pytest.raises(HookInstallError, match="ww has no hooks for custom:kimi"):
        install_hooks(Storage(tmp_path), hook_agent("custom:kimi"))


# Permissions


def test_only_claude_code_says_how_to_allow_commands() -> None:
    commands = ("ww-agentic-workflows", "./ww", "ww")
    permissions = hook_agent("claudecode").permissions

    assert permissions is not None
    assert permissions.shared_file == ".claude/settings.json"
    assert permissions.allow(commands) == {
        "permissions": {
            "allow": [
                "Bash(ww-agentic-workflows *)",
                "Bash(./ww *)",
                "Bash(ww *)",
            ]
        }
    }
    for name in ("codex", "cursor"):
        assert hook_agent(name).permissions is None
