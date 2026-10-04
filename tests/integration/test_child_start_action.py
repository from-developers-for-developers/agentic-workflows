# SPDX-License-Identifier: GPL-3.0-or-later
"""``start_child``: ww starts a per-child stage's child from the child's record."""

import json
from pathlib import Path

import pytest

from ww.cli import main
from ww.errors import ConfigurationError
from ww.service import WorkflowService
from ww.storage import Storage

START_FIELDS = """
                start_child:
                  workflow: "{{ww.child.field.workflow}}"
                  runtime: "{{ww.child.field.runtime}}"
                  model: "{{ww.child.field.model}}"
                  reasoning: "{{ww.child.field.reasoning}}"
                  agent: "{{ww.child.field.agent}}"
"""


def _config(tmp_path: Path, launch: str = START_FIELDS) -> None:
    (tmp_path / "ww.yaml").write_text(
        f"""workflows:
  - parent: Parent work.
    steps:
      - split: Collect children.
        role: manager
        children:
          steps:
            - implement:
                workflow: child{launch}
            - review: Review the child.
              role: manager
  - child: Child work.
    steps:
      - work: Implement the child.
  - express: Express child work.
    steps:
      - quick: Do it fast.
""",
        encoding="utf-8",
    )


def _at_implement(
    tmp_path: Path, fields: tuple[tuple[str, str], ...] = (), launch: str = START_FIELDS
) -> WorkflowService:
    _config(tmp_path, launch)
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "parent",
        "P",
        model="sol",
        reasoning="medium",
        workflow_runtime="auto",
        caller_role="manager",
    )
    service.next("P", caller_role="manager")
    service.add_child("P", "A", "Child requirements.", fields=fields)
    service.complete(
        "P", artifact="Collected.", summary_for_next="Ready.", caller_role="manager"
    )
    return service


def test_next_starts_the_child_from_every_field(tmp_path: Path) -> None:
    service = _at_implement(
        tmp_path,
        (
            ("workflow", "express"),
            ("runtime", "single"),
            ("model", "claude-opus-5-5"),
            ("reasoning", "high"),
            ("agent", "claudecode"),
        ),
    )
    page = service.next("P", caller_role="manager")
    assert page.task_id == "P/A"
    assert any("ww started child `A` for step `implement`" in n for n in page.notices)
    state, _ = service.load("P/A")
    assert (state.workflow, state.workflow_runtime, state.agent) == (
        "express",
        "single",
        "claudecode",
    )
    assert (state.model, state.reasoning) == ("claude-opus-5-5", "high")
    child = service.tasks.read_children("P", "01-parent")[0]
    assert (child.status, child.workflow) == ("in_progress", "express")
    status = service.task_status("P/A")
    assert (status.model, status.reasoning, status.agent) == (
        "claude-opus-5-5",
        "high",
        "claudecode",
    )


def test_absent_or_empty_fields_inherit_as_start_child_does(tmp_path: Path) -> None:
    service = _at_implement(tmp_path, (("model", ""),))
    service.next("P", caller_role="manager")
    state, _ = service.load("P/A")
    parent, _ = service.load("P")
    assert (state.workflow, state.workflow_runtime) == ("child", "auto")
    assert (state.model, state.reasoning, state.agent) == (
        parent.model,
        parent.reasoning,
        parent.agent,
    )


def test_values_are_read_when_the_step_runs(tmp_path: Path) -> None:
    service = _at_implement(tmp_path)
    service.update_child("P", "A", fields=(("model", "late-model"),))
    service.next("P", caller_role="manager")
    state, _ = service.load("P/A")
    assert state.model == "late-model"


def test_cli_markdown_page_names_the_started_child(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _at_implement(tmp_path, (("runtime", "single"),))
    assert main(["--root", str(tmp_path), "next", "P", "--role", "manager"]) == 0
    page = capsys.readouterr().out
    assert "ww started child `A` for step `implement` of `P`" in page
    assert "# P/A" in page
    assert (
        main(["--root", str(tmp_path), "next", "P", "--role", "manager", "--json"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["task_id"] == "P"


def test_a_launch_failure_fails_the_step_and_retry_starts_the_child(
    tmp_path: Path,
) -> None:
    service = _at_implement(tmp_path, (("workflow", "missing"),))
    page = service.next("P", caller_role="manager")
    assert page.status == "failed"
    assert "could not start child 'A' for step 'implement'" in (page.error or "")
    assert "workflow not found: missing" in (page.error or "")
    child = service.tasks.read_children("P", "01-parent")[0]
    assert child.status == "pending"
    service.update_child("P", "A", fields=(("workflow", "express"),))
    retried = service.next("P", retry=True, caller_role="manager")
    assert retried.task_id == "P/A"
    state, _ = service.load("P/A")
    assert state.workflow == "express"


def test_the_compiled_stage_records_its_launch(tmp_path: Path) -> None:
    service = _at_implement(tmp_path)
    _, snapshot = service.load("P")
    stage = next(
        item
        for item in snapshot.plan.items
        if item.name == "implement" and item.child_number == 1
    )
    assert stage.operation is not None
    assert stage.operation.to_dict()["launch"]["runtime"] == (  # type: ignore[union-attr, index]
        "{{ww.child.field.runtime}}"
    )


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            """workflows:
  - w: W.
    steps:
      - a: Do it.
        start_child: {}
""",
            "sets start_child outside the stage that runs the child",
        ),
        (
            """workflows:
  - w: W.
    steps:
      - split: Collect.
        children:
          steps:
            - go:
                workflow: c
            - other:
                start_child: {}
  - c: C.
    steps:
      - x: X.
""",
            "sets start_child outside the stage that runs the child",
        ),
        (
            """workflows:
  - w: W.
    steps:
      - split: Collect.
        children:
          steps:
            - go:
                workflow: c
                start_child:
                  model: "{{ww.child.git.branch}}"
  - c: C.
    steps:
      - x: X.
""",
            "does not exist before the child starts",
        ),
        (
            """workflows:
  - w: W.
    steps:
      - split: Collect.
        children:
          steps:
            - go:
                workflow: c
                start_child:
                  colour: red
  - c: C.
    steps:
      - x: X.
""",
            "colour",
        ),
    ],
)
def test_start_child_is_rejected_outside_the_child_run_stage(
    tmp_path: Path, config: str, message: str
) -> None:
    (tmp_path / "ww.yaml").write_text(config, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    with pytest.raises(ConfigurationError, match=message):
        service.start("w", "T", caller_role="manager")


def test_start_child_without_settings_starts_the_child_with_inheritance(
    tmp_path: Path,
) -> None:
    service = _at_implement(tmp_path, launch="\n                start_child: {}")
    service.next("P", caller_role="manager")
    state, _ = service.load("P/A")
    assert (state.workflow, state.model) == ("child", "sol")
