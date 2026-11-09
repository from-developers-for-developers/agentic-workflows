# SPDX-License-Identifier: GPL-3.0-or-later
"""Feature maps against a Git history: staleness, change sets, ``lookup``,
``changed``, ``stale``, ``show``'s task list, and the visualize tab."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ww.cli import main
from ww.features import (
    changed_paths,
    load_feature_maps,
    record_task,
    staleness,
)
from ww.service import WorkflowService
from ww.storage import Storage
from ww.visualize.features import feature_maps_view
from ww.visualize.project import Project

FORMAT = "{{ww.task.id}}: {{commit_message}}"
AGENTS = """name: agents
summary: Agent adapters behind one registry.
aliases: [adapters]
parts:
  - src/registry.py#Registry
  - src/agents/
docs:
  - docs/guide.md#Agents
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


@pytest.fixture(name="root")
def _root(tmp_path: Path) -> Path:
    settings = {
        "feature_maps": True,
        "extensions": {"ww/git": {"commit_format": FORMAT}},
    }
    (tmp_path / "ww.json").write_text(json.dumps(settings), encoding="utf-8")
    (tmp_path / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - develop: Develop it.\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "agents").mkdir(parents=True)
    (tmp_path / "src" / "registry.py").write_text("class Registry: ...\n", "utf-8")
    (tmp_path / "src" / "agents" / "codex.py").write_text("CODEX = 1\n", "utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n\n## Agents\n", "utf-8")
    features = tmp_path / ".ww" / "features"
    features.mkdir(parents=True)
    (features / "agents.yaml").write_text(AGENTS, encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".ww/*\n!.ww/features/\n", "utf-8")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.st")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _commit(tmp_path, "seed")
    return tmp_path


def _stale(root: Path):  # type: ignore[no-untyped-def]
    feature = load_feature_maps(root).get("agents")
    assert feature is not None
    return staleness(root, feature, FORMAT)


def test_a_map_is_stale_by_the_commits_to_its_files_since_it_changed(
    root: Path,
) -> None:
    assert _stale(root).to_dict() == {
        "committed": True,
        "commits": 0,
        "tasks": [],
        "outside": 0,
    }

    (root / "src" / "agents" / "codex.py").write_text("CODEX = 2\n", "utf-8")
    _commit(root, "TASK-7: Retune Codex")
    (root / "src" / "registry.py").write_text("class Registry: pass\n", "utf-8")
    _commit(root, "Tidy by hand")
    (root / "docs" / "guide.md").write_text("# Guide\n\n## Agents\n\nMore.\n", "utf-8")
    _commit(root, "Docs\n\nWW-Operation: TASK-8:01-task:abc:task:develop:step:1")
    (root / "README.md").write_text("unrelated\n", "utf-8")
    _commit(root, "TASK-9: Unrelated")

    stale = _stale(root)
    assert (stale.commits, stale.tasks, stale.outside) == (3, ("TASK-7", "TASK-8"), 1)
    assert stale.describe() == (
        "3 commit(s) changed its files since the map last changed "
        "(TASK-7, TASK-8, 1 outside ww)"
    )

    (root / ".ww" / "features" / "agents.yaml").write_text(
        AGENTS.replace("Agent adapters", "The agent adapters"), "utf-8"
    )
    _commit(root, "TASK-10: Refresh the agents map")
    assert _stale(root).commits == 0


def test_a_map_not_committed_yet_has_no_staleness_to_measure(root: Path) -> None:
    (root / ".ww" / "features" / "hooks.yaml").write_text(
        "name: hooks\nsummary: Hooks.\nparts: [src/]\n", "utf-8"
    )
    feature = load_feature_maps(root).get("hooks")
    assert feature is not None

    stale = staleness(root, feature)

    assert stale is not None and not stale.committed
    assert stale.describe() == "not committed yet, so its staleness is unknown"


def test_changed_paths_are_measured_from_the_fork_or_from_head(root: Path) -> None:
    _git(root, "checkout", "-qb", "feature/x")
    (root / "src" / "agents" / "codex.py").write_text("CODEX = 3\n", "utf-8")
    _commit(root, "TASK-11: Retune")
    (root / "src" / "new.py").write_text("NEW = 1\n", "utf-8")
    (root / "docs" / "guide.md").unlink()

    assert changed_paths(root) == ("docs/guide.md", "src/new.py")
    assert changed_paths(root, "main") == (
        "docs/guide.md",
        "src/agents/codex.py",
        "src/new.py",
    )


def test_lookup_stale_and_show_report_on_the_maps(root: Path, capsys) -> None:
    (root / "src" / "registry.py").write_text("class Agents: ...\n", "utf-8")
    _commit(root, "TASK-12: Rename the registry")
    record_task(root, "agents", "TASK-12", "Rename the registry")

    assert main(["--root", str(root), "features", "lookup", "src/registry.py"]) == 0
    out = capsys.readouterr().out
    assert "- `agents` — Agent adapters behind one registry. (also: adapters)" in out
    assert "no longer resolves: parts[0] src/registry.py#Registry" in out
    assert "1 commit(s) changed its files since the map last changed (TASK-12)" in out

    assert main(["--root", str(root), "features", "lookup", "billing"]) == 0
    assert "No feature map matches 'billing'" in capsys.readouterr().out

    assert main(["--root", str(root), "features", "stale", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["maps"] == [
        {
            "name": "agents",
            "committed": True,
            "commits": 1,
            "tasks": ["TASK-12"],
            "outside": 0,
        }
    ]

    assert main(["--root", str(root), "features", "show", "agents"]) == 0
    shown = capsys.readouterr().out
    assert "Tasks that changed it:\n- TASK-12  Rename the registry" in shown


def test_changed_maps_the_checkout_or_a_task_s_run(root: Path, capsys) -> None:
    service = WorkflowService(Storage(root))
    service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    (root / "src" / "agents" / "claude.py").write_text("CLAUDE = 1\n", "utf-8")
    (root / "src" / "billing.py").write_text("BILL = 1\n", "utf-8")

    for args in (["features", "changed"], ["features", "changed", "TASK-1"]):
        assert main(["--root", str(root), *args, "--json"]) == 0
        answer = json.loads(capsys.readouterr().out)
        assert answer["affected"] == ["agents"]
        assert answer["unmapped"] == ["src/billing.py"]

    assert main(["--root", str(root), "features", "changed"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Feature maps the uncommitted changes touch:")
    assert "Changed files no map covers:\n- src/billing.py" in out


def test_the_visualize_tab_shows_each_map_or_is_absent(root: Path) -> None:
    (root / "src" / "registry.py").write_text("class Agents: ...\n", "utf-8")
    _commit(root, "Rename by hand")
    record_task(root, "agents", "TASK-3", "Add the registry")

    view = feature_maps_view(Project.load(Storage(root)))

    assert view["enabled"] is True
    [agents] = view["maps"]
    assert agents["aliases"] == ["adapters"]
    assert agents["fields"]["parts"] == [
        {
            "pointer": "src/registry.py#Registry",
            "broken": ["does not contain 'Registry'"],
        },
        {"pointer": "src/agents/", "broken": []},
    ]
    assert agents["staleness"]["commits"] == 1
    assert agents["staleness"]["outside"] == 1
    assert agents["tasks"] == [{"id": "TASK-3", "description": "Add the registry"}]
    assert [problem["map"] for problem in view["problems"]] == ["agents"]

    (root / "ww.json").write_text('{"feature_maps": false}', encoding="utf-8")
    assert feature_maps_view(Project.load(Storage(root))) == {"enabled": False}
