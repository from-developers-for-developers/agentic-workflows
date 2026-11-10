# SPDX-License-Identifier: GPL-3.0-or-later
"""Token usage over time: the ww version on each record, what was true when a
run started, and the per-run and per-step points the visualize page charts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.unit.test_usage_collector import (
    TASKS,
    _collector,
    _invocation,
    _log,
    _tokens,
    home,  # noqa: F401 - the fixture the collection tests share
)
from tests.usage_logs import claude_assistant, claude_session, claude_user
from ww.agents import TokenCounts
from ww.cli import main
from ww.storage import Storage
from ww.usage import RunFacts, Stretch, UsageStore, map_counts, note_run, usage_stats

MAP = """name: app
summary: The application.
parts:
  - src/app/
"""


def _stretch(
    task: str,
    run: str,
    step: str | None,
    model: str,
    version: str | None,
    first: str,
    tokens: int,
) -> Stretch:
    return Stretch(
        task=task,
        run=run,
        step=step,
        role="worker",
        agent="claudecode",
        session="s",
        file="claudecode:s",
        model=model,
        source="transcript",
        first=first,
        last=first,
        counts=TokenCounts(input=tokens),
        ww_version=version,
    )


def test_each_record_carries_the_version_of_the_ww_that_placed_it(
    tmp_path: Path,
    home: Path,  # noqa: F811 - the shared fixture
) -> None:
    _log(
        tmp_path,
        {
            **_invocation("2026-11-01T10:00:30Z", "start", "TASK-1"),
            "ww_version": "1.0.0b17",
        },
        # The operator upgraded ww while the task ran.
        {
            **_invocation("2026-11-01T10:10:30Z", "next", "TASK-1"),
            "ww_version": "1.0.0b18",
        },
    )
    claude_session(
        home,
        [
            claude_user("2026-11-01T09:59:00Z"),
            claude_assistant(
                "2026-11-01T10:00:29Z",
                "m1",
                usage=_tokens(7),
                bash="./ww start --workflow task --role manager",
            ),
            claude_assistant("2026-11-01T10:05:00Z", "m2", usage=_tokens(11)),
            claude_assistant(
                "2026-11-01T10:10:29Z",
                "m3",
                usage=_tokens(13),
                bash="./ww next TASK-1 --role manager",
            ),
            claude_assistant("2026-11-01T10:12:00Z", "m4", usage=_tokens(17)),
        ],
    )

    _collector(tmp_path, home, TASKS).collect()

    by_version: dict[str | None, int] = {}
    for stretch in UsageStore(Storage(tmp_path)).task_records("TASK-1"):
        by_version[stretch.ww_version] = (
            by_version.get(stretch.ww_version, 0) + stretch.counts.total
        )
    # The response that writes `next` still answers the older ww's page, like
    # the step it belongs to; tokens before `start` take its version.
    assert by_version == {"1.0.0b17": 7 + 11 + 13, "1.0.0b18": 17}


def test_records_written_before_versions_were_kept_still_load(tmp_path: Path) -> None:
    store = UsageStore(Storage(tmp_path))
    path = store.task_path("TASK-1")
    path.parent.mkdir(parents=True)
    old = _stretch("TASK-1", "01-task", "develop", "m", None, "2026-10-01T10:00:00Z", 5)
    data = old.to_dict()
    assert "ww_version" not in data
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")

    (loaded,) = store.task_records("TASK-1")

    assert loaded == old
    assert usage_stats(store)["runs"][0]["ww_version"] == "unknown"


def test_a_run_is_noted_once_with_the_maps_the_project_had(tmp_path: Path) -> None:
    (tmp_path / "src/app").mkdir(parents=True)
    for name in ("a.py", "b.py"):
        (tmp_path / "src/app" / name).write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp_path / ".ww/features").mkdir(parents=True)
    (tmp_path / ".ww/features/app.yaml").write_text(MAP, encoding="utf-8")
    storage = Storage(tmp_path)

    assert map_counts(tmp_path) == (1, 2)
    assert note_run(storage, "TASK-1", "01-task", "task", "2026-11-01T10:00:00Z")
    assert not note_run(
        storage,
        "TASK-1",
        "01-task",
        "task",
        "2026-11-02T10:00:00Z",
        counts=lambda _: (9, 9),
    )
    (facts,) = UsageStore(storage).run_facts().values()
    assert (facts.workflow, facts.started, facts.feature_maps, facts.mapped_files) == (
        "task",
        "2026-11-01T10:00:00Z",
        1,
        2,
    )
    assert facts.ww_version
    assert map_counts(tmp_path / "missing") == (0, 0)


def test_runs_and_steps_become_points_by_model_and_version(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    store = UsageStore(storage)
    store.append(
        [
            _stretch(
                "TASK-1",
                "01-task",
                "develop",
                "opus",
                "b17",
                "2026-11-01T10:05:00Z",
                100,
            ),
            _stretch(
                "TASK-1",
                "01-task",
                "review",
                "sonnet",
                "b17",
                "2026-11-01T10:20:00Z",
                30,
            ),
            _stretch(
                "TASK-1",
                "01-task",
                "review",
                "sonnet",
                "b18",
                "2026-11-01T10:25:00Z",
                10,
            ),
            _stretch(
                "TASK-2", "01-express", "do", "opus", "b18", "2026-11-03T09:00:00Z", 50
            ),
            # Outside any task, and empty stretches, are not charted.
            _stretch(
                "TASK-2", "01-express", None, "opus", "b18", "2026-11-03T09:30:00Z", 0
            ),
        ]
    )
    store.note_run(
        RunFacts("TASK-1", "01-task", "task", "2026-11-01T10:00:00Z", "b17", 3, 40)
    )

    stats = usage_stats(store)

    first, second = stats["runs"]
    assert (first["task"], first["workflow"], first["date"]) == (
        "TASK-1",
        "task",
        "2026-11-01T10:00:00Z",
    )
    assert first["tokens"]["input"] == 140
    assert {m: c["input"] for m, c in first["models"].items()} == {
        "opus": 100,
        "sonnet": 40,
    }
    assert {v: c["input"] for v, c in first["versions"].items()} == {
        "b17": 130,
        "b18": 10,
    }
    assert first["ww_version"] == "b17"
    assert (first["feature_maps"], first["mapped_files"]) == (3, 40)
    # Without facts the workflow comes from the run's ID, the date from its tokens.
    assert (second["workflow"], second["date"], second["feature_maps"]) == (
        "express",
        "2026-11-03T09:00:00Z",
        None,
    )
    assert [
        (s["workflow"], s["step"], s["tokens"]["input"]) for s in stats["steps"]
    ] == [
        ("task", "develop", 100),
        ("task", "review", 40),
        ("express", "do", 50),
    ]
    assert stats["versions"] == [
        {"version": "b17", "first": "2026-11-01T10:00:00Z"},
        {"version": "b18", "first": "2026-11-01T10:00:00Z"},
    ]
    assert stats["models"] == ["opus", "sonnet"]


def test_starting_a_task_notes_its_run(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Do it.\n",
        encoding="utf-8",
    )

    code = main(
        [
            "--root",
            str(tmp_path),
            "start",
            "TASK-1",
            "--workflow",
            "task",
            "--agent",
            "claudecode",
            "--role",
            "manager",
            "--requirements",
            "Do it.",
            "--description",
            "Do it",
        ]
    )

    assert code == 0
    (facts,) = UsageStore(Storage(tmp_path)).run_facts().values()
    assert (facts.task, facts.workflow, facts.feature_maps) == ("TASK-1", "task", 0)
