# SPDX-License-Identifier: GPL-3.0-or-later
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ww.storage_adapters.filesystem import FileTaskStorageAdapter

_RUNNER = "import sys; from ww.cli import main; sys.exit(main(sys.argv[1:]))"

_SUMMARY = ("--summary", "ok")


def _project(root: Path) -> None:
    (root / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: develop
        description: Write the code.
""",
        encoding="utf-8",
    )


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, "--root", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _run_together(
    root: Path, invocations: list[tuple[str, ...]]
) -> list[subprocess.CompletedProcess[str]]:
    with ThreadPoolExecutor(max_workers=len(invocations)) as pool:
        futures = [pool.submit(_run, root, *arguments) for arguments in invocations]
        return [future.result() for future in futures]


def test_parallel_processes_append_whole_log_records(tmp_path: Path) -> None:
    _project(tmp_path)

    results = _run_together(tmp_path, [("workflows",)] * 8)

    assert [result.returncode for result in results] == [0] * 8
    lines = (tmp_path / ".ww/executions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 16
    assert all(json.loads(line)["command"] == "workflows" for line in lines)
    assert {json.loads(line)["outcome"] for line in lines} == {"started", "ok"}


def test_parallel_next_hands_the_step_to_exactly_one_process(tmp_path: Path) -> None:
    _project(tmp_path)
    started = _run(
        tmp_path,
        "start",
        "TASK-1",
        "--workflow",
        "task",
        "--agent",
        "codex",
        "--requirements",
        "requirements",
    )
    assert started.returncode == 0, started.stderr

    results = _run_together(tmp_path, [("next", "TASK-1")] * 3)

    # Each process waits for the lock and then sees the step already open;
    # in the single runtime that is answered with the step's page, not an
    # error, and the step is started exactly once.
    assert [result.returncode for result in results] == [0, 0, 0]
    assert all("Write the code." in result.stdout for result in results)

    state = FileTaskStorageAdapter(tmp_path).read_execution_state(
        "TASK-1", "01-task"
    )
    assert state is not None and state.status == "in_progress"
    assert [record.status for record in state.item_executions].count("in_progress") == 1


def test_parallel_starts_reserve_distinct_generated_task_ids(tmp_path: Path) -> None:
    _project(tmp_path)

    results = _run_together(
        tmp_path,
        [
            (
                "start",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--requirements",
                "requirements",
            )
        ]
        * 3,
    )

    assert [result.returncode for result in results] == [0, 0, 0]
    tasks = sorted(path.name for path in (tmp_path / ".ww" / "tasks").iterdir())
    assert len(tasks) == 3
    for name in tasks:
        state = json.loads(
            (tmp_path / ".ww" / "tasks" / name / "state.json").read_text(
                encoding="utf-8"
            )
        )
        assert state["task_id"] == name


def test_parallel_completes_record_one_artifact_per_step(tmp_path: Path) -> None:
    _project(tmp_path)
    assert (
        _run(
            tmp_path,
            "start",
            "TASK-2",
            "--workflow",
            "task",
            "--agent",
            "codex",
            "--requirements",
            "requirements",
        )
    ).returncode == 0
    assert (_run(tmp_path, "next", "TASK-2")).returncode == 0

    results = _run_together(
        tmp_path,
        [
            ("complete", "TASK-2", "--artifact", "first", *_SUMMARY),
            ("complete", "TASK-2", "--artifact", "second", *_SUMMARY),
        ],
    )

    assert sum(result.returncode == 0 for result in results) == 1
    artifacts = sorted(
        path.name
        for path in (tmp_path / ".ww/tasks/TASK-2/runs/01-task/steps").iterdir()
    )
    assert artifacts == ["01-init.md", "02-develop.md"]


def test_status_never_observes_a_half_written_task(tmp_path: Path) -> None:
    _project(tmp_path)
    assert (
        _run(
            tmp_path,
            "start",
            "TASK-3",
            "--workflow",
            "task",
            "--agent",
            "codex",
            "--requirements",
            "requirements",
        )
    ).returncode == 0
    assert (_run(tmp_path, "next", "TASK-3")).returncode == 0

    results = _run_together(
        tmp_path,
        [("complete", "TASK-3", "--artifact", "done", "--summary", "ok")]
        + [("status", "TASK-3")] * 5,
    )
    readers = results[1:]

    assert all(result.returncode in {0, 1} for result in readers)
    assert not any("Traceback" in result.stderr for result in readers)


def test_status_does_not_wait_for_a_writer_that_is_mid_command(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: slow
    argv: [sleep, "1.0"]
workflows:
  - name: task
    steps:
      - name: work
        hooks:
          after_complete:
            - name: slow
""",
        encoding="utf-8",
    )
    assert (
        _run(
            tmp_path,
            "start",
            "TASK-5",
            "--workflow",
            "task",
            "--agent",
            "codex",
            "--requirements",
            "requirements",
        )
    ).returncode == 0
    assert (_run(tmp_path, "next", "TASK-5")).returncode == 0

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(
            _run,
            tmp_path,
            "complete",
            "TASK-5",
            "--artifact",
            "done",
            "--summary",
            "ok",
        )
        time.sleep(0.2)
        started = time.monotonic()
        reader = pool.submit(_run, tmp_path, "status", "TASK-5")
        reader_result = reader.result()
        elapsed = time.monotonic() - started
        writer_result = writer.result()

    # Reads are deliberately unlocked, so status answers while the writer is
    # still running its handler instead of queueing behind it.
    assert reader_result.returncode == 0, reader_result.stderr
    assert elapsed < 0.7
    assert writer_result.returncode == 0, writer_result.stderr
