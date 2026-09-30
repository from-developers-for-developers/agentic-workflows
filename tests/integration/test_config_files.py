# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration file names, levels, and what init and the launcher do with them."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from ww.cli import main
from ww.cli.main import _resolve_project_root
from ww.defaults import GENERATED_LAUNCHERS, PROJECT_LAUNCHER

_WORKFLOW = """workflows:
  - task: Repo task.
    steps:
      - work: Do the work.
"""


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "machine"
    directory.mkdir()
    monkeypatch.setenv("WW_MACHINE_CONFIG_DIR", str(directory))
    return directory


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


# Former file names


@pytest.mark.parametrize(
    ("legacy", "current"),
    [
        ("workflows.yaml", "ww-agentic-workflows.yaml"),
        ("agentic-workflows.json", "ww-agentic-workflows.json"),
    ],
)
def test_a_command_stops_on_a_former_file_name(
    project: Path, capsys: pytest.CaptureFixture[str], legacy: str, current: str
) -> None:
    if legacy.endswith(".json"):
        _write(project / "ww-agentic-workflows.yaml", _WORKFLOW)
    _write(project / legacy, _WORKFLOW if legacy.endswith(".yaml") else "{}")

    assert main(["--root", str(project), "lint"]) == 1
    assert capsys.readouterr().err == (
        f"ww error: found {legacy}; rename it to {current}, "
        "or run init to rename it\n"
    )


