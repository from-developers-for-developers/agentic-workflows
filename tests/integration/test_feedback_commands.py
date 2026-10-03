# SPDX-License-Identifier: GPL-3.0-or-later
"""Feedback commands are scoped to interactive work and respect opt-out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_interactive import _service
from tests.unit.test_feedback import _analysis
from ww.cli import main
from ww.errors import StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter


def test_interactive_instruction_and_cli_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    instruction = service.next("TASK-1")
    assert instruction.feedback_learning
    assert "Learn from operator feedback" in MarkdownOutputAdapter().render_instruction(
        instruction
    )
    service.interact(
        "TASK-1",
        transcript="Agent: I used naam.\nOperator: Use English names.",
        end=True,
    )
    analysis = tmp_path / "analysis.json"
    analysis.write_text(json.dumps(_analysis()))
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "feedback",
                "record",
                "TASK-1",
                "--analysis",
                str(analysis),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["points"][0]["occurrences"] == 1
    assert main(["--root", str(tmp_path), "feedback", "show", "TASK-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["entries"][1]["speaker"] == "operator"


def test_disabled_feedback_has_no_instruction_or_store_writes(tmp_path: Path) -> None:
    (tmp_path / "ww.json").write_text('{"feedback_learning": false}')
    service = _service(tmp_path)
    service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    instruction = service.next("TASK-1")
    assert not instruction.feedback_learning
    assert (
        "Learn from operator feedback"
        not in MarkdownOutputAdapter().render_instruction(instruction)
    )
    service.interact("TASK-1", operator="Fine", end=True)
    with pytest.raises(StateError, match="disabled"):
        service.record_feedback("TASK-1", [])
    assert not service.feedback.path.exists()
