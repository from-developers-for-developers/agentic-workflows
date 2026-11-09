# SPDX-License-Identifier: GPL-3.0-or-later
"""Placing tokens on tasks, steps and roles from the agents' session logs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.usage_logs import (
    SESSION,
    claude_assistant,
    claude_session,
    claude_subagent,
    claude_usage,
    claude_user,
)
from ww.agents import Agent, ClaudeCode, TokenCounts, TurnUsage, session_of
from ww.project_config import UsagePrice, UsageSettings
from ww.storage import Storage
from ww.usage import Collector, UsageStore, render_task_usage, task_usage
from ww.usage.timeline import instant

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class FakeTimes:
    """Tasks with fixed step start times; a finished task stops at ``finished``."""

    def __init__(self, tasks: dict[str, dict]) -> None:
        self.tasks = tasks

    def exists(self, task_id: str) -> bool:
        return task_id in self.tasks

    def active(self, task_id: str, at: str) -> bool:
        finished = self.tasks[task_id].get("finished")
        return finished is None or at <= instant(finished)

    def place(self, task_id: str, at: str) -> tuple[str | None, str | None]:
        begun = [
            path for start, path in self.tasks[task_id]["steps"] if instant(start) <= at
        ]
        return "01-task", begun[-1] if begun else None

    def runtime(self, task_id: str, at: str) -> str | None:
        return self.tasks[task_id].get("runtime", "auto")


def _log(root: Path, *records: dict) -> None:
    path = root / ".ww" / "executions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _invocation(
    at: str,
    command: str,
    task: str | None,
    session: str | None = SESSION,
    host: str = "box",
) -> dict:
    return {
        "timestamp": at,
        "command": command,
        "task_id": task,
        "agent": "claudecode" if session else None,
        "agent_session": session,
        "host": host,
    }


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "claude"


def _collector(
    root: Path,
    home: Path,
    tasks: dict[str, dict],
    agents: tuple[Agent, ...] | None = None,
    **settings,
) -> Collector:
    collector = Collector(
        Storage(root),
        UsageSettings(**settings),
        agents=agents,
        environ={"CLAUDE_CONFIG_DIR": str(home)},
        now=NOW,
    )
    collector.times = FakeTimes(tasks)  # type: ignore[assignment]
    collector._known = (tuple(tasks), {})
    return collector


def _by_place(root: Path, task: str) -> dict[tuple, int]:
    totals: dict[tuple, int] = {}
    for stretch in UsageStore(Storage(root)).task_records(task):
        # The session's own file is named by its session, a subagent's by its own.
        key = (
            stretch.step,
            stretch.role,
            stretch.file.split(":", 1)[1].rsplit("/", 1)[-1],
        )
        totals[key] = totals.get(key, 0) + stretch.counts.total
    return totals


def _tokens(count: int) -> dict:
    return claude_usage(input=count, output=0)


TASKS = {
    "TASK-1": {
        "steps": [
            ("2026-11-01T10:01:00Z", "develop"),
            ("2026-11-01T10:10:00Z", "review"),
        ],
    }
}


def test_a_session_charges_the_task_and_step_its_latest_command_put_it_on(
    tmp_path: Path, home: Path
) -> None:
    _log(tmp_path, _invocation("2026-11-01T10:00:30Z", "start", "TASK-1"))
    claude_session(
        home,
        [
            claude_user("2026-11-01T09:59:00Z"),
            # The conversation that led to ``start`` belongs to the task.
            claude_assistant("2026-11-01T09:59:30Z", "m0", usage=_tokens(5)),
            claude_assistant(
                "2026-11-01T10:00:29Z",
                "m1",
                usage=_tokens(7),
                bash="./ww start --workflow task --role manager",
            ),
            claude_assistant("2026-11-01T10:05:00Z", "m2", usage=_tokens(11)),
            claude_assistant(
                "2026-11-01T10:11:00Z",
                "m3",
                usage=_tokens(13),
                bash="./ww next TASK-1 --role manager",
            ),
        ],
    )

    result = _collector(tmp_path, home, TASKS).collect()

    assert result.files == 1 and result.unreadable == ()
    assert _by_place(tmp_path, "TASK-1") == {
        (None, "manager", SESSION): 12,
        ("develop", "manager", SESSION): 11,
        ("review", "manager", SESSION): 13,
    }


def test_a_second_collection_reads_only_new_lines(tmp_path: Path, home: Path) -> None:
    _log(tmp_path, _invocation("2026-11-01T10:00:30Z", "next", "TASK-1"))
    path = claude_session(
        home,
        [
            claude_assistant(
                "2026-11-01T10:02:00Z",
                "m1",
                usage=_tokens(5),
                bash="./ww next TASK-1 --role manager",
            )
        ],
    )
    _collector(tmp_path, home, TASKS).collect()
    assert _collector(tmp_path, home, TASKS).collect().files == 0
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(claude_assistant("2026-11-01T10:03:00Z", "m2", usage=_tokens(4)))
            + "\n"
        )

    _collector(tmp_path, home, TASKS).collect()

    assert _by_place(tmp_path, "TASK-1") == {("develop", "manager", SESSION): 9}


def test_tokens_before_a_command_that_does_not_claim_them_are_outside(
    tmp_path: Path, home: Path
) -> None:
    _log(tmp_path, _invocation("2026-11-01T10:02:00Z", "next", "TASK-1"))
    claude_session(
        home,
        [
            claude_assistant("2026-11-01T09:00:00Z", "m0", usage=_tokens(5)),
            claude_assistant(
                "2026-11-01T10:02:00Z",
                "m1",
                usage=_tokens(0),
                bash="./ww next TASK-1 --role manager",
            ),
        ],
    )

    _collector(tmp_path, home, TASKS).collect()

    outside = UsageStore(Storage(tmp_path)).outside_records()
    assert sum(stretch.counts.total for stretch in outside) == 5


def test_a_finished_task_stops_taking_tokens(tmp_path: Path, home: Path) -> None:
    tasks = {"TASK-1": {**TASKS["TASK-1"], "finished": "2026-11-01T10:20:00Z"}}
    _log(tmp_path, _invocation("2026-11-01T10:15:00Z", "complete", "TASK-1"))
    claude_session(
        home,
        [
            claude_assistant(
                "2026-11-01T10:15:00Z",
                "m1",
                usage=_tokens(3),
                bash="./ww complete TASK-1 --role manager",
            ),
            claude_assistant("2026-11-01T11:00:00Z", "m2", usage=_tokens(40)),
        ],
    )

    _collector(tmp_path, home, tasks).collect()

    assert _by_place(tmp_path, "TASK-1") == {("review", "manager", SESSION): 3}
    # Still waiting: a later ``start`` would claim them.
    state = UsageStore(Storage(tmp_path)).load()
    waiting = state.files[f"claudecode:{SESSION}"].pending
    assert TokenCounts.from_dict(waiting["claude-opus-5-5"]).total == 40


def test_a_worker_subagent_charges_its_own_assignment(
    tmp_path: Path, home: Path
) -> None:
    _log(tmp_path, _invocation("2026-11-01T10:02:00Z", "next", "TASK-1"))
    claude_session(
        home,
        [
            claude_assistant(
                "2026-11-01T10:02:00Z",
                "m1",
                usage=_tokens(5),
                bash="./ww next TASK-1 --role manager",
            ),
            claude_assistant(
                "2026-11-01T10:02:30Z", "m2", usage=_tokens(1), spawn="toolu_worker"
            ),
        ],
    )
    claude_subagent(
        home,
        "w1",
        "toolu_worker",
        [
            # The worker reads its prompt before running its first command.
            claude_assistant(
                "2026-11-01T10:02:40Z",
                "s1",
                usage=_tokens(100),
                bash="./ww instruction TASK-1 --role worker",
            ),
            claude_assistant("2026-11-01T10:04:00Z", "s2", usage=_tokens(200)),
        ],
    )

    _collector(tmp_path, home, TASKS).collect()

    assert _by_place(tmp_path, "TASK-1") == {
        ("develop", "manager", SESSION): 6,
        ("develop", "worker", "agent-w1"): 300,
    }


def test_a_quiet_subagent_without_ww_commands_charges_its_spawner(
    tmp_path: Path, home: Path
) -> None:
    _log(tmp_path, _invocation("2026-11-01T10:02:00Z", "next", "TASK-1"))
    claude_session(
        home,
        [
            claude_assistant(
                "2026-11-01T10:02:00Z",
                "m1",
                usage=_tokens(5),
                bash="./ww next TASK-1 --role manager",
            ),
            claude_assistant(
                "2026-11-01T10:02:30Z", "m2", usage=_tokens(1), spawn="toolu_explore"
            ),
        ],
    )
    explorer = claude_subagent(
        home,
        "e1",
        "toolu_explore",
        [claude_assistant("2026-11-01T10:03:00Z", "s1", usage=_tokens(50))],
    )
    quiet = (NOW.timestamp() - 600,) * 2
    os.utime(explorer, quiet)

    _collector(tmp_path, home, TASKS).collect()

    assert _by_place(tmp_path, "TASK-1")[("develop", "manager", "agent-e1")] == 50


def test_a_subagent_still_writing_waits(tmp_path: Path, home: Path) -> None:
    _log(tmp_path, _invocation("2026-11-01T10:02:00Z", "next", "TASK-1"))
    claude_session(
        home,
        [
            claude_assistant(
                "2026-11-01T10:02:30Z",
                "m2",
                usage=_tokens(1),
                spawn="toolu_explore",
                bash=None,
            )
        ],
    )
    claude_subagent(
        home,
        "e1",
        "toolu_explore",
        [claude_assistant("2026-11-01T10:03:00Z", "s1", usage=_tokens(50))],
    )
    collector = Collector(
        Storage(tmp_path), UsageSettings(), environ={"CLAUDE_CONFIG_DIR": str(home)}
    )
    collector.times = FakeTimes(TASKS)  # type: ignore[assignment]
    collector._known = (("TASK-1",), {})

    collector.collect()

    assert _by_place(tmp_path, "TASK-1") == {}


def test_sessions_without_logs_here_are_listed_with_their_host(
    tmp_path: Path, home: Path
) -> None:
    _log(
        tmp_path,
        _invocation(
            "2026-11-01T10:02:00Z",
            "next",
            "TASK-1",
            session="elsewhere",
            host="container-7",
        ),
    )
    collector = _collector(tmp_path, home, TASKS)

    result = collector.collect()
    usage = task_usage(
        UsageStore(Storage(tmp_path)), "TASK-1", {}, collector.task_sessions("TASK-1")
    )

    assert result.unreadable == ("claudecode:elsewhere",)
    assert [(s.session, s.host) for s in usage.unreadable] == [
        ("claudecode:elsewhere", "container-7")
    ]
    assert "ran on container-7" in render_task_usage(usage)


def test_log_roots_find_a_session_from_a_mounted_home(tmp_path: Path) -> None:
    mounted = tmp_path / "mounted"
    _log(tmp_path, _invocation("2026-11-01T10:02:00Z", "next", "TASK-1"))
    claude_session(
        mounted,
        [
            claude_assistant(
                "2026-11-01T10:02:00Z",
                "m1",
                usage=_tokens(5),
                bash="./ww next TASK-1 --role manager",
            )
        ],
        directory="-workspace",
    )

    _collector(tmp_path, tmp_path / "empty", TASKS, log_roots=("mounted",)).collect()

    assert _by_place(tmp_path, "TASK-1") == {("develop", "manager", SESSION): 5}


def test_rescan_reads_a_session_again_replacing_its_records(
    tmp_path: Path, home: Path
) -> None:
    _log(tmp_path, _invocation("2026-11-01T10:02:00Z", "next", "TASK-1"))
    claude_session(
        home,
        [
            claude_assistant(
                "2026-11-01T10:02:00Z",
                "m1",
                usage=_tokens(5),
                bash="./ww next TASK-1 --role manager",
            )
        ],
    )
    _collector(tmp_path, home, TASKS).collect()

    _collector(tmp_path, home, TASKS).collect(
        everything=True, rescan={f"claudecode:{SESSION}"}
    )

    assert _by_place(tmp_path, "TASK-1") == {("develop", "manager", SESSION): 5}


def test_switched_off_usage_reads_nothing(tmp_path: Path, home: Path) -> None:
    _log(tmp_path, _invocation("2026-11-01T10:02:00Z", "next", "TASK-1"))
    claude_session(
        home, [claude_assistant("2026-11-01T10:02:00Z", "m1", usage=_tokens(5))]
    )

    result = _collector(tmp_path, home, TASKS, enabled=False).collect()

    assert result.files == 0
    assert not (tmp_path / ".ww" / "usage").exists()


def test_a_hook_turn_goes_to_the_latest_command_without_a_session(
    tmp_path: Path, home: Path
) -> None:
    _log(tmp_path, _invocation("2026-11-01T11:50:00Z", "next", "TASK-1", session=None))
    collector = _collector(tmp_path, home, TASKS)
    turn = TurnUsage("conv-1", "gen-1", "gpt-x", TokenCounts(10, 0, 5, 3))
    cursor = collector.agents["cursor"]

    collector.record_turn(cursor, turn)
    collector.record_turn(cursor, turn)

    stretches = UsageStore(Storage(tmp_path)).task_records("TASK-1")
    assert [(s.step, s.role, s.source, s.counts.total) for s in stretches] == [
        ("review", "manager", "hook", 18)
    ]


def test_prices_turn_tokens_into_cost(tmp_path: Path, home: Path) -> None:
    _log(tmp_path, _invocation("2026-11-01T10:02:00Z", "next", "TASK-1"))
    claude_session(
        home,
        [
            claude_assistant(
                "2026-11-01T10:02:00Z",
                "m1",
                usage=claude_usage(
                    input=1_000_000, write=0, read=1_000_000, output=1_000_000
                ),
                bash="./ww next TASK-1 --role manager",
            )
        ],
    )
    _collector(tmp_path, home, TASKS).collect()
    prices = {
        "claude-opus-5-5": UsagePrice(
            input=5, output=25, cache_read=0.5, cache_write=6.25
        )
    }

    usage = task_usage(UsageStore(Storage(tmp_path)), "TASK-1", prices, {})

    assert usage.total.cost == pytest.approx(30.5)
    assert "$30.50" in render_task_usage(usage)


def test_the_claude_reader_is_the_one_collected_with() -> None:
    assert ClaudeCode().session_variable == "CLAUDE_CODE_SESSION_ID"


class VendorAgent(Agent):
    """An extension's agent that writes Claude Code's log format."""

    key = "vendor"
    title = "Vendor"
    session_variable = "VENDOR_SESSION_ID"
    usage = ClaudeCode.usage


def test_an_extension_agent_with_a_usage_reader_is_collected_under_its_own_name(
    tmp_path: Path, home: Path
) -> None:
    name = "ext/acme/vendor/agents:vendor"
    agent = VendorAgent(name)
    found = session_of({"VENDOR_SESSION_ID": SESSION}, (agent,))
    assert found is not None and found[0] is agent
    record = _invocation("2026-11-01T10:02:00Z", "next", "TASK-1")
    _log(tmp_path, {**record, "agent": name})
    claude_session(
        home,
        [
            claude_assistant(
                "2026-11-01T10:02:00Z",
                "m1",
                usage=_tokens(5),
                bash="./ww next TASK-1 --role manager",
            )
        ],
    )

    _collector(tmp_path, home, TASKS, agents=(agent,)).collect()

    (stretch,) = UsageStore(Storage(tmp_path)).task_records("TASK-1")
    assert (stretch.agent, stretch.file) == (name, f"{name}:{SESSION}")
    assert _collector(tmp_path, home, TASKS).collect().files == 0
