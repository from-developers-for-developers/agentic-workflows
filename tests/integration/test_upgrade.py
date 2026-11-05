# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import dataclasses
import importlib
import json
import subprocess
from pathlib import Path

import pytest

from tests.integration.test_removed_constructs import OLD_WAYS
from ww.cli import main
from ww.migrations import removals
from ww.migrations.config import catalog
from ww.migrations.config.catalog import ASSESS_QUESTION
from ww.service import WorkflowService
from ww.storage import Storage

upgrades = importlib.import_module("ww.upgrade")
checks = importlib.import_module("ww.upgrade_checks")
# The new version's check, which the fixture replaces so no test spawns ww.
_REAL_AFTER = upgrades._after
# A workflow file still holding the deprecated ``assess.question``.
LEGACY = """\
workflows:
  - name: task
    steps:
      - code-review: Review the changes thoroughly.
        assess:
          question: Are there issues?
          positive:
            steps:
              - fix: Fix the exact findings.
      - publish: Publish the accepted result.
  - name: legacy
    steps:
      - old-ways: Do it the old way.
"""


@pytest.fixture
def package_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(upgrades, "installation_checkout", lambda: None)
    monkeypatch.setattr(upgrades, "_editable_install", lambda: False)
    monkeypatch.setattr(upgrades.sys, "prefix", str(tmp_path / "venv"))
    # Offline: the version to install is unknown unless a test names it.
    monkeypatch.setattr(upgrades, "fetch_releases", lambda: None)
    monkeypatch.setattr(upgrades, "_after", lambda storage: "")
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
    upgrades.upgrade(pre=True)
    command, kwargs = package_install[0]
    assert command == ["/bin/pipx", "upgrade", prefix.name, "--pip-args=--pre"]
    assert kwargs["env"]["PIPX_HOME"] == str(tmp_path / "pipx")


def test_upgrade_runs_while_a_task_is_open(
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
                "--description",
                "Test task.",
                "--role",
                "manager",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "upgrade"]) == 0
    assert "finished" in capsys.readouterr().out
    assert package_install


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
    assert "Updated" in upgrades.upgrade()
    assert (checkout / "file").read_text() == "after"
    (checkout / "file").write_text("local changes")
    with pytest.raises(upgrades.StateError, match="local changes"):
        upgrades.upgrade()
    assert (checkout / "file").read_text() == "local changes"


@pytest.fixture
def scheduled_removals(monkeypatch: pytest.MonkeyPatch) -> None:
    """``assess.question`` and the ``old-ways`` step, both removed in 2.0.0."""
    removed = dataclasses.replace(ASSESS_QUESTION, removed_in="2.0.0")
    monkeypatch.setattr(catalog, "CONFIG_MIGRATIONS", (removed,))
    monkeypatch.setattr(checks, "CONFIG_MIGRATIONS", (removed,))
    monkeypatch.setattr(removals, "REMOVED_CONSTRUCTS", (OLD_WAYS,))
    monkeypatch.setattr(removals, "__version__", "1.9.3")
    monkeypatch.setattr(upgrades, "__version__", "1.9.3")


