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
from ww.defaults import PROJECT_LAUNCHER

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


def test_an_edited_launcher_is_rewritten(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The launcher is ww-owned: an edit, like an older ww's, is replaced.
    _write(project / "ww", PROJECT_LAUNCHER + "# mine\n")

    assert main(["--root", str(project), "init", "--no-input"]) == 0
    capsys.readouterr()

    assert (project / "ww").read_text(encoding="utf-8") == PROJECT_LAUNCHER


def test_a_current_launcher_is_preserved(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(project / "ww", PROJECT_LAUNCHER)

    assert main(["--root", str(project), "init", "--no-input"]) == 0
    output = capsys.readouterr().out

    assert "ww (updated" not in output
    assert (project / "ww").read_text(encoding="utf-8") == PROJECT_LAUNCHER


def test_the_project_root_is_found_by_its_workflow_file(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(project / "ww.yaml", _WORKFLOW)
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
        user / "ww.yaml",
        "handlers:\n  - name: test\n    argv: [pytest]\n",
    )
    _write(user / "ww.json", "{}")
    _write(project / "ww.yaml", _WORKFLOW)
    _write(project / "ww.json", "{}")
    _write(
        project / "ww.local.yaml",
        "handlers:\n  - name: test\n    argv: [pytest, -q]\n",
    )
    _write(project / "ww.local.json", "{}")
    user_yaml = user / "ww.yaml"
    user_json = user / "ww.json"

    assert main(["--root", str(project), "lint"]) == 0

    assert capsys.readouterr().out == (
        "ww.yaml is valid.\n"
        f"Configuration files: {user_yaml}, ww.yaml, "
        f"ww.local.yaml, {user_json}, "
        "ww.json, ww.local.json\n"
        f"Notice: handler 'test' from {user_yaml} is overridden by "
        "ww.local.yaml.\n"
    )


def test_the_markdown_plan_ends_with_the_files_read(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(project / "ww.yaml", _WORKFLOW)
    _write(project / "ww.local.yaml", "modes: []\n")

    assert main(["--root", str(project), "plan", "-w", "task", "-a", "codex"]) == 0
    markdown = capsys.readouterr().out
    assert markdown.endswith("\nConfiguration files: ww.yaml, ww.local.yaml\n")

    assert (
        main(["--root", str(project), "plan", "-w", "task", "-a", "codex", "--json"])
        == 0
    )
    json.loads(capsys.readouterr().out)


# init and .gitignore

_LOCAL_PATTERNS = "*ww.local.yaml\n*ww.local.json\nww-setup.local.yaml\n"
_RUNTIME_LINES = ".ww/*\n!.ww/project.md\n"


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
    _write(project / ".gitignore", "*ww.local.json\n")

    _init(project, capsys, "--no-update-gitignore")

    assert (project / ".gitignore").read_text() == (
        "*ww.local.json\n*ww.local.yaml\nww-setup.local.yaml\n"
    )


def test_init_replaces_a_whole_directory_runtime_line_where_it_stands(
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
        ".ww/*\n!.ww/team.md\n!.ww/project.md\n" + _LOCAL_PATTERNS
    )


@pytest.mark.parametrize("whole", [".ww", ".ww/", "/.ww", "/.ww/", "  .ww/  "])
def test_init_replaces_every_line_that_ignores_the_runtime_directory_whole(
    project: Path, capsys: pytest.CaptureFixture[str], whole: str
) -> None:
    _write(
        project / ".gitignore",
        f"node_modules/\n{whole}\ndist/\n.ww/\n" + _LOCAL_PATTERNS,
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
        "!.ww/team.md\n.ww/*\n!.ww/project.md\nend/\n" + _LOCAL_PATTERNS
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
        ".ww/project.md"
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
    _write(user / "ww.yaml", _WORKFLOW)
    local = _write(project / "ww.local.yaml", "modes: []\n")

    _init(project, capsys)

    assert (user / "ww.yaml").read_text() == _WORKFLOW
    assert local.read_text() == "modes: []\n"
    assert not (user / "ww.json").exists()
    assert not (project / "ww.local.json").exists()
    # The user level already defines workflows, so init adds no empty list.
    assert "workflows:" not in (project / "ww.yaml").read_text()


def test_init_creates_the_user_configuration_directory(
    tmp_path: Path,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "config" / "ww"
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(directory))

    assert main(["--root", str(project), "init", "--no-input"]) == 0
    first = capsys.readouterr().out

    assert directory.is_dir()
    assert f"- {directory} (user configuration directory)" in first

    # Once it exists, a later init leaves it alone and says nothing about it.
    assert main(["--root", str(project), "init", "--no-input"]) == 0
    assert "user configuration directory" not in capsys.readouterr().out


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

    settings(user / "ww.json", "user")
    assert _run_launcher(project, user) == "ww-user"

    settings(project / "ww.json", "repo")
    assert _run_launcher(project, user) == "ww-repo"

    settings(project / "ww.local.json", "local")
    assert _run_launcher(project, user) == "ww-local"

    # An unreadable or keyless level is skipped, not fatal.
    _write(project / "ww.local.json", "{not json")
    assert _run_launcher(project, user) == "ww-repo"
    _write(project / "ww.local.json", '{"executable": "  "}')
    assert _run_launcher(project, user) == "ww-repo"


@pytest.mark.parametrize("level", ["user", "local"])
def test_init_commits_no_enabled_another_level_sets(
    user: Path, project: Path, capsys: pytest.CaptureFixture[str], level: str
) -> None:
    directory = user if level == "user" else project
    name = "ww.json" if level == "user" else "ww.local.json"
    _write(directory / name, '{"enabled": false}\n')

    _init(project, capsys)

    assert "enabled" not in json.loads((project / "ww.json").read_text())


def test_init_keeps_the_repo_files_own_enabled(
    user: Path, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(user / "ww.json", '{"enabled": false}\n')
    _write(project / "ww.json", '{"enabled": "on_request"}\n')

    _init(project, capsys)

    assert json.loads((project / "ww.json").read_text())["enabled"] == "on_request"
