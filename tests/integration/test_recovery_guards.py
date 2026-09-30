# SPDX-License-Identifier: GPL-3.0-or-later
"""Operator guards on ``recover`` for interrupted automatic handlers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.errors import StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"


def _interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkflowService:
    """Leave a checked command hook interrupted with an unknown outcome."""
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        hooks:
          after_complete:
            - name: publish
              argv: [printf, published]
              assert:
                - equals: published
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", TASK, agent="codex")
    service.next(TASK)

    def interrupted(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", interrupted)
        with pytest.raises(KeyboardInterrupt):
            service.complete(TASK, artifact="done", summary_for_next="Done.")
    resumed = WorkflowService(Storage(tmp_path))
    assert resumed.next(TASK).status == "interrupted"
    return resumed


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"retry": True, "mark_succeeded": True}, "choose only one of --retry"),
        ({"output": "x"}, "require --mark-succeeded"),
        ({"working_directory": "."}, "require --mark-succeeded"),
        ({"variables": (("a", "b"),)}, "require --mark-succeeded"),
    ],
)
def test_conflicting_flags_are_rejected_before_loading(
    tmp_path: Path, options: dict[str, object], message: str
) -> None:
    service = WorkflowService(Storage(tmp_path))

    with pytest.raises(StateError, match=message):
        service.recover(TASK, **options)  # type: ignore[arg-type]


def test_recover_outside_an_interruption_only_reports(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", TASK, agent="codex")

    assert service.recover(TASK).item_name == "work"
    for options in ({"retry": True}, {"mark_succeeded": True}):
        with pytest.raises(StateError, match="task has no interrupted automatic"):
            service.recover(TASK, **options)  # type: ignore[arg-type]


def test_inspecting_an_unknown_outcome_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _interrupted(tmp_path, monkeypatch)

    inspected = service.recover(TASK)

    assert inspected.status == "interrupted"
    assert inspected.item_name == "publish"
    assert [command.action for command in inspected.recovery_commands] == [
        "retry",
        "force",
    ]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (
            {"mark_succeeded": True},
            "--output is required to attest a CLI command with an assertion",
        ),
        (
            {"mark_succeeded": True, "output": "published", "working_directory": "."},
            "--working-directory is not supported for this action",
        ),
        (
            {"mark_succeeded": True, "output": "published", "variables": (("a", "b"),)},
            "--variable is not supported for this action",
        ),
    ],
)
def test_command_attestation_needs_output_and_nothing_else(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, object],
    message: str,
) -> None:
    service = _interrupted(tmp_path, monkeypatch)

    with pytest.raises(StateError, match=message):
        service.recover(TASK, **options)  # type: ignore[arg-type]
    assert service.status(TASK).status == "interrupted"


def test_attested_output_settles_the_command_without_running_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _interrupted(tmp_path, monkeypatch)

    settled = service.recover(TASK, mark_succeeded=True, output="published")

    assert (settled.item_name, settled.item_status) == (
        "update-workflow-summary",
        "pending",
    )


def test_retry_replays_the_handler_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _interrupted(tmp_path, monkeypatch)

    retried = service.recover(TASK, retry=True)

    assert retried.item_name == "update-workflow-summary"


def test_a_failed_handler_shows_the_operator_recovery_commands(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        hooks:
          after_complete:
            - name: publish
              argv: [test, -e, ready.txt]
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", TASK, agent="codex")
    service.next(TASK)

    failed = service.complete(TASK, artifact="done", summary_for_next="Done.")

    assert failed.status == "failed"
    rendered = MarkdownOutputAdapter().render_instruction(failed)
    assert "Do not retry on your own" in rendered
    assert "### Operator recovery" in rendered
    assert "./ww next TASK-1 --retry --yes --role manager" in rendered
    assert '--force --reason "<reason>" --yes' in rendered
    # Once the operator has fixed the cause and says so, retry re-runs it.
    (tmp_path / "ready.txt").write_text("", encoding="utf-8")
    retried = service.next(TASK, retry=True)
    assert retried.status != "failed"
    assert retried.item_name == "update-workflow-summary"


def test_a_failed_handler_hands_the_decision_to_the_operator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The page must set expectations, not just list two commands."""
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """
handlers:
  - name: tests
    argv: [python3, -c, "import sys; print('2 failed'); sys.exit(1)"]

hooks:
  before_complete:
    - workflows: [task]
      steps: [develop]
      handlers:
        - name: tests

workflows:
  - name: task
    steps:
      - develop: Implement the change.
      - document: Write it up.
""",
        encoding="utf-8",
    )
    root = ["--root", str(tmp_path)]
    assert (
        main(
            [
                *root,
                "start",
                "T-1",
                "-w",
                "task",
                "-a",
                "claudecode",
                "--requirements",
                "Add retries.",
                "--role",
                "manager",
            ]
        )
        == 0
    )
    main([*root, "next", "T-1", "--role", "manager"])
    capsys.readouterr()
    main(
        [
            *root,
            "complete",
            "T-1",
            "--role",
            "worker",
            "--artifact",
            "Done.",
            "--summary",
            "Retries added.",
        ]
    )
    page = capsys.readouterr().out

    # What the handler actually printed, so the operator can decide at all.
    assert "2 failed" in page
    # What the agent must tell them, and that it must then stop.
    assert "Then wait for their choice" in page
    assert "the work so far is saved" in page
    assert "do not pick for them" in page
    # Both routes out, with the force needing a recorded reason.
    assert "--retry --yes --role manager" in page
    assert '--force --reason "<reason>" --yes' in page
