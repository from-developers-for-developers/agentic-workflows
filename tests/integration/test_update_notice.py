# SPDX-License-Identifier: GPL-3.0-or-later
"""Every command announces a newer ww before its own output, and only once."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ww.cli import main
from ww.cli import updates as cli_updates

CHANGELOG_BEFORE = "# Changelog\n\n## Unreleased\n"
CHANGELOG_AFTER = "# Changelog\n\n## Unreleased\n\n- `interact --pause` pauses.\n"


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _commit(repository: Path, message: str) -> None:
    _git("add", ".", cwd=repository)
    _git(
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-m",
        message,
        cwd=repository,
    )


def _project(root: Path) -> None:
    (root / "ww-agentic-workflows.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Do it.\n",
        encoding="utf-8",
    )


@pytest.fixture
def behind_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ww install whose checkout is one commit behind its origin."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "CHANGELOG.md").write_text(CHANGELOG_BEFORE, encoding="utf-8")
    _commit(origin, "First")

    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    (origin / "CHANGELOG.md").write_text(CHANGELOG_AFTER, encoding="utf-8")
    _commit(origin, "Pause interactions")

    monkeypatch.setenv("WW_UPDATE_CHECK", "1")
    monkeypatch.setenv("WW_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli_updates, "installation_checkout", lambda: clone)
    return clone


def test_a_command_announces_the_update_above_its_own_output(
    tmp_path: Path, behind_checkout: Path, capsys
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)

    assert main(["--root", str(project), "lint"]) == 0

    output = capsys.readouterr().out
    assert output.index("A newer ww is available") < output.index(
        "ww-agentic-workflows.yaml is valid."
    )
    assert "- `interact --pause` pauses." in output
    assert "Tell the person you are working for" in output


def test_the_same_update_is_not_announced_a_second_time(
    tmp_path: Path, behind_checkout: Path, capsys
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)

    main(["--root", str(project), "lint"])
    capsys.readouterr()
    main(["--root", str(project), "lint"])

    second = capsys.readouterr().out
    assert "A newer ww is available" not in second
    assert second.strip() == "ww-agentic-workflows.yaml is valid."


def test_json_output_stays_machine_readable(
    tmp_path: Path, behind_checkout: Path, capsys
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)

    assert main(["--root", str(project), "workflows"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)
    assert "A newer ww is available" in captured.err


def test_updates_reprints_the_notice_on_demand(
    tmp_path: Path, behind_checkout: Path, capsys
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)

    main(["--root", str(project), "lint"])
    capsys.readouterr()

    assert main(["--root", str(project), "updates"]) == 0
    assert "A newer ww is available" in capsys.readouterr().out


def test_updates_reports_an_up_to_date_checkout(
    tmp_path: Path, behind_checkout: Path, capsys
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    _git("pull", "-q", cwd=behind_checkout)

    assert main(["--root", str(project), "updates", "--check"]) == 0
    assert capsys.readouterr().out.strip() == "ww is up to date."


def test_the_project_can_turn_the_check_off(
    tmp_path: Path, behind_checkout: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    (project / "ww-agentic-workflows.json").write_text(
        json.dumps({"update_check": False}), encoding="utf-8"
    )
    monkeypatch.delenv("WW_UPDATE_CHECK", raising=False)

    assert main(["--root", str(project), "lint"]) == 0

    output = capsys.readouterr().out
    assert "A newer ww is available" not in output
    assert output.strip() == "ww-agentic-workflows.yaml is valid."


def test_a_broken_update_check_never_disturbs_the_command(
    tmp_path: Path, behind_checkout: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)

    def explode() -> Path:
        raise RuntimeError("the checkout cannot be resolved")

    monkeypatch.setattr(cli_updates, "installation_checkout", explode)

    assert main(["--root", str(project), "lint"]) == 0
    assert capsys.readouterr().out.strip() == "ww-agentic-workflows.yaml is valid."
