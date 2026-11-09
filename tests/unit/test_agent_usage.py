# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading agents' session logs: token figures, ww commands and subagents."""

from __future__ import annotations

from pathlib import Path

from tests.usage_logs import (
    CHILD_THREAD,
    SESSION,
    THREAD,
    claude_assistant,
    claude_session,
    claude_subagent,
    claude_usage,
    claude_user,
    codex_count,
    codex_exec,
    codex_figures,
    codex_meta,
    codex_record,
    codex_rollout,
    codex_turn,
    write_lines,
)
from ww.agents import (
    Call,
    ClaudeCode,
    Codex,
    Cursor,
    Spawn,
    TokenCounts,
    Usage,
    session_of,
)
from ww.agents.usage import ww_calls


def test_ww_calls_finds_task_commands_with_their_task_and_role() -> None:
    text = (
        "cd ww-worktrees/TASK-7 && ./ww complete TASK-7 --role manager "
        '--summary="done"; "$CLAUDE_PROJECT_DIR"/ww instruction Atlas --role=worker'
    )

    assert ww_calls(text, "t") == [
        Call("t", "complete", "TASK-7", "manager"),
        Call("t", "instruction", "Atlas", "worker"),
    ]


def test_ww_calls_ignores_read_only_commands_and_paths_named_ww() -> None:
    text = "./ww status TASK-1; ./ww discover; cat src/ww/cli/main.py; ls ww-worktrees"

    assert ww_calls(text, "t") == []


def test_ww_calls_reads_a_start_without_a_task_and_global_options() -> None:
    assert ww_calls("ww --root /p start --workflow task --role manager", "t") == [
        Call("t", "start", None, "manager")
    ]
    assert ww_calls("ww-agentic-workflows record TASK-3 --summary x", "t") == [
        Call("t", "record", "TASK-3", None)
    ]


def test_session_of_names_the_agent_whose_variable_is_set() -> None:
    agent, session = session_of({"CLAUDE_CODE_SESSION_ID": "abc"}) or (None, None)
    assert agent is not None and agent.name == "claudecode" and session == "abc"
    found = session_of({"CODEX_THREAD_ID": "t-1"})
    assert found is not None and found[0].name == "codex"
    assert session_of({}) is None


def test_claude_counts_each_message_once_and_reads_calls_and_spawns(
    tmp_path: Path,
) -> None:
    usage = claude_usage(input=3, write=100, read=1000, output=50, thinking=20)
    path = claude_session(
        tmp_path,
        [
            claude_user("2026-11-01T10:00:00.000Z"),
            # The same message on two lines, one per content block.
            claude_assistant("2026-11-01T10:00:01.000Z", "msg_1", usage=usage),
            claude_assistant(
                "2026-11-01T10:00:01.500Z",
                "msg_1",
                usage=usage,
                bash="./ww next TASK-1 --role manager",
            ),
            claude_assistant("2026-11-01T10:00:05.000Z", "msg_2", spawn="toolu_spawn"),
        ],
    )
    reader = ClaudeCode().usage
    assert reader is not None
    files = reader.files(SESSION, (tmp_path,))
    assert [file.path for file in files] == [path]

    reading = reader.read(files[0], 0, {})

    # A message's commands come before its tokens, which count once.
    assert reading.events == [
        Call("2026-11-01T10:00:01.500Z", "next", "TASK-1", "manager"),
        Usage(
            "2026-11-01T10:00:01.000Z",
            "claude-opus-5-5",
            TokenCounts(3, 100, 1000, 50, 20),
        ),
        Spawn("2026-11-01T10:00:05.000Z", "toolu_spawn"),
        Usage("2026-11-01T10:00:05.000Z", "claude-opus-5-5", TokenCounts(2, 0, 0, 10)),
    ]
    assert reading.offset == path.stat().st_size
    assert reading.directory == "/work/project"


def test_claude_reads_on_from_its_offset_and_leaves_a_partial_line(
    tmp_path: Path,
) -> None:
    path = claude_session(tmp_path, [claude_assistant("2026-11-01T10:00:01Z", "m1")])
    reader = ClaudeCode().usage
    assert reader is not None
    file = reader.files(SESSION, (tmp_path,))[0]
    first = reader.read(file, 0, {})
    write_lines(path, [claude_assistant("2026-11-01T10:00:02Z", "m1")])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "assistant", "timest')

    second = reader.read(file, first.offset, first.carry)

    # The repeated line of m1 is skipped across reads; the partial line waits.
    assert second.events == []
    assert second.offset < path.stat().st_size


def test_claude_finds_subagents_with_the_tool_call_that_started_them(
    tmp_path: Path,
) -> None:
    claude_session(tmp_path, [claude_assistant("2026-11-01T10:00:01Z", "m1")])
    claude_subagent(
        tmp_path, "a1", "toolu_spawn", [claude_assistant("2026-11-01T10:00:02Z", "s1")]
    )
    reader = ClaudeCode().usage
    assert reader is not None

    files = reader.files(SESSION, (tmp_path / "projects",))

    assert [file.key for file in files] == [
        SESSION,
        f"{SESSION}/agent-a1",
    ]
    assert files[1].spawn_tool == "toolu_spawn" and files[1].depth == 1


