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
    (project / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
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
        },
        "project": {
            "file": str(root / ".ww/metadata.json"),
            "setup.done": False,
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
        "setup.done=true",
        "--set",
        "learned.project=now",
    )

    state = _state(root, capsys)
    assert state["user"] == {
        "file": str(user / "state.json"),
        "explain": False,
    }
    project = state["project"]
    assert isinstance(project, dict)
    assert project["setup.done"] is True
    assert str(project["learned.project"]).endswith("Z")
    assert json.loads((user / "state.json").read_text(encoding="utf-8")) == {
        "explain": False
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
        ("learned.project=yesterday", "learned.project takes `now` or an ISO"),
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


@pytest.mark.parametrize("key", ["learned.other", "other"])
def test_an_unknown_key_cannot_be_set(
    user: Path, root: Path, capsys: pytest.CaptureFixture[str], key: str
) -> None:
    arguments = [
        "onboarding",
        "--set",
        "setup.done=true",
        "--set",
        f"{key}=now",
    ]

    assert main(["--root", str(root), *arguments]) == 1

    error = capsys.readouterr().err
    assert f"unknown onboarding key {key!r}" in error
    assert _state(root, capsys)["project"]["setup.done"] is False  # type: ignore[index]


def test_unrelated_values_stay_on_disk_and_are_ignored(
    user: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user.mkdir()
    (user / "state.json").write_text(
        json.dumps(
            {"explain": True, "learned": {"extra": "2026-01-01T00:00:00Z"}, "other": 1}
        ),
        encoding="utf-8",
    )
    (root / ".ww").mkdir()
    (root / ".ww/metadata.json").write_text(
        json.dumps(
            {
                "ww": {
                    "learned": {
                        "extra": "2026-01-01T00:00:00Z",
                        "project": "2026-02-01T00:00:00Z",
                    }
                },
                "keep": {"me": "x"},
            }
        ),
        encoding="utf-8",
    )

    state = _state(root, capsys)
    assert state["user"] == {"file": str(user / "state.json"), "explain": True}
    assert state["project"] == {
        "file": str(root / ".ww/metadata.json"),
        "setup.done": False,
        "learned.project": "2026-02-01T00:00:00Z",
    }

    _run(
        root, capsys, "onboarding", "--set", "explain=false", "--set", "setup.done=true"
    )

    saved = json.loads((user / "state.json").read_text(encoding="utf-8"))
    assert saved == {
        "explain": False,
        "learned": {"extra": "2026-01-01T00:00:00Z"},
        "other": 1,
    }
    metadata = json.loads((root / ".ww/metadata.json").read_text(encoding="utf-8"))
    assert metadata["ww"]["learned"]["extra"] == "2026-01-01T00:00:00Z"
    assert metadata["keep"] == {"me": "x"}


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


def test_discover_mentions_setup_once_without_interrupting_or_interviewing(
    user: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _run(root, capsys, "discover")
    assert "## Onboarding" in output
    assert "has not been set up in this project" in output
    assert "never blocks ordinary work" in output
    assert "`ww-setup` skill" in output
    for word in ("explain", "interview"):
        assert word not in output.split("## Onboarding")[1].split("##")[0]
    report = json.loads(_run(root, capsys, "discover", "--json"))
    assert report["onboarding"]["setup_done"] is False
    assert len(report["onboarding"]["guidance"]) == 1

    _run(root, capsys, "onboarding", "--set", "setup.done=true")
    report = json.loads(_run(root, capsys, "discover", "--json"))
    assert report["onboarding"]["guidance"] == []
    assert "## Onboarding" not in _run(root, capsys, "discover")


def test_on_request_discover_mentions_onboarding_only_as_information(
    user: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "ww.json").write_text(
        json.dumps({"enabled": "on_request"}), encoding="utf-8"
    )

    output = _run(root, capsys, "discover")

    assert "For information: ww has not been set up" in output
    assert "Setup is optional" not in output
