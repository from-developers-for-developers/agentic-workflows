# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww setup update``: change one defined workflow where it is written."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ww.cli import main
from ww.config import load_configuration
from ww.extensions import ExtensionRegistry

REPO = """# The project's own workflows.
imports:
  - shared.yaml

workflows:
  # How a change is made.
  - name: task
    description: Implement a change.
    steps:
      - work: Work.  # keep it short

  # Reviews stay as they are.
  - name: review
    description: Review a change.
    steps:
      - read: Read the change.
"""
SHARED = """# Shared by the team.
workflows:
  - name: deploy
    description: Ship it.
    steps:
      - ship: Ship.
  - name: audit
    description: Audit it.
    steps:
      - look: Look.
"""
LOCAL = (
    "workflows:\n  - name: task\n    description: Mine.\n    steps:\n      - w: W.\n"
)
NEW_TASK = """workflows:
  - name: task
    description: Implement a change, tests first.
    steps:
      - tests: Write the tests.
      - work: Make them pass.
"""


@pytest.fixture(autouse=True)
def user_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "user"
    directory.mkdir()
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(directory))
    return directory


@pytest.fixture
def root(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "ww.yaml").write_text(REPO, encoding="utf-8")
    (project / "shared.yaml").write_text(SHARED, encoding="utf-8")
    return project


