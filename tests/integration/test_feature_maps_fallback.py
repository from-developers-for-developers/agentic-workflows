# SPDX-License-Identifier: GPL-3.0-or-later
"""A workflow without a ``write_feature_maps`` step: every agent step answers
for its own changes, and ``ww lint`` says so."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ww.cli import main
from ww.errors import StateError
from ww.features import read_task_list
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

PLAIN = """workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
      - name: review
        description: Review it.
"""
WITH_WRITER = """workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
      - name: update-maps
        description: Bring the maps up to date.
        write_feature_maps: true
"""
READONLY = """workflows:
  - name: task
    readonly: true
    steps:
      - name: develop
        description: Look at it.
"""
AGENTS = "name: agents\nsummary: Agent adapters.\nparts: [src/agents/]\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _project(root: Path, workflows: str) -> WorkflowService:
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    (root / "ww.json").write_text(json.dumps({"feature_maps": True}), "utf-8")
    (root / "src" / "agents").mkdir(parents=True)
    (root / "src" / "agents" / "codex.py").write_text("CODEX = 1\n", "utf-8")
    features = root / ".ww" / "features"
    features.mkdir(parents=True)
    (features / "agents.yaml").write_text(AGENTS, encoding="utf-8")
    (root / ".gitignore").write_text(".ww/*\n!.ww/features/\n", "utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.st")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    service = WorkflowService(Storage(root))
    service.start(
        "task", "TASK-1", agent="codex", init_artifact="Do it.", description="Tune"
    )
    return service


def _page(service: WorkflowService) -> str:
    return MarkdownOutputAdapter().render_instruction(service.instruction("TASK-1"))


def test_a_step_with_nothing_due_completes_as_before(tmp_path: Path) -> None:
    service = _project(tmp_path, PLAIN)
    develop = service.next("TASK-1")
    # The maps' own files never count.
    (tmp_path / ".ww" / "features" / "agents.yaml").write_text(
        AGENTS.replace("Agent adapters.", "The agent adapters."), "utf-8"
    )

    assert "Feature maps" not in MarkdownOutputAdapter().render_instruction(develop)
    assert (
        service.complete("TASK-1", artifact="Done.", summary_for_next="Done.").item_name
        != "develop"
    )


def test_a_step_that_changes_mapped_code_answers_for_its_own_changes(
    tmp_path: Path,
) -> None:
    service = _project(tmp_path, PLAIN)
    service.next("TASK-1")
    assert "Feature maps" not in _page(service)
    (tmp_path / "src" / "agents" / "claude.py").write_text("CLAUDE = 1\n", "utf-8")

    assert "### Feature maps to update" in _page(service)
    with pytest.raises(StateError, match="has no answer for these") as refused:
        service.complete("TASK-1", artifact="Done.", summary_for_next="Done.")
    assert "- agents\n" in str(refused.value)

    done = service.complete(
        "TASK-1",
        artifact="Done.\n\n## Feature maps\nagents: no change needed\n",
        summary_for_next="Done.",
    )

    assert done.item_name == "review"
    assert [entry.task_id for entry in read_task_list(tmp_path, "agents")] == ["TASK-1"]
    # The next step answers only for what it changes itself.
    assert "Feature maps" not in _page(service)


def test_a_writing_step_takes_the_answering_over(tmp_path: Path) -> None:
    service = _project(tmp_path, WITH_WRITER)
    service.next("TASK-1")
    (tmp_path / "src" / "agents" / "claude.py").write_text("CLAUDE = 1\n", "utf-8")

    assert "Feature maps" not in _page(service)
    assert (
        service.complete("TASK-1", artifact="Done.", summary_for_next="Done.").item_name
        == "update-maps"
    )


def test_a_readonly_workflow_answers_for_nothing(tmp_path: Path) -> None:
    service = _project(tmp_path, READONLY)

    develop = service.next("TASK-1")

    assert "Feature maps" not in MarkdownOutputAdapter().render_instruction(develop)
    assert (
        service.complete(
            "TASK-1", artifact="Looked.", summary_for_next="Done."
        ).item_name
        != "develop"
    )


def test_lint_says_who_brings_the_maps_up_to_date(tmp_path: Path, capsys) -> None:
    _project(tmp_path, PLAIN)
    (tmp_path / "ww.yaml").write_text(
        PLAIN
        + """  - name: checked
    steps:
      - name: develop
        description: Develop it.
      - name: test
        argv: ["true"]
        read_feature_maps: true
      - name: update-maps
        description: Bring the maps up to date.
        write_feature_maps: true
""",
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "lint"]) == 0
    out = capsys.readouterr().out

    assert (
        "Note: workflow 'task' has no write_feature_maps step; each agent step "
        "that changes files brings the feature maps up to date itself." in out
    )
    assert "Note: workflow 'checked'" not in out
    assert (
        "Warning: step 'test' of workflow 'checked' sets read_feature_maps, but "
        "no agent performs it, so it has no effect." in out
    )

    (tmp_path / "ww.json").write_text('{"feature_maps": false}', "utf-8")
    assert main(["--root", str(tmp_path), "lint"]) == 0
    assert "feature_maps" not in capsys.readouterr().out
