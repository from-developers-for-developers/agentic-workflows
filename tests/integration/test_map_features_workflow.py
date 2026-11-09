# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww-map-features``, the built-in workflow that writes the feature maps;
the workflow proposals that give new workflows the feature-map settings; and
init's suggestion to map a project that has no maps yet."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import ww
from ww.builtin_workflows import builtin_workflow, builtin_workflow_text
from ww.cli import main
from ww.config import load_configuration
from ww.feature_map_steps import falls_back
from ww.plan import compile_workflow_plan
from ww.results import MAP_FEATURES_ACTION
from ww.service import WorkflowService
from ww.storage import Storage

AGENTS = """name: agents
summary: Agent adapters.
parts: [src/agents/]
"""


def _project(root: Path) -> WorkflowService:
    """A Git project whose launcher, ``./ww``, runs this source."""
    launcher = root / "ww"
    launcher.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={Path(ww.__file__).parents[1]} exec {sys.executable} -m ww.cli "
        '"$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    (root / "ww.json").write_text(json.dumps({"feature_maps": True}), "utf-8")
    (root / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - develop: Develop it.\n",
        encoding="utf-8",
    )
    (root / "src" / "agents").mkdir(parents=True)
    (root / "src" / "agents" / "codex.py").write_text("CODEX = 1\n", "utf-8")
    (root / ".gitignore").write_text(".ww/*\n!.ww/features/\nww\n", "utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@e.st"],
        ["config", "user.name", "Test"],
        ["add", "-A"],
        ["commit", "-qm", "seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return WorkflowService(Storage(root))


@pytest.mark.usefixtures("shipped_builtins")
def test_the_mapping_workflow_changes_only_the_maps_and_leaves_them_valid(
    tmp_path: Path,
) -> None:
    service = _project(tmp_path)
    service.start(
        "ww-map-features", "TASK-1", agent="codex", init_artifact="Map the agents."
    )
    assert service.next("TASK-1").item_name == "survey"
    service.complete(
        "TASK-1", artifact="- agents: the adapters.", summary_for_next="One map."
    )
    assert service.next("TASK-1").item_name == "map"
    features = tmp_path / ".ww" / "features"
    features.mkdir(parents=True)

    (features / "agents.yaml").write_text(AGENTS, encoding="utf-8")
    (tmp_path / "src" / "agents" / "codex.py").write_text("CODEX = 2\n", "utf-8")
    outside = service.complete("TASK-1", artifact="Mapped.", summary_for_next="Done.")
    assert outside.fix_required is not None
    assert "src/agents/codex.py" in json.dumps(outside.to_dict())

    (tmp_path / "src" / "agents" / "codex.py").write_text("CODEX = 1\n", "utf-8")
    (features / "agents.yaml").write_text(
        AGENTS.replace("src/agents/", "src/gone/"), "utf-8"
    )
    broken = service.complete("TASK-1", artifact="Mapped.", summary_for_next="Done.")
    assert broken.fix_required is not None

    (features / "agents.yaml").write_text(AGENTS, encoding="utf-8")
    done = service.complete("TASK-1", artifact="Mapped.", summary_for_next="Done.")

    assert done.fix_required is None
    assert done.item_name != "map"


@pytest.mark.usefixtures("shipped_builtins")
def test_the_mapping_workflow_is_built_in_and_never_falls_back(tmp_path: Path) -> None:
    _project(tmp_path)
    plan = compile_workflow_plan(
        load_configuration(tmp_path / "ww.yaml"), tmp_path, "ww-map-features", "codex"
    )

    assert builtin_workflow("ww-map-features").name == "ww-map-features"
    assert not falls_back(plan)


@pytest.mark.usefixtures("shipped_builtins")
def test_workflow_proposals_give_new_workflows_the_feature_map_settings() -> None:
    for name in ("ww-suggest", "ww-solve"):
        found = builtin_workflow_text(name)
        assert found is not None
        text = " ".join(found[1].split())
        assert "read_feature_maps: true" in text, name
        assert "write_feature_maps: true" in text, name


def test_init_suggests_mapping_a_project_without_maps(tmp_path: Path, capsys) -> None:
    (tmp_path / "ww.json").write_text('{"feature_maps": true}', encoding="utf-8")

    assert main(["--root", str(tmp_path), "init", "--no-input", "--json"]) == 0
    assert MAP_FEATURES_ACTION in json.loads(capsys.readouterr().out)["manual_actions"]

    features = tmp_path / ".ww" / "features"
    features.mkdir(parents=True)
    (features / "agents.yaml").write_text(AGENTS, encoding="utf-8")
    assert main(["--root", str(tmp_path), "init", "--no-input", "--json"]) == 0
    actions = json.loads(capsys.readouterr().out)["manual_actions"]
    assert MAP_FEATURES_ACTION not in actions
