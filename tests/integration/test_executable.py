# SPDX-License-Identifier: GPL-3.0-or-later
"""A project names its ww binary: printed commands and ./ww follow it."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ww.cli import main
from ww.defaults import PROJECT_LAUNCHER
from ww.errors import ConfigurationError
from ww.project_config import load_project_config

WORKFLOWS = """workflows:
  - name: task
    steps:
      - work: Work.
"""


def _project(tmp_path: Path, config: dict[str, object]) -> Path:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (tmp_path / "ww-agentic-workflows.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    return tmp_path


def _run(root: Path, capsys: pytest.CaptureFixture[str], *args: str) -> str:
    assert main(["--root", str(root), *args]) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("ww-agentic-workflows-dev", True),
        ("/opt/ww/bin/ww", True),
        ("", False),
        (3, False),
    ],
)
def test_the_executable_is_a_name_or_a_path(
    tmp_path: Path, value: object, valid: bool
) -> None:
    path = tmp_path / "ww-agentic-workflows.json"
    path.write_text(json.dumps({"executable": value}), encoding="utf-8")

    if valid:
        assert load_project_config(path).executable == value
    else:
        with pytest.raises(ConfigurationError, match="executable must be"):
            load_project_config(path)


def test_printed_commands_use_the_configured_executable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, {"executable": "ww-agentic-workflows-dev"})

    page = _run(
        root,
        capsys,
        "start",
        "TASK-1",
        "--workflow",
        "task",
        "--agent",
        "codex",
        "--requirements",
        "Do it.",
        "--role",
        "manager",
    )
    discover = _run(root, capsys, "discover")

    assert "ww-agentic-workflows-dev next TASK-1 --role manager" in page
    assert "./ww " not in page
    assert "ww-agentic-workflows-dev start <TASK-ID>" in discover
    assert "ww-agentic-workflows-dev status <task-id>" in discover
    assert "./ww " not in discover


def test_without_an_executable_commands_keep_the_project_launcher(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, {})

    page = _run(
        root,
        capsys,
        "start",
        "TASK-1",
        "--workflow",
        "task",
        "--agent",
        "codex",
        "--requirements",
        "Do it.",
        "--role",
        "manager",
    )

    assert "./ww next TASK-1 --role manager" in page


def test_a_path_with_spaces_is_quoted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, {"executable": "/opt/my tools/ww"})

    assert "'/opt/my tools/ww' status <task-id>" in _run(root, capsys, "discover")


def _fake_binary(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    binary.write_text(f'#!/bin/sh\necho "{name} $*"\n', encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.mark.parametrize("configured", [True, False])
def test_the_launcher_runs_the_configured_binary(
    tmp_path: Path, configured: bool
) -> None:
    bin_dir = tmp_path / "bin"
    _fake_binary(bin_dir, "ww-agentic-workflows")
    dev = _fake_binary(bin_dir, "ww-agentic-workflows-dev")
    project = tmp_path / "project"
    project.mkdir()
    config = {"executable": str(dev)} if configured else {}
    (project / "ww-agentic-workflows.json").write_text(json.dumps(config))
    launcher = project / "ww"
    launcher.write_text(PROJECT_LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)

    result = subprocess.run(
        [str(launcher), "status", "TASK-1"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=True,
    )

    expected = "ww-agentic-workflows-dev" if configured else "ww-agentic-workflows"
    assert result.stdout.strip() == f"{expected} status TASK-1"


def test_init_records_the_standard_executable_and_writes_the_launcher(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, capsys, "init", "--no-input")

    config = json.loads((tmp_path / "ww-agentic-workflows.json").read_text())
    assert config["executable"] == "ww-agentic-workflows"
    assert (tmp_path / "ww").read_text(encoding="utf-8") == PROJECT_LAUNCHER


def test_init_keeps_an_edited_launcher_and_a_configured_executable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ww").write_text('#!/bin/sh\nexec my-ww "$@"\n', encoding="utf-8")
    (tmp_path / "ww-agentic-workflows.json").write_text(
        json.dumps({"executable": "ww-agentic-workflows-dev"}), encoding="utf-8"
    )

    output = _run(tmp_path, capsys, "init", "--no-input")

    config = json.loads((tmp_path / "ww-agentic-workflows.json").read_text())
    assert config["executable"] == "ww-agentic-workflows-dev"
    assert "my-ww" in (tmp_path / "ww").read_text(encoding="utf-8")
    # The summary names the binary the project runs.
    assert "     ww-agentic-workflows-dev\n" in output