def _fragment(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "update.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _update(root: Path, *arguments: str) -> int:
    return main(["--root", str(root), "setup", "update", *arguments])


def _description(root: Path, name: str) -> str:
    configuration = load_configuration(
        root / "ww.yaml", ExtensionRegistry.discover(root)
    )
    return next(
        workflow.description
        for workflow in configuration.workflows
        if workflow.name == name
    )


def test_a_root_yaml_workflow_is_replaced_and_the_rest_of_the_file_stays(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fragment = _fragment(tmp_path, NEW_TASK)

    assert _update(root, "task", str(fragment), "--yes") == 0

    captured = capsys.readouterr()
    assert "replaces workflow `task` in ww.yaml (project level)" in captured.err
    assert "+    description: Implement a change, tests first." in captured.err
    assert "comments inside the replaced entry are not kept" in captured.err
    text = (root / "ww.yaml").read_text(encoding="utf-8")
    expected = REPO.replace(
        "    description: Implement a change.\n    steps:\n"
        "      - work: Work.  # keep it short\n",
        "    description: Implement a change, tests first.\n    steps:\n"
        "      - tests: Write the tests.\n      - work: Make them pass.\n",
    )
    assert text == expected
    assert "# How a change is made." in text
    assert "# Reviews stay as they are." in text
    assert _description(root, "task") == "Implement a change, tests first."
    assert _description(root, "review") == "Review a change."
    assert (root / "shared.yaml").read_text(encoding="utf-8") == SHARED


def test_an_imported_definition_is_edited_in_its_own_file(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fragment = _fragment(
        tmp_path,
        "workflows:\n  - name: deploy\n    description: Ship it twice.\n"
        "    steps:\n      - ship: Ship.\n",
    )

    assert _update(root, "deploy", str(fragment), "--yes") == 0

    assert "in shared.yaml (project level)" in capsys.readouterr().err
    assert (root / "shared.yaml").read_text(encoding="utf-8") == SHARED.replace(
        "Ship it.", "Ship it twice."
    )
    assert (root / "ww.yaml").read_text(encoding="utf-8") == REPO
    assert _description(root, "audit") == "Audit it."


def test_the_root_definition_wins_over_an_import_and_is_the_one_edited(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "shared.yaml").write_text(
        SHARED
        + "  - name: task\n    description: Imported task.\n"
        + "    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )
    before = (root / "shared.yaml").read_text(encoding="utf-8")

    assert _update(root, "task", str(_fragment(tmp_path, NEW_TASK)), "--yes") == 0

    assert "in ww.yaml (project level)" in capsys.readouterr().err
    assert (root / "shared.yaml").read_text(encoding="utf-8") == before
    assert _description(root, "task") == "Implement a change, tests first."


def test_a_definition_hidden_by_a_higher_level_is_refused(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    local = LOCAL
    (root / "ww.local.yaml").write_text(local, encoding="utf-8")
    fragment = _fragment(tmp_path, NEW_TASK)

    assert _update(root, "task", str(fragment), "--level", "project", "--yes") == 1

    error = capsys.readouterr().err
    assert "hidden by ww.local.yaml (local level)" in error
    assert (root / "ww.yaml").read_text(encoding="utf-8") == REPO
    assert _description(root, "task") == "Mine."


def test_without_a_level_the_winning_local_definition_is_edited(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    local = LOCAL
    (root / "ww.local.yaml").write_text(local, encoding="utf-8")

    assert _update(root, "task", str(_fragment(tmp_path, NEW_TASK)), "--yes") == 0

    assert "in ww.local.yaml (local level)" in capsys.readouterr().err
    assert (root / "ww.yaml").read_text(encoding="utf-8") == REPO
    assert _description(root, "task") == "Implement a change, tests first."


def test_a_global_definition_overridden_by_the_project_is_refused(
    root: Path, tmp_path: Path, user_directory: Path
) -> None:
    (user_directory / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    description: Global.\n    steps:\n"
        "      - w: W.\n",
        encoding="utf-8",
    )

    code = _update(
        root, "task", str(_fragment(tmp_path, NEW_TASK)), "--level", "global", "--yes"
    )

    assert code == 1
    assert "Global." in (user_directory / "ww.yaml").read_text(encoding="utf-8")


def test_dry_run_shows_the_diff_and_writes_nothing(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _update(root, "task", str(_fragment(tmp_path, NEW_TASK)), "--dry-run")

    assert code == 0
    assert "Dry run: the configuration would be valid" in capsys.readouterr().out
    assert (root / "ww.yaml").read_text(encoding="utf-8") == REPO


def test_the_json_report_names_the_file_level_and_diff(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fragment = _fragment(tmp_path, NEW_TASK)

    assert _update(root, "task", str(fragment), "--json", "--yes") == 0

    report = json.loads(capsys.readouterr().out)
    assert report["workflow"] == "task"
    assert report["file"] == "ww.yaml"
    assert report["level"] == "project"
    assert report["applied"] is True
    assert "-    description: Implement a change." in report["diff"]


def test_an_update_that_would_not_load_is_refused_and_writes_nothing(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = _fragment(
        tmp_path,
        "workflows:\n  - name: task\n    description: Broken.\n"
        "    modes: [nonexistent]\n    steps:\n      - a: A.\n",
    )

    assert _update(root, "task", str(broken), "--yes") == 1

    assert "would not be valid" in capsys.readouterr().err
    assert (root / "ww.yaml").read_text(encoding="utf-8") == REPO


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("nothing", NEW_TASK.replace("task", "nothing"), "no configuration file"),
        ("task", NEW_TASK.replace("name: task", "name: other"), "does not rename"),
        ("task", "modes:\n  - m: M.\n", "only `workflows`"),
        (
            "task",
            NEW_TASK
            + "  - name: review\n    description: D.\n    steps:\n      - a: A.\n",
            "exactly one",
        ),
    ],
)
def test_malformed_requests_are_refused(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    text: str,
    message: str,
) -> None:
    assert _update(root, name, str(_fragment(tmp_path, text)), "--yes") == 1

    assert message in capsys.readouterr().err
    assert (root / "ww.yaml").read_text(encoding="utf-8") == REPO


def test_an_update_equal_to_the_current_definition_changes_nothing(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    same = _fragment(
        tmp_path,
        yaml.safe_dump(
            {"workflows": yaml.safe_load(REPO)["workflows"][:1]}, sort_keys=False
        ),
    )

    assert _update(root, "task", str(same), "--yes") == 1

    assert "already is that" in capsys.readouterr().err


def test_a_flow_style_entry_in_a_deeper_list_is_replaced_in_place(
    root: Path, tmp_path: Path
) -> None:
    text = (
        "workflows:\n- name: task\n  description: Old.\n  steps:\n  - work: Work.\n"
        "# tail comment\n- name: review\n  description: R.\n  steps:\n  - r: R.\n"
    )
    (root / "ww.yaml").write_text(text, encoding="utf-8")

    assert _update(root, "task", str(_fragment(tmp_path, NEW_TASK)), "--yes") == 0

    updated = (root / "ww.yaml").read_text(encoding="utf-8")
    assert "# tail comment\n- name: review\n" in updated
    assert _description(root, "task") == "Implement a change, tests first."
    assert _description(root, "review") == "R."


def test_dry_run_inspects_the_compiled_plan_the_change_would_leave(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fragment = _fragment(tmp_path, NEW_TASK)

    code = _update(
        root,
        "task",
        str(fragment),
        "--dry-run",
        "--inspect",
        "task",
        "--agent",
        "codex",
    )

    assert code == 0
    output = capsys.readouterr().out
    assert "Compiled plan of task after this change:" in output
    assert "Write the tests." in output
    assert (root / "ww.yaml").read_text(encoding="utf-8") == REPO


def test_inspect_needs_an_agent(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _update(
        root,
        "task",
        str(_fragment(tmp_path, NEW_TASK)),
        "--dry-run",
        "--inspect",
        "task",
    )

    assert code == 1
    assert "--inspect needs --agent" in capsys.readouterr().err


def test_setup_apply_dry_run_inspects_a_new_workflow(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fragment = _fragment(
        tmp_path,
        "workflows:\n  - name: hotfix\n    description: Fix fast.\n    steps:\n"
        "      - patch: Patch it.\n",
    )

    code = main(
        [
            "--root",
            str(root),
            "setup",
            "apply",
            str(fragment),
            "--for",
            "me",
            "--dry-run",
            "--inspect",
            "hotfix",
            "--agent",
            "codex",
        ]
    )

    assert code == 0
    assert "Patch it." in capsys.readouterr().out
    assert not (root / "ww-setup.local.yaml").exists()