def test_claude_finds_no_files_for_an_unknown_or_unsafe_session(tmp_path: Path) -> None:
    reader = ClaudeCode().usage
    assert reader is not None
    assert reader.files("missing", (tmp_path,)) == ()
    assert reader.files("../*", (tmp_path,)) == ()


def test_claude_lists_sessions_that_ran_in_a_directory(tmp_path: Path) -> None:
    project = Path("/work/project")
    claude_session(tmp_path, [claude_assistant("2026-11-01T10:00:01Z", "m1")])
    write_lines(
        tmp_path / "projects" / "-work-project-other" / "other.jsonl",
        [{**claude_user("2026-11-01T10:00:00Z"), "cwd": "/work/project-other"}],
    )
    reader = ClaudeCode().usage
    assert reader is not None

    assert reader.sessions_in((tmp_path,), project) == (SESSION,)


def test_codex_reads_usage_records_net_of_cache_and_its_shell_calls(
    tmp_path: Path,
) -> None:
    path = codex_rollout(
        tmp_path,
        "2026/10/26",
        "2026-10-26T11-55-08",
        THREAD,
        [
            codex_meta("2026-10-26T10:55:08.000Z", THREAD),
            codex_turn("2026-10-26T10:55:09.000Z"),
            codex_record(
                "2026-10-26T10:55:10.000Z",
                codex_figures(
                    input=1000, cached=800, written=50, output=20, reasoning=5
                ),
                "resp_1",
            ),
            # The same response as a token_count event: counted once.
            codex_count(
                "2026-10-26T10:55:10.100Z", codex_figures(input=1000, cached=800)
            ),
            codex_exec(
                "2026-10-26T10:55:11.000Z", "./ww complete TASK-2 --role manager"
            ),
        ],
    )
    reader = Codex().usage
    assert reader is not None
    files = reader.files(THREAD, (tmp_path,))
    assert [file.path for file in files] == [path]

    reading = reader.read(files[0], 0, {})

    assert reading.events == [
        Usage(
            "2026-10-26T10:55:10.000Z", "gpt-6.1-sol", TokenCounts(150, 50, 800, 20, 5)
        ),
        Call("2026-10-26T10:55:11.000Z", "complete", "TASK-2", "manager"),
    ]
    assert reading.directory == "/work/project"


def test_codex_without_usage_records_reads_token_counts(tmp_path: Path) -> None:
    codex_rollout(
        tmp_path,
        "2026/09/15",
        "2026-09-15T18-12-07",
        THREAD,
        [
            codex_meta("2026-09-15T16:12:07Z", THREAD),
            codex_turn("2026-09-15T16:12:08Z", model="gpt-5"),
            codex_count("2026-09-15T16:12:09Z", codex_figures(input=500, cached=400)),
        ],
    )
    reader = Codex().usage
    assert reader is not None

    reading = reader.read(reader.files(THREAD, (tmp_path,))[0], 0, {})

    assert reading.events == [
        Usage("2026-09-15T16:12:09Z", "gpt-5", TokenCounts(100, 0, 400, 10))
    ]


def test_codex_finds_subagent_threads_by_their_parent(tmp_path: Path) -> None:
    codex_rollout(
        tmp_path,
        "2026/10/26",
        "2026-10-26T11-55-08",
        THREAD,
        [codex_meta("2026-10-26T10:55:08Z", THREAD)],
    )
    codex_rollout(
        tmp_path,
        "2026/10/26",
        "2026-10-26T11-57-00",
        CHILD_THREAD,
        [codex_meta("2026-10-26T10:57:00Z", CHILD_THREAD, parent=THREAD)],
    )
    codex_rollout(
        tmp_path,
        "2026/10/26",
        "2026-10-26T11-58-00",
        "01a177e1-0000-7000-8000-000000000000",
        [codex_meta("2026-10-26T10:58:00Z", "01a177e1-0000-7000-8000-000000000000")],
    )
    reader = Codex().usage
    assert reader is not None

    files = reader.files(THREAD, (tmp_path,))

    # A reader's keys are bare IDs: the collector adds the agent's name.
    assert [file.key for file in files] == [THREAD, CHILD_THREAD]
    assert files[1].parent == THREAD
    assert files[1].spawned_at == "2026-10-26T10:57:00Z"
    assert reader.sessions_in((tmp_path,), Path("/work/project")) == (
        THREAD,
        "01a177e1-0000-7000-8000-000000000000",
    )


def test_cursor_stop_payload_gives_the_turn_net_of_cache() -> None:
    hooks = Cursor().hooks
    assert hooks is not None

    payload = hooks.parse(
        "stop",
        {
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "model": "claude-opus-5-5",
            "status": "completed",
            "input_tokens": 1000,
            "output_tokens": 30,
            "cache_read_tokens": 700,
            "cache_write_tokens": 200,
            "workspace_roots": ["/work/project"],
        },
    )

    assert payload.usage is not None
    assert payload.usage.session == "conv-1" and payload.usage.key == "gen-1"
    assert payload.usage.counts == TokenCounts(100, 200, 700, 30)
    assert hooks.parse("stop", {"status": "completed"}).usage is None
    assert hooks.parse("session-start", {"input_tokens": 3}).usage is None
