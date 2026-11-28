# SPDX-License-Identifier: GPL-3.0-or-later
"""Terminal runs must prove all the work is automated before any handler starts."""

import json
import shlex
from pathlib import Path

import pytest

from ww.cli import main
from ww.service import WorkflowService
from ww.storage import Storage

START = """hooks:
  before_start_workflow:
    - shell: echo hook >> trace
workflows:
  - name: release
    steps:
      - build: ~
        shell: echo build >> trace
"""


@pytest.mark.parametrize("custom", [False, True])
def test_terminal_run_records_complete_workflow(tmp_path: Path, capsys, custom: bool):
    (tmp_path / "ww.yaml").write_text(
        START
        + """      - publish: ~
        handlers:
          - shell: echo publish >> trace
          - shell: echo done >> trace
"""
    )
    args = ["--root", str(tmp_path), "run", "release", "--json"]
    if custom:
        args.extend(
            ["--description", "Monthly release", "--requirements", "Publish it."]
        )

    assert main(args) == 0
    page = json.loads(capsys.readouterr().out)
    service = WorkflowService(Storage(tmp_path))
    state, plan = service.load(page["task_id"])
    assert state.status == "completed"
    assert state.agent == "custom:terminal-invoked"
    assert state.description == ("Monthly release" if custom else "release")
    assert service.requirements(state.task_id).text == (
        "Publish it." if custom else "Run workflow release."
    )
    assert (tmp_path / "trace").read_text().splitlines() == [
        "hook",
        "build",
        "publish",
        "done",
    ]
    assert all(record.status == "completed" for record in state.item_executions)
    assert plan.plan.items[-1].summary
    artifact = state.item_executions[-1].artifact
    assert artifact
    assert "publish" in (tmp_path / artifact).read_text()


@pytest.mark.parametrize(
    ("tail", "reason"),
    [
        ("      - review: Review the code.", "review"),
        (
            "        variables: [target]\n      - choose: ~\n"
            '        handoff_to: "{{target}}"',
            "handoff",
        ),
        ("      - again: ~\n        handoff_to: release", "handoff"),
        (
            "      - ship: ~\n        handoff_to: publish\n"
            "  - name: publish\n    steps:\n      - again: ~\n"
            "        handoff_to: finish\n"
            "  - name: finish\n    steps:\n      - done: ~\n        shell: 'true'",
            "only once",
        ),
        (
            "      - approve: ~\n        shell: 'true'\n"
            "        variables: [{approval: Enter approval}]",
            "requires input",
        ),
        ("      - assess:\n          assess: Ship?", "assessment"),
        (
            "      - review: Review changes.\n        rules: [Use clear names.]",
            "verdict",
        ),
        (
            "      - verify: ~\n        shell: 'true'\n        on_failure: fix",
            "agent repair",
        ),
        (
            "        hooks:\n          after_complete:\n"
            "            - inspect: Review the result.",
            "inspect",
        ),
        (
            "      - ship: ~\n        handoff_to: publish\n"
            "  - name: publish\n    steps:\n      - approval: Approve it.",
            "approval",
        ),
    ],
)
def test_terminal_preflight_has_no_workflow_side_effects(
    tmp_path: Path, capsys, tail: str, reason: str
):
    (tmp_path / "ww.yaml").write_text(START + tail + "\n")
    assert main(["--root", str(tmp_path), "run", "release"]) == 1
    message = capsys.readouterr().err
    assert reason in message
    assert "start" in message
    assert not (tmp_path / "trace").exists()
    assert not list((tmp_path / ".ww/tasks").glob("*/state.json"))


@pytest.mark.parametrize("runtime", ["single", "auto"])
def test_terminal_run_static_handoff(tmp_path: Path, capsys, runtime: str):
    (tmp_path / "ww.yaml").write_text(
        START
        + f"""      - ship: ~
        handoff_to: publish
  - name: publish
    runtime: {runtime}
    steps:
      - publish: ~
        shell: echo publish >> trace
"""
    )
    assert main(["--root", str(tmp_path), "run", "release", "--json"]) == 0
    page = json.loads(capsys.readouterr().out)
    assert page["workflow"] == "publish"
    assert page["status"] == "completed"
    assert (tmp_path / "trace").read_text().splitlines() == [
        "hook",
        "build",
        "hook",
        "publish",
    ]


def test_terminal_failure_returns_saved_retry_command(tmp_path: Path, capsys):
    (tmp_path / "ww.yaml").write_text(
        START
        + """      - publish: ~
        shell: test -f ready
      - finish: ~
        shell: echo finish >> trace
"""
    )
    args = ["--root", str(tmp_path)]
    assert main([*args, "run", "release", "--json"]) == 1
    page = json.loads(capsys.readouterr().out)
    assert page["status"] == "failed"
    task_id = page["task_id"]
    assert "test -f ready" in page["error"]
    assert (tmp_path / "trace").read_text().splitlines() == ["hook", "build"]
    retry = next(
        c["command"] for c in page["recovery_commands"] if c["action"] == "retry"
    )
    assert task_id in retry
    (tmp_path / "ready").touch()
    assert main([*args, *shlex.split(retry)[1:]]) == 0
    state, snapshot = WorkflowService(Storage(tmp_path)).load(task_id)
    publish = next(
        i for i, step in enumerate(snapshot.plan.items) if step.name == "publish"
    )
    assert state.item_executions[publish].status == "completed"
