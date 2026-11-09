# SPDX-License-Identifier: GPL-3.0-or-later
"""``write_feature_maps``: the page of what a run's changes leave due, the
refused completion until each map and file is answered for and every pointer
resolves, and the task lists it leaves."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ww.errors import StateError
from ww.features import read_task_list
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """workflows:
  - name: task
    steps:
      - name: research
        description: Research it.
        read_feature_maps: true
      - name: develop
        description: Develop it.
      - name: update-maps
        description: Bring the maps up to date.
        write_feature_maps: true
"""
AGENTS = """name: agents
summary: Agent adapters behind one registry.
parts:
  - src/registry.py#Registry
  - src/agents/
docs:
  - docs/guide.md#Agents
"""
RESEARCH = """Researched.

## Feature maps
Used: agents
Gaps: agents misses the CLI wiring
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _project(root: Path, *, feature_maps: bool = True) -> WorkflowService:
    (root / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (root / "ww.json").write_text(
        json.dumps({"feature_maps": feature_maps}), encoding="utf-8"
    )
    (root / "src" / "agents").mkdir(parents=True)
    (root / "src" / "registry.py").write_text("class Registry: ...\n", "utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("# Guide\n\n## Agents\n", "utf-8")
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
        "task",
        "TASK-1",
        agent="codex",
        init_artifact="Do it.",
        description="Add the Claude adapter",
    )
    return service


def _to_update_maps(service: WorkflowService, root: Path) -> str:
    """Research, develop a change, and open the writing step's page."""
    service.next("TASK-1")
    service.complete("TASK-1", artifact=RESEARCH, summary_for_next="Done.")
    service.next("TASK-1")
    (root / "src" / "agents" / "claude.py").write_text("CLAUDE = 1\n", "utf-8")
    (root / "src" / "billing.py").write_text("BILL = 1\n", "utf-8")
    service.complete("TASK-1", artifact="Developed.", summary_for_next="Done.")
    page = service.next("TASK-1")
    assert page.item_name == "update-maps"
    return MarkdownOutputAdapter().render_instruction(page)


def test_the_page_lists_what_the_run_s_changes_leave_due(tmp_path: Path) -> None:
    service = _project(tmp_path)

    page = _to_update_maps(service, tmp_path)

    assert "### Feature maps to update" in page
    assert "- `agents` (`" in page and "`docs/guide.md#Agents`" in page
    assert "Changed files no map covers:\n\n- `src/billing.py`" in page
    assert "- research: agents misses the CLI wiring" in page
    assert "## Feature maps\nagents: <updated" in page
    assert "src/billing.py: <updated" in page


def test_completion_waits_for_an_answer_for_each_map_and_file(tmp_path: Path) -> None:
    service = _project(tmp_path)
    _to_update_maps(service, tmp_path)

    with pytest.raises(StateError) as refused:
        service.complete(
            "TASK-1",
            artifact="Updated.\n\n## Feature maps\nagents: updated\n",
            summary_for_next="Done.",
        )

    assert "has no answer for these" in str(refused.value)
    assert "- src/billing.py" in str(refused.value)
    assert "- agents\n" not in str(refused.value)
    assert service.instruction("TASK-1").item_name == "update-maps"
    assert read_task_list(tmp_path, "agents") == ()

    done = service.complete(
        "TASK-1",
        artifact=(
            "Updated.\n\n## Feature maps\n- agents: updated\n"
            "- `src/billing.py`: not feature code\n"
        ),
        summary_for_next="Done.",
    )

    assert done.item_name != "update-maps"
    [entry] = read_task_list(tmp_path, "agents")
    assert (entry.task_id, entry.description) == ("TASK-1", "Add the Claude adapter")


def test_completion_waits_for_every_pointer_of_a_touched_map(tmp_path: Path) -> None:
    service = _project(tmp_path)
    _to_update_maps(service, tmp_path)
    (tmp_path / "src" / "registry.py").write_text("class Agents: ...\n", "utf-8")
    answered = "## Feature maps\nagents: updated\nsrc/billing.py: not feature code\n"

    with pytest.raises(StateError, match="no longer resolve") as refused:
        service.complete("TASK-1", artifact=answered, summary_for_next="Done.")
    assert "src/registry.py#Registry: does not contain 'Registry'" in str(refused.value)

    maps = tmp_path / ".ww" / "features" / "agents.yaml"
    maps.write_text(AGENTS.replace("#Registry", "#Agents"), "utf-8")
    assert (
        service.complete(
            "TASK-1", artifact=answered, summary_for_next="Done."
        ).item_name
        != "update-maps"
    )


def test_nothing_is_due_when_the_run_touches_no_map(tmp_path: Path) -> None:
    service = _project(tmp_path)
    service.next("TASK-1")
    service.complete("TASK-1", artifact=RESEARCH, summary_for_next="Done.")
    service.next("TASK-1")
    service.complete("TASK-1", artifact="Nothing to change.", summary_for_next="Done.")

    page = MarkdownOutputAdapter().render_instruction(service.next("TASK-1"))

    assert "there is nothing to update" in page
    assert (
        service.complete(
            "TASK-1", artifact="Nothing to update.", summary_for_next="Done."
        ).item_name
        != "update-maps"
    )


def test_with_feature_maps_off_the_step_is_an_ordinary_one(tmp_path: Path) -> None:
    service = _project(tmp_path, feature_maps=False)

    page = _to_update_maps(service, tmp_path)

    assert "Feature maps" not in page
    assert (
        service.complete("TASK-1", artifact="Done.", summary_for_next="Done.").item_name
        != "update-maps"
    )