def test_a_former_name_next_to_the_current_one_must_be_removed(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(project / "ww-agentic-workflows.yaml", _WORKFLOW)
    _write(project / "workflows.yaml", _WORKFLOW)

    assert main(["--root", str(project), "lint"]) == 1
    assert capsys.readouterr().err == (
        "ww error: found workflows.yaml next to ww-agentic-workflows.yaml; "
        "ww reads only ww-agentic-workflows.yaml, so remove workflows.yaml\n"
    )
    assert main(["--root", str(project), "init", "--no-input"]) == 1


def test_init_renames_former_files_and_keeps_their_settings(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(project / "workflows.yaml", _WORKFLOW)
    _write(
        project / "agentic-workflows.json",
        json.dumps({"max_rounds": 7, "executable": "ww-custom"}),
    )

    assert main(["--root", str(project), "init", "--no-input", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)["created"]

    assert created[:2] == [
        "ww-agentic-workflows.yaml (renamed from workflows.yaml)",
        "ww-agentic-workflows.json (renamed from agentic-workflows.json)",
    ]
    assert not (project / "workflows.yaml").exists()
    assert not (project / "agentic-workflows.json").exists()
    assert "task: Repo task." in (project / "ww-agentic-workflows.yaml").read_text()
    settings = json.loads((project / "ww-agentic-workflows.json").read_text())
    assert settings["max_rounds"] == 7
    assert settings["executable"] == "ww-custom"
    assert main(["--root", str(project), "lint"]) == 0


@pytest.mark.parametrize("launcher", GENERATED_LAUNCHERS)
def test_init_upgrades_a_launcher_an_earlier_ww_wrote(
    project: Path, capsys: pytest.CaptureFixture[str], launcher: str
) -> None:
    _write(project / "ww", launcher)

    assert main(["--root", str(project), "init", "--no-input"]) == 0
    capsys.readouterr()

    assert (project / "ww").read_text(encoding="utf-8") == PROJECT_LAUNCHER


def test_an_edited_launcher_is_preserved(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    edited = GENERATED_LAUNCHERS[0] + "# mine\n"
    _write(project / "ww", edited)

    assert main(["--root", str(project), "init", "--no-input"]) == 0
    capsys.readouterr()

    assert (project / "ww").read_text(encoding="utf-8") == edited


@pytest.mark.parametrize("marker", ["workflows.yaml", "ww-agentic-workflows.yaml"])
def test_the_project_root_is_found_by_either_workflow_file_name(
    project: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    _write(project / marker, _WORKFLOW)
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    # Outside a Git checkout, the walk up from the current directory decides.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(project.parent))

    assert _resolve_project_root(None) == project.resolve()


# Levels as the CLI reports them


def test_lint_lists_the_files_read_and_the_overrides(
    machine: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        machine / "ww-agentic-workflows.machine.yaml",
        "handlers:\n  - name: test\n    argv: [pytest]\n",
    )
    _write(machine / "ww-agentic-workflows.machine.json", "{}")
    _write(project / "ww-agentic-workflows.yaml", _WORKFLOW)
    _write(project / "ww-agentic-workflows.json", "{}")
    _write(
        project / "ww-agentic-workflows.local.yaml",
        "handlers:\n  - name: test\n    argv: [pytest, -q]\n",
    )
    _write(project / "ww-agentic-workflows.local.json", "{}")
    machine_yaml = machine / "ww-agentic-workflows.machine.yaml"
    machine_json = machine / "ww-agentic-workflows.machine.json"

    assert main(["--root", str(project), "lint"]) == 0

    assert capsys.readouterr().out == (
        "ww-agentic-workflows.yaml is valid.\n"
        f"Configuration files: {machine_yaml}, ww-agentic-workflows.yaml, "
        f"ww-agentic-workflows.local.yaml, {machine_json}, "
        "ww-agentic-workflows.json, ww-agentic-workflows.local.json\n"
        f"Notice: handler 'test' from {machine_yaml} is overridden by "
        "ww-agentic-workflows.local.yaml.\n"
    )


def test_the_markdown_plan_ends_with_the_files_read(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(project / "ww-agentic-workflows.yaml", _WORKFLOW)
    _write(project / "ww-agentic-workflows.local.yaml", "modes: []\n")

    assert main(["--root", str(project), "plan", "-w", "task", "-a", "codex"]) == 0
    markdown = capsys.readouterr().out
    assert markdown.endswith(
        "\nConfiguration files: ww-agentic-workflows.yaml, "
        "ww-agentic-workflows.local.yaml\n"
    )

    assert (
        main(["--root", str(project), "plan", "-w", "task", "-a", "codex", "--json"])
        == 0
    )
    json.loads(capsys.readouterr().out)


# init and .gitignore

_LOCAL_PATTERNS = "*ww-agentic-workflows.local.yaml\n*ww-agentic-workflows.local.json\n"


def _init(project: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> None:
    assert main(["--root", str(project), "init", "--no-input", *extra]) == 0
    capsys.readouterr()


def test_init_appends_local_patterns_to_an_existing_gitignore_once(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(project / ".gitignore", "node_modules/")

    _init(project, capsys, "--no-update-gitignore")
    _init(project, capsys, "--no-update-gitignore")

    assert (project / ".gitignore").read_text() == "node_modules/\n" + _LOCAL_PATTERNS


def test_init_keeps_patterns_already_listed(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(project / ".gitignore", "*ww-agentic-workflows.local.json\n")

    _init(project, capsys, "--no-update-gitignore")

    assert (project / ".gitignore").read_text() == (
        "*ww-agentic-workflows.local.json\n*ww-agentic-workflows.local.yaml\n"
    )


def test_init_creates_a_gitignore_only_in_a_git_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = tmp_path / "plain"
    checkout = tmp_path / "checkout"
    plain.mkdir()
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)

    _init(plain, capsys, "--no-update-gitignore")
    _init(checkout, capsys, "--no-update-gitignore")

    assert not (plain / ".gitignore").exists()
    assert (checkout / ".gitignore").read_text() == _LOCAL_PATTERNS


def test_init_writes_only_repo_level_files(
    machine: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(machine / "ww-agentic-workflows.machine.yaml", _WORKFLOW)
    local = _write(project / "ww-agentic-workflows.local.yaml", "modes: []\n")

    _init(project, capsys)

    assert (machine / "ww-agentic-workflows.machine.yaml").read_text() == _WORKFLOW
    assert local.read_text() == "modes: []\n"
    assert not (machine / "ww-agentic-workflows.machine.json").exists()
    assert not (project / "ww-agentic-workflows.local.json").exists()
    # The machine level already defines workflows, so init adds no empty list.
    assert "workflows:" not in (project / "ww-agentic-workflows.yaml").read_text()


# The ./ww launcher


def _fake_binary(directory: Path, name: str) -> Path:
    path = _write(directory / name, f"#!/bin/sh\necho {name}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run_launcher(project: Path, machine: Path) -> str:
    launcher = _write(project / "ww", PROJECT_LAUNCHER)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    environment = {**os.environ, "WW_MACHINE_CONFIG_DIR": str(machine)}
    result = subprocess.run(
        [str(launcher)], env=environment, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_the_launcher_runs_the_lowest_level_executable(
    tmp_path: Path, machine: Path, project: Path
) -> None:
    bin_dir = tmp_path / "bin"
    for level in ("machine", "repo", "local"):
        _fake_binary(bin_dir, f"ww-{level}")

    def settings(path: Path, level: str) -> None:
        _write(path, json.dumps({"executable": str(bin_dir / f"ww-{level}")}))

    settings(machine / "ww-agentic-workflows.machine.json", "machine")
    assert _run_launcher(project, machine) == "ww-machine"

    settings(project / "ww-agentic-workflows.json", "repo")
    assert _run_launcher(project, machine) == "ww-repo"

    settings(project / "ww-agentic-workflows.local.json", "local")
    assert _run_launcher(project, machine) == "ww-local"

    # An unreadable or keyless level is skipped, not fatal.
    _write(project / "ww-agentic-workflows.local.json", "{not json")
    assert _run_launcher(project, machine) == "ww-repo"
    _write(project / "ww-agentic-workflows.local.json", '{"executable": "  "}')
    assert _run_launcher(project, machine) == "ww-repo"
