# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent hook adapters and the hooks-file installer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.hooks import (
    HOOK_AGENTS,
    HookInstallError,
    hook_agent,
    hook_snippet,
    hooks_installed,
    install_hooks,
    registered_elsewhere,
    uninstall_hooks,
)
from ww.storage import Storage

# Payload parsing


def test_claude_code_reads_its_payload() -> None:
    agent = hook_agent("claudecode")

    start = agent.parse("session-start", {"cwd": "/w", "source": "compact"})
    stop = agent.parse("stop", {"cwd": "/w", "stop_hook_active": True})
    end = agent.parse("interrupt", {"cwd": "/w", "reason": "prompt_input_exit"})

    assert (start.directory, start.source) == (Path("/w"), "compact")
    assert stop.continued is True
    assert end.reason == "prompt_input_exit"
    assert agent.parse("stop", {}).continued is False


def test_codex_reads_its_payload_and_names_an_interrupt() -> None:
    agent = hook_agent("codex")

    assert agent.parse("session-start", {"source": "compact"}).source == "compact"
    assert agent.parse("stop", {"stop_hook_active": True}).continued is True
    assert (
        agent.parse("interrupt", {"hook_event_name": "Interrupt"}).reason
        == "interrupted"
    )
    assert (
        agent.parse("interrupt", {"hook_event_name": "SessionEnd", "reason": "other"})
        .reason
        == "other"
    )


@pytest.mark.parametrize(
    ("status", "interrupted"),
    [("completed", False), ("error", False), ("aborted", True)],
)
def test_cursor_treats_an_aborted_stop_as_an_interruption(
    status: str, interrupted: bool
) -> None:
    payload = hook_agent("cursor").parse(
        "stop", {"workspace_roots": ["/w"], "status": status, "loop_count": 0}
    )

    assert payload.directory == Path("/w")
    assert payload.interrupted is interrupted
    assert payload.continued is False


def test_cursor_counts_its_follow_up_loop_as_continued() -> None:
    assert hook_agent("cursor").parse("stop", {"loop_count": 1}).continued is True


def test_antigravity_wants_context_only_on_the_first_invocation() -> None:
    agent = hook_agent("antigravity")

    first = agent.parse(
        "session-start", {"invocationNum": 0, "workspacePaths": ["/w"]}
    )
    later = agent.parse("session-start", {"invocationNum": 3})

    assert (first.wants_context, first.directory) == (True, Path("/w"))
    assert later.wants_context is False
    assert agent.parse("stop", {"terminationReason": "model_stop"}).interrupted is False
    assert agent.parse("stop", {"terminationReason": "user_cancelled"}).interrupted


# Reply shapes


def test_each_agent_answers_in_its_own_shape() -> None:
    claude, codex = hook_agent("claudecode"), hook_agent("codex")
    cursor, antigravity = hook_agent("cursor"), hook_agent("antigravity")

    for agent in (claude, codex):
        assert json.loads(agent.context_reply("ctx")) == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "ctx",
            }
        }
        assert json.loads(agent.continue_reply("go")) == {
            "decision": "block",
            "reason": "go",
        }
    assert json.loads(cursor.context_reply("ctx")) == {"additional_context": "ctx"}
    assert json.loads(cursor.continue_reply("go")) == {"followup_message": "go"}
    assert json.loads(antigravity.context_reply("ctx")) == {
        "injectSteps": [{"ephemeralMessage": "ctx"}]
    }
    assert json.loads(antigravity.continue_reply("go")) == {
        "decision": "continue",
        "reason": "go",
    }


# Registration


def _commands(document: dict, agent: str) -> dict[str, list[str]]:
    hooks = (
        document["ww-agentic-workflows"]
        if agent == "antigravity"
        else document["hooks"]
    )
    result: dict[str, list[str]] = {}
    for native, entries in hooks.items():
        if native == "enabled":
            continue
        for entry in entries:
            handlers = entry.get("hooks", [entry])
            result.setdefault(native, []).extend(h["command"] for h in handlers)
    return result


def test_the_session_start_hook_is_registered_without_a_subagent_start() -> None:
    claude = _commands(json.loads(hook_snippet(hook_agent("claudecode"))), "claudecode")

    assert set(claude) == {"SessionStart", "Stop", "SubagentStop", "SessionEnd"}
    assert "SubagentStart" not in claude
    snippet = json.loads(hook_snippet(hook_agent("claudecode")))
    assert "matcher" not in snippet["hooks"]["SessionStart"][0]
    assert claude["SessionStart"] == [
        '"$CLAUDE_PROJECT_DIR"/ww hook session-start --agent claudecode'
    ]


@pytest.mark.parametrize(
    ("agent", "events"),
    [
        ("codex", {"SessionStart", "Stop", "SubagentStop", "Interrupt", "SessionEnd"}),
        ("cursor", {"sessionStart", "stop", "subagentStop", "sessionEnd"}),
        ("antigravity", {"PreInvocation", "Stop"}),
    ],
)
def test_each_agent_registers_its_native_events(agent: str, events: set[str]) -> None:
    assert set(_commands(json.loads(hook_snippet(hook_agent(agent))), agent)) == events


def test_cursor_commands_run_from_the_project_root() -> None:
    snippet = json.loads(hook_snippet(hook_agent("cursor")))

    assert snippet["version"] == 1
    assert snippet["hooks"]["stop"] == [
        {"command": "./ww hook stop --agent cursor", "timeout": 10}
    ]


# Installing


@pytest.mark.parametrize("name", sorted(HOOK_AGENTS))
def test_install_is_idempotent_and_uninstall_removes_only_ww(
    tmp_path: Path, name: str
) -> None:
    agent = hook_agent(name)
    storage = Storage(tmp_path)
    path = tmp_path / agent.settings_file
    path.parent.mkdir(parents=True)
    own = (
        {"mine": {"enabled": True, "Stop": [{"hooks": [{"command": "echo x"}]}]}}
        if name == "antigravity"
        else {"version": 1, "hooks": {"Stop": [{"command": "echo x"}]}}
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
    assert "/ww hook stop --agent claudecode" in message
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
        ("antigravity", "~/.gemini/config/hooks.json"),
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
