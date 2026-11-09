# SPDX-License-Identifier: GPL-3.0-or-later
"""``read_feature_maps``: the step setting, its page section, and the
Feature maps report its artifact ends with."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.features import MapsReport
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage
from ww.visualize.atlas import build_atlas
from ww.visualize.project import Project

WORKFLOWS = """workflows:
  - name: task
    steps:
      - name: research
        description: Research the change.
        read_feature_maps: true
      - name: develop
        description: Develop it.
        artifact_from: research
"""

REPORT = """# Research

The registry lives in src/registry.py.

## Feature maps
Used: agents
Gaps: agents map misses src/cli/audit.py
"""


def _service(root: Path, *, feature_maps: bool) -> WorkflowService:
    (root / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (root / "ww.json").write_text(
        json.dumps({"feature_maps": feature_maps}), encoding="utf-8"
    )
    return WorkflowService(Storage(root))


@pytest.mark.parametrize("feature_maps", [True, False])
def test_a_reading_step_s_page_points_to_the_maps_only_with_them_on(
    tmp_path: Path, feature_maps: bool
) -> None:
    service = _service(tmp_path, feature_maps=feature_maps)
    start_after_init(service, "task", "TASK-1", agent="codex")

    research = service.next("TASK-1")
    page = MarkdownOutputAdapter().render_instruction(research)

    assert research.item_name == "research"
    assert research.read_feature_maps is feature_maps
    assert ("### Feature maps" in page) is feature_maps
    assert ("features lookup <words or paths>" in page) is feature_maps
    assert ("## Feature maps\nUsed: <the maps" in page) is feature_maps


def test_a_step_without_the_setting_gets_no_maps_text(tmp_path: Path) -> None:
    service = _service(tmp_path, feature_maps=True)
    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    service.complete("TASK-1", artifact=REPORT, summary_for_next="Done.")

    develop = service.next("TASK-1")

    assert develop.item_name == "develop"
    assert not develop.read_feature_maps
    assert "### Feature maps" not in MarkdownOutputAdapter().render_instruction(develop)


def test_the_run_keeps_what_its_reading_steps_reported(tmp_path: Path) -> None:
    service = _service(tmp_path, feature_maps=True)
    start_after_init(service, "task", "TASK-1", agent="codex")
    assert service.feature_map_reports("TASK-1") == ()
    service.next("TASK-1")
    service.complete("TASK-1", artifact=REPORT, summary_for_next="Done.")

    assert service.feature_map_reports("TASK-1") == (
        ("research", MapsReport(("agents",), ("agents map misses src/cli/audit.py",))),
    )


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("read_feature_maps: yes please", "read_feature_maps must be true or false"),
        (
            "read_feature_maps: true\n        artifact: false",
            "read_feature_maps requires artifact: true",
        ),
    ],
)
def test_a_malformed_setting_is_refused(
    tmp_path: Path, setting: str, message: str
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        "workflows:\n  - name: task\n    steps:\n      - name: research\n"
        f"        description: Research.\n        {setting}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(path)


def test_the_visualizer_shows_which_steps_read_the_maps(tmp_path: Path) -> None:
    _service(tmp_path, feature_maps=True)

    atlas = build_atlas(Project.load(Storage(tmp_path)))

    [task] = [entry for entry in atlas["workflows"] if entry["name"] == "task"]
    steps = {step["name"]: step for step in task["steps"]}
    assert steps["research"]["read_feature_maps"] is True
    assert steps["develop"]["read_feature_maps"] is False
