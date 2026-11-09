# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent session logs shaped like the real ones, for the token usage tests.

The shapes follow logs Claude Code 2.x and Codex 0.106–0.164 wrote: only
the fields ww reads are filled in, with a few it must ignore.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SESSION = "0f5d3c1e-1111-4222-8333-444455556666"
THREAD = "01a00000-0000-7000-8000-000000000001"
CHILD_THREAD = "01a00000-0000-7000-8000-000000000002"


def write_lines(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")
    return path


def claude_usage(
    input: int = 2, write: int = 0, read: int = 0, output: int = 10, thinking: int = 0
) -> dict[str, Any]:
    return {
        "input_tokens": input,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
        "output_tokens": output,
        "output_tokens_details": {"thinking_tokens": thinking},
        "service_tier": "standard",
    }


def claude_assistant(
    at: str,
    message: str,
    *,
    usage: dict[str, Any] | None = None,
    bash: str | None = None,
    spawn: str | None = None,
    model: str = "claude-opus-5-5",
    session: str = SESSION,
) -> dict[str, Any]:
    """One assistant line: Claude Code writes one per content block."""
    if bash is not None:
        block: dict[str, Any] = {
            "type": "tool_use",
            "id": f"toolu_{message}",
            "name": "Bash",
            "input": {"command": bash, "description": "Run it"},
        }
    elif spawn is not None:
        block = {
            "type": "tool_use",
            "id": spawn,
            "name": "Agent",
            "input": {"prompt": "Run ./ww instruction TASK-1 --role worker"},
        }
    else:
        block = {"type": "text", "text": "Some words that ww never keeps."}
    return {
        "type": "assistant",
        "timestamp": at,
        "sessionId": session,
        "cwd": "/work/project",
        "requestId": f"req_{message}",
        "message": {
            "id": message,
            "model": model,
            "role": "assistant",
            "content": [block],
            "usage": usage or claude_usage(),
        },
    }


def claude_user(at: str, text: str = "Please do it.") -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": at,
        "sessionId": SESSION,
        "cwd": "/work/project",
        "message": {"role": "user", "content": text},
    }


def claude_session(
    home: Path, lines: list[dict[str, Any]], directory: str = "-work-project"
) -> Path:
    return write_lines(home / "projects" / directory / f"{SESSION}.jsonl", lines)


def claude_subagent(
    home: Path,
    agent: str,
    tool: str,
    lines: list[dict[str, Any]],
    directory: str = "-work-project",
    depth: int = 1,
) -> Path:
    base = home / "projects" / directory / SESSION / "subagents"
    base.mkdir(parents=True, exist_ok=True)
    (base / f"agent-{agent}.meta.json").write_text(
        json.dumps(
            {"agentType": "general-purpose", "toolUseId": tool, "spawnDepth": depth}
        ),
        encoding="utf-8",
    )
    return write_lines(base / f"agent-{agent}.jsonl", lines)


def codex_figures(
    input: int = 100,
    cached: int = 0,
    written: int = 0,
    output: int = 10,
    reasoning: int = 0,
) -> dict[str, int]:
    return {
        "input_tokens": input,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": written,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input + output,
    }


def codex_meta(
    at: str, thread: str, parent: str | None = None, cwd: str = "/work/project"
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": thread,
        "timestamp": at,
        "cwd": cwd,
        "originator": "codex_cli_rs",
        "cli_version": "0.164.0",
        "source": "cli",
        "base_instructions": {"text": "You are Codex."},
    }
    if parent:
        payload["source"] = {
            "subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}
        }
    return {"timestamp": at, "type": "session_meta", "payload": payload}


def codex_turn(at: str, model: str = "gpt-6.1-sol") -> dict[str, Any]:
    return {
        "timestamp": at,
        "type": "turn_context",
        "payload": {"model": model, "cwd": "/work/project"},
    }


def codex_record(at: str, figures: dict[str, int], response: str) -> dict[str, Any]:
    return {
        "timestamp": at,
        "type": "token_usage_record",
        "payload": {
            "thread_id": THREAD,
            "response_id": response,
            "usage": figures,
            "turn_token_usage": figures,
        },
    }


def codex_count(at: str, figures: dict[str, int]) -> dict[str, Any]:
    return {
        "timestamp": at,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": figures,
                "last_token_usage": figures,
                "model_context_window": 258400,
            },
        },
    }


def codex_exec(at: str, command: str, name: str = "exec") -> dict[str, Any]:
    return {
        "timestamp": at,
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": name,
            "call_id": "call_1",
            "input": (
                f'text(await tools.exec_command({{cmd:"{command}",'
                '"max_output_tokens":4000}}));\n'
            ),
        },
    }


def codex_rollout(
    home: Path, day: str, stamp: str, thread: str, lines: list[dict[str, Any]]
) -> Path:
    return write_lines(
        home / "sessions" / day / f"rollout-{stamp}-{thread}.jsonl", lines
    )
