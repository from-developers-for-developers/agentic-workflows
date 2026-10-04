# SPDX-License-Identifier: GPL-3.0-or-later
"""Requirements print once, later pages point at them, and amendments always show."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.cli import main
from ww.errors import StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "T1"
REQUIREMENTS = "Add a --verbose flag to the CLI."
POINTER = "print them again with `./ww requirements T1`"
CONFIG = """workflows:
  - name: task
    steps:
      - develop: Implement the change.
      - review: Review the change.
"""


def _service(tmp_path: Path, requirements: str = REQUIREMENTS) -> WorkflowService:
    (tmp_path / "ww.yaml").write_text(CONFIG, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "task", TASK, agent="codex", init_artifact=requirements, caller_role="manager"
    )
    return service


def _finish(service: WorkflowService) -> None:
    while service.instruction(TASK).status != "completed":
        page = service.next(TASK)
        values = (
            (("summary", "All done."),)
            if page.item_name == "update-workflow-summary"
            else ()
        )
        service.complete(TASK, values, artifact="Done.", summary_for_next="Ok.")


def test_the_first_work_page_shows_the_requirements_and_later_pages_point_at_them(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    first = service.next(TASK)
    render = MarkdownOutputAdapter().render_instruction
    assert REQUIREMENTS in render(first)
    assert POINTER not in render(first)
    service.complete(TASK, artifact="Done.", summary_for_next="Added it.")
    second = service.next(TASK)
    page = render(second)
    assert REQUIREMENTS not in page
    assert POINTER in page
    # JSON keeps the requirements and gains the additive fields.
    data = second.to_dict()
    assert data["task_requirements"] == REQUIREMENTS
    assert data["requirements_in_full"] is False
    assert data["requirements_command"] == "./ww requirements T1"
    assert first.to_dict()["requirements_in_full"] is True


def test_requirements_command_prints_the_recorded_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _service(tmp_path)
    assert main(["--root", str(tmp_path), "requirements", TASK]) == 0
    assert REQUIREMENTS in capsys.readouterr().out
    assert main(["--root", str(tmp_path), "requirements", TASK, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "task_id": TASK,
        "requirements": REQUIREMENTS,
        "amendments": [],
    }


def test_a_paragraph_the_work_instruction_quotes_is_not_printed_twice(
    tmp_path: Path,
) -> None:
    quoted = "Rework the parser so that every token carries its source position. " * 2
    (tmp_path / "ww.yaml").write_text(
        f"""workflows:
  - name: task
    steps:
      - refine: "Refine this: {quoted.strip()}"
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    requirements = f"Short intro.\n\n{quoted.strip()}\n\nA closing remark."
    service.start("task", TASK, agent="codex", init_artifact=requirements)
    page = MarkdownOutputAdapter().render_instruction(service.next(TASK))
    assert page.count(quoted.strip()) == 1
    assert "(quoted in the work instruction below)" in page
    assert "Short intro." in page
    assert "A closing remark." in page


def test_amendments_append_and_show_on_every_page_newest_last(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    service.next(TASK)
    assert (
        main(
            [
                "--root", str(tmp_path), "amend", TASK,
                "--requirements", "Also cover the docs.", "--role", "manager",
            ]
        )
        == 0
    )  # fmt: skip
    capsys.readouterr()
    service.amend(TASK, "Skip the changelog.")
    render = MarkdownOutputAdapter().render_instruction
    first = render(service.instruction(TASK))
    assert REQUIREMENTS in first
    assert first.index("manager: Also cover the docs.") < first.index(
        "operator: Skip the changelog."
    )
    service.complete(TASK, artifact="Done.", summary_for_next="Added it.")
    later = render(service.next(TASK))
    assert POINTER in later
    assert "manager: Also cover the docs." in later
    assert "operator: Skip the changelog." in later
    # The original is never rewritten.
    recorded = service.requirements(TASK)
    assert recorded.text == REQUIREMENTS
    assert [entry.text for entry in recorded.amendments] == [
        "Also cover the docs.",
        "Skip the changelog.",
    ]
    assert main(["--root", str(tmp_path), "requirements", TASK]) == 0
    printed = capsys.readouterr().out
    assert "## Amendments, oldest first" in printed
    assert main(["--root", str(tmp_path), "status", TASK, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == TASK
    assert [e["role"] for e in later_json(service)] == ["manager", "operator"]


def later_json(service: WorkflowService) -> list[dict[str, str]]:
    data = service.instruction(TASK).to_dict()
    return data["task_amendments"]  # type: ignore[return-value]


def test_amend_refuses_empty_oversized_and_completed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(StateError, match="non-empty"):
        service.amend(TASK, "  ")
    with pytest.raises(StateError, match="short clarification"):
        service.amend(TASK, "x" * 1001)
    _finish(service)
    assert service.instruction(TASK).status == "completed"
    with pytest.raises(StateError, match="already completed"):
        service.amend(TASK, "Too late.")
    assert service.requirements(TASK).amendments == ()


def test_cli_refuses_amending_a_completed_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    _finish(service)
    assert main(["--root", str(tmp_path), "amend", TASK, "--requirements", "x"]) == 1
    assert "already completed" in capsys.readouterr().err
