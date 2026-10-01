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
def user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "user"
    directory.mkdir()
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(directory))
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


def test_the_launcher_written_for_the_former_machine_level_is_upgraded() -> None:
    assert any("WW_MACHINE_CONFIG_DIR" in launcher for launcher in GENERATED_LAUNCHERS)
    assert "WW_USER_CONFIG_DIR" in PROJECT_LAUNCHER
    assert ".machine." not in PROJECT_LAUNCHER


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
    user: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        user / "ww-agentic-workflows.yaml",
        "handlers:\n  - name: test\n    argv: [pytest]\n",
    )
    _write(user / "ww-agentic-workflows.json", "{}")
    _write(project / "ww-agentic-workflows.yaml", _WORKFLOW)
    _write(project / "ww-agentic-workflows.json", "{}")
    _write(
        project / "ww-agentic-workflows.local.yaml",
        "handlers:\n  - name: test\n    argv: [pytest, -q]\n",
    )
    _write(project / "ww-agentic-workflows.local.json", "{}")
    user_yaml = user / "ww-agentic-workflows.yaml"
    user_json = user / "ww-agentic-workflows.json"

    assert main(["--root", str(project), "lint"]) == 0

    assert capsys.readouterr().out == (
        "ww-agentic-workflows.yaml is valid.\n"
        f"Configuration files: {user_yaml}, ww-agentic-workflows.yaml, "
        f"ww-agentic-workflows.local.yaml, {user_json}, "
        "ww-agentic-workflows.json, ww-agentic-workflows.local.json\n"
        f"Notice: handler 'test' from {user_yaml} is overridden by "
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

_LOCAL_PATTERNS = (
    "*ww-agentic-workflows.local.yaml\n*ww-agentic-workflows.local.json\n"
    "ww-setup.local.yaml\n"
)
_RUNTIME_LINES = ".ww/*\n!.ww/team.md\n!.ww/company.md\n!.ww/project.md\n"


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
        "ww-setup.local.yaml\n"
    )


