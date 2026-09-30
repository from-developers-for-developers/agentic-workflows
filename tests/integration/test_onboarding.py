# SPDX-License-Identifier: GPL-3.0-or-later
"""Onboarding state: ``ww onboarding`` and what ``discover`` offers from it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.cli import main
from ww.config import parse_yaml_text
from ww.errors import ConfigurationError

WORKFLOWS = """workflows:
  - name: task
    steps:
      - work: Work.
"""


@pytest.fixture
def user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "user"
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(directory))
    return directory


@pytest.fixture
def root(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    return project


def _run(root: Path, capsys: pytest.CaptureFixture[str], *arguments: str) -> str:
    assert main(["--root", str(root), *arguments]) == 0
    return capsys.readouterr().out


def _state(root: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(_run(root, capsys, "onboarding", "--json"))  # type: ignore[no-any-return]


def test_nothing_is_set_at_first(
    user: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _state(root, capsys) == {
        "user": {
            "file": str(user / "state.json"),
            "explain": None,
            "learned.me": None,
        },
        "project": {
            "file": str(root / ".ww/metadata.json"),
            "setup.done": False,
            "learned.team": None,
            "learned.company": None,
            "learned.project": None,
        },
    }
    text = _run(root, capsys, "onboarding")
    assert "- explain: not asked yet" in text
    assert "- setup.done: no" in text
    # Reading writes nothing, not even the audit log.
    assert not (root / ".ww").exists()
    assert not user.exists()


def test_set_records_each_key_at_its_level(
    user: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(
        root,
        capsys,
        "onboarding",
        "--set",
        "explain=false",
        "--set",
        "learned.me=2026-09-30T10:00:00Z",
        "--set",
        "setup.done=true",
        "--set",
        "learned.project=now",
    )

    state = _state(root, capsys)
    assert state["user"] == {
        "file": str(user / "state.json"),
        "explain": False,
        "learned.me": "2026-09-30T10:00:00Z",
    }
    project = state["project"]
    assert isinstance(project, dict)
    assert project["setup.done"] is True
    assert str(project["learned.project"]).endswith("Z")
    assert json.loads((user / "state.json").read_text(encoding="utf-8")) == {
        "explain": False,
        "learned": {"me": "2026-09-30T10:00:00Z"},
    }
    metadata = json.loads((root / ".ww/metadata.json").read_text(encoding="utf-8"))
    assert metadata["ww"]["setup"] == {"done": "true"}
    # Setting is an operator's choice on record.
    log = (root / ".ww/executions.jsonl").read_text(encoding="utf-8")
    assert '"command": "onboarding"' in log


@pytest.mark.parametrize(
    ("assignment", "message"),
    [
        ("colour=blue", "unknown onboarding key 'colour'; the keys are explain"),
        ("explain", "--set takes KEY=VALUE"),
        ("explain=maybe", "explain takes true or false"),
        ("learned.team=yesterday", "learned.team takes `now` or an ISO timestamp"),
    ],
)
def test_a_wrong_assignment_writes_nothing(
    user: Path,
    root: Path,
    capsys: pytest.CaptureFixture[str],
    assignment: str,
    message: str,
) -> None:
    arguments = ["onboarding", "--set", "setup.done=true", "--set", assignment]

    assert main(["--root", str(root), *arguments]) == 1

    assert message in capsys.readouterr().err
    assert _state(root, capsys)["project"]["setup.done"] is False  # type: ignore[index]


def test_workflows_cannot_save_into_ww_own_project_metadata() -> None:
    body = (
        "workflows:\n  - task:\n    steps:\n      - work: Work.\n"
        "        saves:\n          - project_metadata.ww.setup.done: Done.\n"
    )

    with pytest.raises(ConfigurationError, match="project metadata under ww. is"):
        parse_yaml_text(body)
    # Task metadata and other project paths stay free.
    parse_yaml_text(body.replace("project_metadata.ww.", "metadata.ww."))
    parse_yaml_text(body.replace("project_metadata.ww.", "project_metadata.www."))


def test_discover_offers_setup_and_asks_about_explaining_once(
    user: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _run(root, capsys, "discover")
    assert "## Onboarding" in output
    assert "first use of ww in this project" in output
    assert "`ww-setup` skill" in output
    assert "onboarding --set explain=true" in output
    report = json.loads(_run(root, capsys, "discover", "--json"))
    assert report["onboarding"]["setup_done"] is False
    assert report["onboarding"]["explain"] is None
    assert len(report["onboarding"]["guidance"]) == 2

    _run(root, capsys, "onboarding", "--set", "explain=true")
    report = json.loads(_run(root, capsys, "discover", "--json"))
    assert report["onboarding"]["explain"] is True
    (guidance,) = report["onboarding"]["guidance"]
    assert "ww-setup" in guidance

    _run(root, capsys, "onboarding", "--set", "setup.done=true")
    report = json.loads(_run(root, capsys, "discover", "--json"))
    assert report["onboarding"]["guidance"] == []
    assert "## Onboarding" not in _run(root, capsys, "discover")


def test_on_request_discover_mentions_onboarding_only_as_information(
    user: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "ww-agentic-workflows.json").write_text(
        json.dumps({"enabled": "on_request"}), encoding="utf-8"
    )

    output = _run(root, capsys, "discover")

    assert "For information: ww has not been set up" in output
    assert "Offer the operator" not in output
    assert "Ask them once" not in output