def _legacy_project(root: Path) -> Path:
    """A project with deprecated configuration and an open run of ``legacy``."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "ww.yaml").write_text(LEGACY, encoding="utf-8")
    WorkflowService(Storage(root)).start(
        "legacy", "L-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )
    return root


def _target(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    monkeypatch.setattr(upgrades, "_package_target", lambda include_pre: version)


@pytest.mark.usefixtures("scheduled_removals")
def test_a_new_major_version_is_refused_while_removals_are_pending(
    tmp_path: Path, package_install, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _legacy_project(tmp_path / "project")
    _target(monkeypatch, "2.0.0")

    assert main(["--root", str(root), "upgrade"]) == 1

    error = capsys.readouterr().err
    assert not package_install
    assert (
        "ww error: ww 2.0.0 is a new major version, and this project holds what "
        "it no longer reads; nothing was upgraded."
    ) in error
    assert "Configuration ww 2.0.0 no longer reads:" in error
    assert "- ww.yaml:workflows[0].steps[0].assess.question (line 6): " in error
    assert "Fix it with `./ww migrate` (or the ww-migrate workflow)." in error
    assert "Tasks in progress whose saved plan ww 2.0.0 refuses:" in error
    assert "- L-1 (open run " in error and "the `old-ways` step" in error
    assert "`./ww upgrade --force`" in error


@pytest.mark.usefixtures("scheduled_removals")
def test_force_installs_the_new_major_version_and_says_what_it_overrode(
    tmp_path: Path, package_install, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _legacy_project(tmp_path / "project")
    _target(monkeypatch, "2.0.0")

    assert main(["--root", str(root), "upgrade", "--force"]) == 0

    output = capsys.readouterr().out
    assert package_install
    assert "ww upgrade finished." in output
    assert "Upgraded with --force although ww 2.0.0 refuses:" in output
    assert "- L-1 (open run " in output


@pytest.mark.usefixtures("scheduled_removals")
def test_a_minor_upgrade_is_not_blocked_by_removals_of_the_next_major(
    tmp_path: Path, package_install, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _legacy_project(tmp_path / "project")
    _target(monkeypatch, "1.10.0")

    assert main(["--root", str(root), "upgrade"]) == 0
    assert package_install
    assert "refuses" not in capsys.readouterr().out


@pytest.mark.usefixtures("scheduled_removals")
def test_a_major_upgrade_with_nothing_pending_goes_ahead(
    tmp_path: Path, package_install, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "ww.yaml").write_text(
        LEGACY.replace("question:", "assess:"), encoding="utf-8"
    )
    _target(monkeypatch, "2.0.0")

    assert main(["--root", str(root), "upgrade"]) == 0
    assert package_install


def test_an_unknown_target_is_said_rather_than_checked(
    tmp_path: Path, package_install, capsys
) -> None:
    (tmp_path / "ww.yaml").write_text(LEGACY, encoding="utf-8")

    assert main(["--root", str(tmp_path), "upgrade"]) == 0
    assert "ww could not tell which version this installs" in capsys.readouterr().out


def test_the_readiness_rule_uses_this_builds_removed_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        checks,
        "CONFIG_MIGRATIONS",
        (dataclasses.replace(ASSESS_QUESTION, removed_in="3.0.0"),),
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / "ww.yaml").write_text(LEGACY, encoding="utf-8")
    storage = Storage(root)

    assert not checks.readiness(storage, "2.0.0").blocked
    assert checks.readiness(storage, "3.0.0").blocked
    assert checks.crosses_major("1.9.3", "2.0.0b1")
    assert not checks.crosses_major("1.0.0b1", "1.0.0")
    assert not checks.crosses_major("1.9.3", "dev")


@pytest.mark.parametrize(
    ("returncode", "said"),
    [
        (0, "The upgraded ww found nothing to migrate in this project."),
        (1, "The upgraded ww found configuration to migrate in this project:"),
    ],
)
def test_the_new_version_checks_the_project_and_offers_migrate(
    tmp_path: Path,
    package_install,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    returncode: int,
    said: str,
) -> None:
    (tmp_path / "ww.yaml").write_text(LEGACY, encoding="utf-8")
    monkeypatch.setattr(upgrades, "_after", _REAL_AFTER)
    spawned = []
    run_for_real = subprocess.run

    def run(command, **kwargs):
        if "ww.cli" not in command:
            return run_for_real(command, **kwargs)
        spawned.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, returncode, "Deprecated: ww.yaml:... [auto-fixable]\n", ""
        )

    monkeypatch.setattr(subprocess, "run", run)

    assert main(["--root", str(tmp_path), "upgrade"]) == 0

    output = capsys.readouterr().out
    [(command, kwargs)] = spawned
    assert command[1:] == [
        "-m",
        "ww.cli",
        "--root",
        str(tmp_path),
        "migrate",
        "--check",
        "--deprecations",
    ]
    assert kwargs["env"]["WW_UPDATE_CHECK"] == "0"
    assert said in output
    if returncode:
        assert "Deprecated: ww.yaml:... [auto-fixable]" in output
        assert "Run `./ww migrate` to see the changes" in output
        assert "ww-migrate workflow" in output
        assert "Nothing was changed." in output


@pytest.mark.usefixtures("scheduled_removals")
def test_a_checkout_reads_the_upstream_series_before_fast_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def git(directory, *args):
        return subprocess.run(
            ["git", *args], cwd=directory, capture_output=True, text=True, check=True
        ).stdout.strip()

    def commit(directory, version):
        (directory / "pyproject.toml").write_text(
            f'[project]\nname = "ww-agentic-workflows"\nversion = "{version}"\n'
        )
        git(directory, "add", ".")
        git(
            directory,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            version,
        )

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "dev")
    commit(origin, "1.9.3")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", str(origin), str(checkout))
    commit(origin, "2.0.0b1")
    monkeypatch.setattr(upgrades, "installation_checkout", lambda: checkout)
    monkeypatch.setattr(upgrades, "_after", lambda storage: "")
    storage = Storage(_legacy_project(tmp_path / "project"))

    with pytest.raises(upgrades.StateError, match="ww 2.0.0b1 is a new major"):
        upgrades.upgrade(storage=storage)
    assert '"1.9.3"' in (checkout / "pyproject.toml").read_text()

    assert "Upgraded with --force" in upgrades.upgrade(storage=storage, force=True)
    assert '"2.0.0b1"' in (checkout / "pyproject.toml").read_text()
