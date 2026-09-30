# SPDX-License-Identifier: GPL-3.0-or-later
"""The workflows ww ships as YAML, composed below every configuration level."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest

from ww import builtin_workflows
from ww.builtin_workflows import (
    CATCHALL,
    builtin_files,
    builtin_workflow,
    is_builtin,
)
from ww.cli import main
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.extensions import ExtensionRegistry
from ww.workflow_config import WorkflowConfiguration

LEARN = """workflows:
  - name: learn
    description: Learns who the operator is.
    modes: [gently]
    steps:
      - name: interview
        description: "Interview the operator into {{ww.documents.me}}."
        saves:
          - documents.me: What the operator said.
documents:
  - me: Who the operator is.
    scope: user
modes:
  - gently: Ask one question at a time.
"""
PROJECT = """workflows:
  - name: task
    description: Implement a change.
    steps:
      - work: Work.
"""


@pytest.fixture
def builtins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A built-in directory holding the shipped catch-all and a learning file."""
    directory = tmp_path / "builtins"
    directory.mkdir()
    shipped = files("ww.assets").joinpath("workflows", "catchall.yaml")
    (directory / "catchall.yaml").write_text(
        shipped.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (directory / "learn.yaml").write_text(LEARN, encoding="utf-8")
    monkeypatch.setattr(builtin_workflows, "BUILTIN_DIRECTORY", directory)
    return directory


def _project(
    tmp_path: Path,
    workflows: str = PROJECT,
    settings: dict[str, object] | None = None,
) -> Path:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "ww-agentic-workflows.yaml").write_text(workflows, encoding="utf-8")
    (root / "ww-agentic-workflows.json").write_text(
        json.dumps(settings or {}), encoding="utf-8"
    )
    return root


def _load(root: Path) -> WorkflowConfiguration:
    return load_configuration(
        root / "ww-agentic-workflows.yaml", ExtensionRegistry.discover(root)
    )


def test_the_shipped_catchall_is_one_manager_step_that_can_restart() -> None:
    catchall = builtin_workflow(CATCHALL)

    assert catchall.runtime == "auto"
    assert catchall.restartable is True
    (work,) = catchall.steps
    assert (work.name, work.role) == ("work", "manager")
    assert "Carry out the request exactly as you would" in work.description


@pytest.mark.usefixtures("shipped_builtins")
def test_every_shipped_file_parses_with_unique_names() -> None:
    names = [workflow.name for item in builtin_files() for workflow in item.workflows]

    assert CATCHALL in names
    assert "ww-learn" in names
    assert len(names) == len(set(names))


def test_built_in_workflows_follow_the_configured_ones_with_their_extras(
    builtins: Path, tmp_path: Path
) -> None:
    configuration = _load(_project(tmp_path))

    assert [workflow.name for workflow in configuration.workflows] == [
        "task",
        "catchall",
        "learn",
    ]
    assert configuration.documents_by_name["me"].scope == "user"
    assert "gently" in {mode.name for mode in configuration.modes}
    assert is_builtin(configuration.workflows_by_name["learn"])
    assert not is_builtin(configuration.workflows_by_name["task"])


def test_a_configured_definition_replaces_the_built_in_one(
    builtins: Path, tmp_path: Path
) -> None:
    own = PROJECT + (
        "  - name: learn\n"
        "    description: Our own learning.\n"
        "    steps:\n"
        "      - ask: Ask.\n"
        "documents:\n"
        "  - me: Kept in the project.\n"
        "    scope: project\n"
    )

    configuration = _load(_project(tmp_path, own))

    learn = configuration.workflows_by_name["learn"]
    assert learn.description == "Our own learning."
    assert not is_builtin(learn)
    assert configuration.documents_by_name["me"].scope == "project"
    assert [workflow.name for workflow in configuration.workflows].count("learn") == 1


def test_the_user_level_replaces_a_built_in_by_name(
    builtins: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(user))
    (user / "ww-agentic-workflows.yaml").write_text(
        "workflows:\n  - name: learn\n    description: Mine.\n"
        "    steps:\n      - ask: Ask.\n",
        encoding="utf-8",
    )

    configuration = _load(_project(tmp_path))

    assert configuration.workflows_by_name["learn"].description == "Mine."


def test_a_switched_off_built_in_brings_nothing_along(
    builtins: Path, tmp_path: Path
) -> None:
    root = _project(tmp_path, settings={"workflows": {"learn": {"enabled": False}}})

    configuration = _load(root)

    assert "learn" not in configuration.workflows_by_name
    assert "me" not in configuration.documents_by_name
    assert "gently" not in {mode.name for mode in configuration.modes}
    assert CATCHALL in configuration.workflows_by_name


def test_the_switch_accepts_only_built_in_names(
    builtins: Path, tmp_path: Path
) -> None:
    root = _project(tmp_path, settings={"workflows": {"task": {"enabled": False}}})

    with pytest.raises(
        ConfigurationError, match="unknown name.*built-in workflows: catchall, learn"
    ):
        _load(root)


def test_a_built_in_file_holds_only_workflows_documents_and_modes(
    builtins: Path, tmp_path: Path
) -> None:
    (builtins / "extra.yaml").write_text(
        "handlers:\n  - lint: Lint.\nworkflows:\n  - x:\n    steps:\n"
        "      - a: A.\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="built-in extra.yaml declares"):
        _load(_project(tmp_path))


def test_discover_lists_ww_own_workflows_apart(
    builtins: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert main(["--root", str(root), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [workflow["name"] for workflow in report["workflows"]] == ["task"]
    assert report["builtin_workflows"] == [
        {"name": "learn", "description": "Learns who the operator is."}
    ]
    assert report["catchall"]["name"] == CATCHALL

    assert main(["--root", str(root), "discover"]) == 0
    output = capsys.readouterr().out
    assert "## ww's own workflows\n\n- `learn` — Learns who the operator is." in output
    assert "## Changes no workflow covers" in output
