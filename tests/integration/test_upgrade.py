# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from ww.cli import main
from ww.storage import Storage

upgrades = importlib.import_module("ww.upgrade")


@pytest.fixture
def package_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(upgrades, "installation_checkout", lambda: None)
    monkeypatch.setattr(upgrades, "_editable_install", lambda: False)
    monkeypatch.setattr(upgrades.sys, "prefix", str(tmp_path / "venv"))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return "done"

    monkeypatch.setattr(upgrades, "_run", run)
    return calls


@pytest.mark.parametrize(
    ("version", "pre"), [("1.0.0", False), ("1.0.0.dev42", True), ("1.0.0b2", True)]
)
def test_cli_upgrade_preserves_channel(
    tmp_path: Path,
    package_install,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    version: str,
    pre: bool,
) -> None:
    monkeypatch.setattr(upgrades, "__version__", version)
    assert main(["--root", str(tmp_path), "upgrade"]) == 0
    command, _ = package_install[0]
    assert command[:5] == [upgrades.sys.executable, "-m", "pip", "install", "--upgrade"]
    assert ("--pre" in command) is pre
    assert "finished" in capsys.readouterr().out
    assert not (tmp_path / ".ww").exists()


def test_explicit_pre_and_pipx_suffix_use_the_owning_environment(
    tmp_path: Path, package_install, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "pipx" / "venvs" / "ww-agentic-workflows-preview"
    prefix.mkdir(parents=True)
    (prefix / "pipx_metadata.json").write_text(
        json.dumps(
            {
                "main_package": {"package": "ww-agentic-workflows"},
                "environment": "ww-agentic-workflows-preview",
            }
        )
    )
    monkeypatch.setattr(upgrades.sys, "prefix", str(prefix))
    monkeypatch.setattr(upgrades.shutil, "which", lambda _: "/bin/pipx")
    upgrades.upgrade(Storage(tmp_path), pre=True)
    command, kwargs = package_install[0]
    assert command == ["/bin/pipx", "upgrade", prefix.name, "--pip-args=--pre"]
    assert kwargs["env"]["PIPX_HOME"] == str(tmp_path / "pipx")


def test_upgrade_refuses_an_open_task_before_running_installer(
    tmp_path: Path, package_install, capsys
) -> None:
    (tmp_path / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Do it.\n"
    )
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "start",
                "T",
                "-w",
                "task",
                "-a",
                "codex",
                "--requirements",
                "work",
                "--role",
                "manager",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "upgrade"]) == 1
    assert "Finish open tasks" in capsys.readouterr().err
    assert not package_install


def test_dirty_editable_checkout_is_not_changed(
    tmp_path: Path, package_install, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(upgrades, "installation_checkout", lambda: tmp_path)
    monkeypatch.setattr(upgrades, "_run", lambda *a, **k: " M local-file")
    assert main(["--root", str(tmp_path), "upgrade"]) == 1
    assert "local changes" in capsys.readouterr().err


def test_editable_upgrade_fast_forwards_and_preserves_dirty_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def git(directory, *args):
        return subprocess.run(
            ["git", *args], cwd=directory, capture_output=True, text=True, check=True
        ).stdout.strip()

    def commit(directory):
        git(directory, "add", ".")
        git(
            directory,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "change",
        )

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "dev")
    (origin / "file").write_text("before")
    commit(origin)
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", str(origin), str(checkout))
    (origin / "file").write_text("after")
    commit(origin)
    monkeypatch.setattr(upgrades, "installation_checkout", lambda: checkout)
    project = Storage(tmp_path / "project")
    assert "Updated" in upgrades.upgrade(project)
    assert (checkout / "file").read_text() == "after"
    (checkout / "file").write_text("local changes")
    with pytest.raises(upgrades.StateError, match="local changes"):
        upgrades.upgrade(project)
    assert (checkout / "file").read_text() == "local changes"