def test_init_replaces_the_former_runtime_line_where_it_stands(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(project / ".gitignore", "node_modules/\n.ww/\ndist/\n" + _LOCAL_PATTERNS)

    _init(project, capsys, "--update-gitignore")
    _init(project, capsys, "--update-gitignore")

    assert (project / ".gitignore").read_text() == (
        "node_modules/\n" + _RUNTIME_LINES + "dist/\n" + _LOCAL_PATTERNS
    )


def test_init_writes_the_runtime_lines_and_completes_a_partial_set(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(project / ".gitignore", "node_modules/\n")
    _init(project, capsys, "--update-gitignore")
    assert (project / ".gitignore").read_text() == (
        "node_modules/\n" + _RUNTIME_LINES + _LOCAL_PATTERNS
    )

    _write(project / ".gitignore", ".ww/*\n!.ww/team.md\n")
    _init(project, capsys, "--update-gitignore")
    assert (project / ".gitignore").read_text() == (
        ".ww/*\n!.ww/team.md\n!.ww/company.md\n!.ww/project.md\n" + _LOCAL_PATTERNS
    )


@pytest.mark.parametrize("former", [".ww", ".ww/", "/.ww", "/.ww/", "  .ww/  "])
def test_init_replaces_every_line_that_ignores_the_runtime_directory_whole(
    project: Path, capsys: pytest.CaptureFixture[str], former: str
) -> None:
    _write(
        project / ".gitignore",
        f"node_modules/\n{former}\ndist/\n.ww/\n" + _LOCAL_PATTERNS,
    )

    _init(project, capsys, "--update-gitignore")
    _init(project, capsys, "--update-gitignore")

    assert (project / ".gitignore").read_text() == (
        "node_modules/\n" + _RUNTIME_LINES + "dist/\n" + _LOCAL_PATTERNS
    )


def test_a_re_inclusion_counts_only_after_the_runtime_line(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(project / ".gitignore", "!.ww/team.md\n.ww/*\n!.ww/project.md\nend/\n")

    _init(project, capsys, "--update-gitignore")

    assert (project / ".gitignore").read_text() == (
        "!.ww/team.md\n.ww/*\n!.ww/project.md\n!.ww/team.md\n!.ww/company.md\n"
        "end/\n" + _LOCAL_PATTERNS
    )


def test_init_keeps_a_gitignores_crlf_line_endings(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / ".gitignore").write_bytes(b"node_modules/\r\n.ww/\r\ndist/\r\n")

    _init(project, capsys, "--update-gitignore")

    assert (project / ".gitignore").read_bytes() == (
        "node_modules/\n" + _RUNTIME_LINES + "dist/\n" + _LOCAL_PATTERNS
    ).replace("\n", "\r\n").encode()


def test_the_runtime_lines_let_git_see_only_the_shared_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _init(tmp_path, capsys, "--update-gitignore")
    for name in ("team.md", "company.md", "project.md", "metadata.json"):
        _write(tmp_path / ".ww" / name, "x\n")

    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", ".ww"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert sorted(line.split()[-1] for line in status.splitlines()) == [
        ".ww/company.md",
        ".ww/project.md",
        ".ww/team.md",
    ]


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
    user: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(user / "ww-agentic-workflows.yaml", _WORKFLOW)
    local = _write(project / "ww-agentic-workflows.local.yaml", "modes: []\n")

    _init(project, capsys)

    assert (user / "ww-agentic-workflows.yaml").read_text() == _WORKFLOW
    assert local.read_text() == "modes: []\n"
    assert not (user / "ww-agentic-workflows.json").exists()
    assert not (project / "ww-agentic-workflows.local.json").exists()
    # The user level already defines workflows, so init adds no empty list.
    assert "workflows:" not in (project / "ww-agentic-workflows.yaml").read_text()


def test_init_creates_the_user_configuration_directory(
    tmp_path: Path,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "config" / "ww-agentic-workflows"
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(directory))

    assert main(["--root", str(project), "init", "--no-input"]) == 0
    first = capsys.readouterr().out

    assert directory.is_dir()
    assert f"- {directory} (user configuration directory)" in first

    # Once it exists, a later init leaves it alone and says nothing about it.
    assert main(["--root", str(project), "init", "--no-input"]) == 0
    assert "user configuration directory" not in capsys.readouterr().out


def test_a_former_user_level_file_stops_every_command(
    user: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(project, capsys)
    _write(user / "ww-agentic-workflows.machine.yaml", _WORKFLOW)

    assert main(["--root", str(project), "lint"]) != 0

    error = capsys.readouterr().err
    assert "ww-agentic-workflows.machine.yaml" in error
    assert str(user / "ww-agentic-workflows.yaml") in error


# The ./ww launcher


def _fake_binary(directory: Path, name: str) -> Path:
    path = _write(directory / name, f"#!/bin/sh\necho {name}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run_launcher(project: Path, user: Path) -> str:
    launcher = _write(project / "ww", PROJECT_LAUNCHER)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    environment = {**os.environ, "WW_USER_CONFIG_DIR": str(user)}
    result = subprocess.run(
        [str(launcher)], env=environment, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_the_launcher_runs_the_lowest_level_executable(
    tmp_path: Path, user: Path, project: Path
) -> None:
    bin_dir = tmp_path / "bin"
    for level in ("user", "repo", "local"):
        _fake_binary(bin_dir, f"ww-{level}")

    def settings(path: Path, level: str) -> None:
        _write(path, json.dumps({"executable": str(bin_dir / f"ww-{level}")}))

    settings(user / "ww-agentic-workflows.json", "user")
    assert _run_launcher(project, user) == "ww-user"

    settings(project / "ww-agentic-workflows.json", "repo")
    assert _run_launcher(project, user) == "ww-repo"

    settings(project / "ww-agentic-workflows.local.json", "local")
    assert _run_launcher(project, user) == "ww-local"

    # An unreadable or keyless level is skipped, not fatal.
    _write(project / "ww-agentic-workflows.local.json", "{not json")
    assert _run_launcher(project, user) == "ww-repo"
    _write(project / "ww-agentic-workflows.local.json", '{"executable": "  "}')
    assert _run_launcher(project, user) == "ww-repo"


@pytest.mark.parametrize(
    ("former", "current", "value"),
    [
        ("commit_message", "commit_format", "{{ww.task.id}}: {{commit_message}}"),
        ("use_separate_branch", "separate_branch", False),
    ],
)
def test_init_adds_no_git_setting_next_to_its_former_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    former: str,
    current: str,
    value: object,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    settings = json.dumps({"extensions": {"ww/git": {former: value}}}) + "\n"
    _write(tmp_path / "ww-agentic-workflows.json", settings)

    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 1

    assert (
        f"ww/git {former} in ww-agentic-workflows.json was renamed to {current}"
        in capsys.readouterr().err
    )
    assert (tmp_path / "ww-agentic-workflows.json").read_text() == settings


@pytest.mark.parametrize("level", ["user", "local"])
def test_init_commits_no_enabled_another_level_sets(
    user: Path, project: Path, capsys: pytest.CaptureFixture[str], level: str
) -> None:
    directory = user if level == "user" else project
    name = (
        "ww-agentic-workflows.json"
        if level == "user"
        else "ww-agentic-workflows.local.json"
    )
    _write(directory / name, '{"enabled": false}\n')

    _init(project, capsys)

    assert "enabled" not in json.loads(
        (project / "ww-agentic-workflows.json").read_text()
    )


def test_init_keeps_the_repo_files_own_enabled(
    user: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(user / "ww-agentic-workflows.json", '{"enabled": false}\n')
    _write(project / "ww-agentic-workflows.json", '{"enabled": "on_request"}\n')

    _init(project, capsys)

    assert json.loads((project / "ww-agentic-workflows.json").read_text())[
        "enabled"
    ] == "on_request"
