# SPDX-License-Identifier: GPL-3.0-or-later
"""The core ``catchall`` workflow: a plain prompt, recorded by ww."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.cli import main
from ww.errors import StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"


def _service(tmp_path: Path) -> WorkflowService:
    config_file = tmp_path / "ww-agentic-workflows.yaml"
    config_file.write_text("workflows: []\n", encoding="utf-8")
    return WorkflowService(Storage(tmp_path))


def _start(service: WorkflowService, request: str) -> str:
    return MarkdownOutputAdapter().render_instruction(
        service.start(
            "catchall",
            TASK,
            agent="claudecode",
            init_artifact=request,
            caller_role="manager",
        )
    )


def test_the_session_that_got_the_prompt_does_the_work(tmp_path: Path) -> None:
    service = _service(tmp_path)
    md = MarkdownOutputAdapter()

    dispatch = _start(service, "Fix the typo in the README.")
    assert "no worker is selected" in dispatch
    assert "to the selected worker" not in dispatch
    assert "--selected-agent" not in dispatch
    assert "./ww next TASK-1 --role manager\n" in dispatch

    work = md.render_instruction(service.next(TASK, caller_role="manager"))
    assert "## Manager: perform the `work` assignment" in work
    assert "Select the worker" not in work
    assert "### Worker bootstrap" not in work
    assert "Fix the typo in the README." in work
    assert "exactly as you would if the user had asked you directly" in work

    service.complete(
        TASK,
        artifact="Fixed the typo.",
        caller_role="worker",
        summary_for_next="Fixed the typo.",
    )
    done = service.complete(
        TASK,
        artifact="Fixed a README typo.",
        variables={"summary": "Fixed a README typo."},
        caller_role="worker",
    )
    assert done.status == "completed"


def test_a_new_request_on_the_same_task_starts_a_new_run(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _start(service, "Rename the helper.")

    # The first request was never finished; the next one replaces it.
    assert "## Manager: dispatch the `work` assignment" in _start(
        service, "Rename it back."
    )


# --------------------------------------------------------------------------- #
# lookup: which task the change belongs to
# --------------------------------------------------------------------------- #


def _lookup_project(tmp_path: Path, task_format: str = "FORMS-{digit}") -> Path:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        f"task_format: {task_format}\n"
        "workflows:\n  - name: task\n    steps:\n      - develop: Develop.\n",
        encoding="utf-8",
    )
    return tmp_path


def _lookup(
    root: Path, capsys: pytest.CaptureFixture[str], *arguments: str
) -> dict[str, object]:
    assert main(["--root", str(root), "lookup", *arguments, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_lookup_maps_a_bare_number_onto_the_task_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _lookup_project(tmp_path)
    service = WorkflowService(Storage(root))
    service.start("catchall", "FORMS-12345", agent="codex", init_artifact="One.")

    report = _lookup(root, capsys, "12345", "--agent", "codex")

    # Its only run is an unfinished catch-all, which a new start replaces.
    assert report["outcome"] == "start"
    assert report["command"] == (
        "./ww start FORMS-12345 --workflow catchall --agent codex "
        '--init-artifact "<the request, normalized>" --role manager'
    )


def test_lookup_sends_a_change_to_the_task_s_unfinished_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _lookup_project(tmp_path)
    WorkflowService(Storage(root)).start(
        "task", "FORMS-7", agent="codex", init_artifact="Build it."
    )

    report = _lookup(root, capsys, "forms-7", "--agent", "codex")

    assert report["outcome"] == "continue"
    assert report["command"] == "./ww instruction FORMS-7 --role manager"
    # Starting the catch-all there anyway is refused with the same way out.
    with pytest.raises(StateError, match="continue it with ./ww instruction FORMS-7"):
        WorkflowService(Storage(root)).start(
            "catchall", "FORMS-7", agent="codex", init_artifact="Also this."
        )


def test_lookup_asks_the_operator_before_a_never_seen_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _lookup_project(tmp_path)

    report = _lookup(root, capsys, "99", "--agent", "codex")

    assert report["outcome"] == "confirm"
    assert report["choice_mechanism"] == "request_user_input"
    assert [choice["label"] for choice in report["choices"]] == [
        "Create FORMS-99",
        "Work without ww",
    ]
    assert report["choices"][0]["command"].startswith("./ww start FORMS-99 ")
    assert report["choices"][1]["command"] is None
    assert not (root / ".ww/tasks/FORMS-99").exists()

    assert main(["--root", str(root), "lookup", "99", "--agent", "claudecode"]) == 0
    page = capsys.readouterr().out
    assert "`AskUserQuestion` tool" in page
    assert "1. **Create FORMS-99**" in page
    assert "Run a command only after the operator picked its choice" in page


def test_lookup_lets_the_operator_pick_between_matching_tasks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _lookup_project(tmp_path, "explicit")
    service = WorkflowService(Storage(root))
    for task_id in ("OPS-7", "WEB-7"):
        service.start("catchall", task_id, agent="kimi", init_artifact="One.")

    report = _lookup(root, capsys, "7", "--agent", "kimi")

    assert report["outcome"] == "choose"
    assert report["choice_mechanism"] == "plain text"
    assert [choice["label"] for choice in report["choices"]] == [
        "Use OPS-7",
        "Use WEB-7",
        "Work without ww",
    ]


@pytest.mark.parametrize(
    ("task_format", "command"),
    [
        ("FORMS-{digit}", "./ww start --workflow catchall"),
        ("explicit", "./ww start <task-id> --workflow catchall"),
    ],
)
def test_lookup_without_a_task_asks_before_creating_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], task_format: str, command: str
) -> None:
    root = _lookup_project(tmp_path, task_format)

    report = _lookup(root, capsys, "--agent", "gemini")

    assert report["outcome"] == "confirm"
    assert report["choice_mechanism"] == "ask_user"
    assert report["choices"][0]["command"].startswith(command)


def test_lookup_refuses_when_the_catchall_is_switched_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _lookup_project(tmp_path)
    (root / "ww-agentic-workflows.json").write_text(
        json.dumps({"workflows": {"catchall": {"enabled": False}}}), encoding="utf-8"
    )

    assert main(["--root", str(root), "lookup", "1", "--agent", "codex"]) == 1
    assert "catchall workflow is switched off" in capsys.readouterr().err
