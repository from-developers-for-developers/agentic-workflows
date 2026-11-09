# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww migrate``: the configuration files and the stored tasks brought up to date."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from ww import __version__
from ww.cli import main
from ww.cli import migrate as migrate_command
from ww.config import load_configuration
from ww.execution_models import PlanSnapshot
from ww.migrations import data
from ww.migrations.config import (
    ConfigMigration,
    Option,
    catalog,
    delete_key,
    rename_key,
)
from ww.migrations.config.catalog import ASSESS_QUESTION
from ww.migrations.data import DataFormat

LEGACY = """\
workflows:
  - name: task
    steps:
      - code-review: Review the changes thoroughly.
        assess:
          question: Are there issues?  # the reviewer's question
          positive:
            steps:
              - fix: Fix the exact findings.
      - publish: Publish the accepted result.
"""
MIGRATED = LEGACY.replace("question: Are", "assess: Are")

# Test-only migrations: ww has no choice, manual, rule or settings migration
# of its own yet.
PROMPT_CHOICE = ConfigMigration(
    id="prompt-key",
    deprecated_in="1.0.0b12",
    why="an assessment's question key is retired",
    anchor="prompt-key",
    files=("workflows",),
    options=(
        Option(
            "rename",
            "the prompt stays, under assess",
            (rename_key("**.assess.question", "assess"),),
        ),
        Option(
            "drop",
            "the assessment loses its own prompt",
            (delete_key("**.assess.question"),),
        ),
    ),
)
QUESTION_MANUAL = ConfigMigration(
    id="question-meaning",
    deprecated_in="1.0.0b12",
    why="question changes meaning",
    anchor="question-meaning",
    files=("workflows",),
    detect=("**.assess.question",),
    options=(
        Option("assess", "keep the prompt by renaming question to assess"),
        Option("remove", "the assessment loses its prompt"),
    ),
)
RULE_GLOBS = ConfigMigration(
    id="rule-globs",
    deprecated_in="1.0.0b12",
    removed_in="2.0.0",
    why="a rule's globs are now its paths",
    anchor="rule-globs",
    files=("rules",),
    changes=(rename_key("globs", "paths"),),
)
UPDATE_CHECKS = ConfigMigration(
    id="update-checks",
    deprecated_in="1.0.0b12",
    removed_in="2.0.0",
    why="update_checks is now update_check",
    anchor="update-checks",
    files=("settings",),
    changes=(rename_key("update_checks", "update_check"),),
)

_TICKETS = """
from ww.extensions.api import Extension
from ww.migrations.config import ConfigMigration, rename_key

EXTENSION = Extension(
    vendor="acme",
    name="tickets",
    version="2.1.0",
    settings_migrations=(
        ConfigMigration(
            id="project-key",
            deprecated_in="2.1.0",
            why="the setting project is now project_key",
            guide="https://acme.example/tickets/upgrading",
            anchor="project-key",
            files=("settings",),
            changes=(rename_key("project", "project_key"),),
        ),
    ),
)
"""


def _project(root: Path, workflows: str = LEGACY) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    return root


def _ww(root: Path, *arguments: str) -> int:
    return main(["--root", str(root), "migrate", *arguments])


def _tickets(root: Path) -> None:
    directory = root / "ext" / "acme" / "tickets"
    directory.mkdir(parents=True)
    (directory / "extension.py").write_text(_TICKETS, encoding="utf-8")


def _remove_in_two(
    monkeypatch: pytest.MonkeyPatch, *migrations: ConfigMigration
) -> None:
    """Run as ww 2.0.0, which no longer reads ``migrations``' old shapes."""
    monkeypatch.setattr(catalog, "CONFIG_MIGRATIONS", migrations)
    monkeypatch.setattr(catalog, "__version__", "2.0.0")


def test_a_dry_run_lists_findings_and_diffs_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _ww(root) == 0

    out = capsys.readouterr().out
    assert (
        "  ww.yaml:6 [automatic, ww migrate --apply fixes it] assess-question\n"
        "    workflows[0].steps[0].assess.question: an assessment's prompt key "
        "question is now assess (deprecated in ww 1.0.0b12); run ww migrate to "
        "fix. See ww docs upgrade#assessment-prompt-key-question.\n"
    ) in out
    assert "--- a/ww.yaml\n+++ b/ww.yaml\n" in out
    assert (
        "-          question: Are there issues?  # the reviewer's question\n"
        "+          assess: Are there issues?  # the reviewer's question\n"
    ) in out
    assert (root / "ww.yaml").read_text(encoding="utf-8") == LEGACY


def test_apply_writes_the_changes_and_a_second_run_finds_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _ww(root, "--apply") == 0
    assert "Changed:\n  ww.yaml\n" in capsys.readouterr().out
    assert (root / "ww.yaml").read_text(encoding="utf-8") == MIGRATED

    assert _ww(root, "--apply") == 0
    assert capsys.readouterr().out == (
        "Nothing to migrate: no configuration file holds a deprecated or "
        "removed construct.\n"
    )
    assert (root / "ww.yaml").read_text(encoding="utf-8") == MIGRATED
    assert _ww(root, "--check", "--deprecations") == 0


def test_check_fails_on_removed_constructs_and_on_deprecations_when_asked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)

    assert _ww(root, "--check") == 0
    assert "Check passed: no removed construct is left" in capsys.readouterr().out
    assert _ww(root, "--check", "--deprecations") == 1
    assert "Check failed: 1 construct is left." in capsys.readouterr().out

    _remove_in_two(
        monkeypatch, dataclasses.replace(ASSESS_QUESTION, removed_in="2.0.0")
    )
    assert _ww(root, "--check") == 1
    out = capsys.readouterr().out
    assert "ww.yaml:6 [removed, ww migrate --apply fixes it] assess-question" in out
    assert "Check failed: 1 removed construct is left." in out
    assert (root / "ww.yaml").read_text(encoding="utf-8") == LEGACY


def test_deprecations_go_only_with_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _ww(root, "--deprecations") == 1
    assert "--deprecations goes with --check" in capsys.readouterr().err
    assert _ww(root, "--tasks", "--choose", "a=b") == 1
    assert "--tasks takes only --apply and --json" in capsys.readouterr().err


def test_a_choice_is_applied_only_once_the_operator_chooses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "CONFIG_MIGRATIONS", (PROMPT_CHOICE,))
    root = _project(tmp_path)

    assert _ww(root, "--apply") == 0
    out = capsys.readouterr().out
    assert (root / "ww.yaml").read_text(encoding="utf-8") == LEGACY
    assert "ww.yaml:6 [choice, choose an option] prompt-key" in out
    assert (
        "To choose (ww applies none of the options until you do):\n"
        "  prompt-key: an assessment's question key is retired (ww.yaml:6)\n"
        "    - rename: the prompt stays, under assess\n"
        "    - drop: the assessment loses its own prompt\n"
        "    Apply one with: ww migrate --apply --choose prompt-key=<option>\n"
        "    Guide: ww docs upgrade#prompt-key\n"
    ) in out

    assert _ww(root, "--choose", "prompt-key=nothing") == 1
    assert "prompt-key has no option 'nothing'" in capsys.readouterr().err
    assert _ww(root, "--choose", "assess-question=rename") == 1
    assert "'assess-question' is no migration with options" in capsys.readouterr().err

    assert _ww(root, "--apply", "--choose", "prompt-key=drop") == 0
    assert "[choice, fixed] prompt-key" in capsys.readouterr().out
    assert "question" not in (root / "ww.yaml").read_text(encoding="utf-8")


def test_at_a_terminal_apply_asks_which_option(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "CONFIG_MIGRATIONS", (PROMPT_CHOICE,))
    monkeypatch.setattr(migrate_command, "_terminal", lambda: True)
    answers = iter(["7", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    root = _project(tmp_path)

    assert _ww(root, "--apply") == 0

    out = capsys.readouterr().out
    assert "  1. rename: the prompt stays, under assess\n" in out
    assert "Answer a number from 1 to 2, or press Enter." in out
    assert (root / "ww.yaml").read_text(encoding="utf-8") == MIGRATED


def test_a_manual_finding_names_its_place_options_and_guide(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "CONFIG_MIGRATIONS", (QUESTION_MANUAL,))
    root = _project(tmp_path)

    assert _ww(root, "--apply") == 0

    out = capsys.readouterr().out
    assert (
        "Left for you to change by hand:\n"
        "  ww.yaml:6 workflows[0].steps[0].assess.question\n"
        "    question changes meaning (deprecated in ww 1.0.0b12); change "
        "workflows[0].steps[0].assess.question by hand: assess (keep the prompt "
        "by renaming question to assess); remove (the assessment loses its "
        "prompt). See ww docs upgrade#question-meaning.\n"
        "    Options:\n"
        "      - assess: keep the prompt by renaming question to assess\n"
        "      - remove: the assessment loses its prompt\n"
        "    Guide: ww docs upgrade#question-meaning\n"
    ) in out
    assert (root / "ww.yaml").read_text(encoding="utf-8") == LEGACY


def test_an_edit_ww_cannot_make_is_left_with_exact_instructions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    flow = LEGACY.replace(
        "        assess:\n"
        "          question: Are there issues?  # the reviewer's question\n"
        "          positive:\n"
        "            steps:\n"
        "              - fix: Fix the exact findings.\n",
        "        assess: {question: Any issues, positive: {handler: fix}}\n",
    )
    root = _project(tmp_path, flow)

    assert _ww(root, "--apply") == 0

    out = capsys.readouterr().out
    assert "ww.yaml:5 [manual, change it by hand] assess-question" in out
    assert "rename workflows[0].steps[0].assess.question to assess" in out
    assert "Guide: ww docs upgrade#assessment-prompt-key-question" in out
    assert (root / "ww.yaml").read_text(encoding="utf-8") == flow


def test_json_reports_findings_files_and_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _ww(root, "--check", "--deprecations", "--json") == 1

    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "check"
    assert report["unloaded"] == []
    assert report["findings"] == [
        {
            "file": "ww.yaml",
            "line": 6,
            "migration": "assess-question",
            "kind": "automatic",
            "location": "workflows[0].steps[0].assess.question",
            "message": (
                "an assessment's prompt key question is now assess (deprecated in "
                "ww 1.0.0b12); run ww migrate to fix. See ww docs "
                "upgrade#assessment-prompt-key-question."
            ),
            "fixed": True,
            "status": "fixable",
            "options": [],
            "guide": "ww docs upgrade#assessment-prompt-key-question",
        }
    ]
    [changed] = report["files"]
    assert (changed["file"], changed["kind"]) == ("ww.yaml", "workflows")
    assert changed["diff"].startswith("--- a/ww.yaml\n+++ b/ww.yaml\n")
    assert report["check"] == {"passed": False, "deprecations": True, "failing": 1}
    assert (report["choices"], report["written"]) == ([], [])

    assert _ww(root, "--apply", "--json") == 0
    assert json.loads(capsys.readouterr().out)["written"] == ["ww.yaml"]


def test_files_are_found_and_fixed_when_the_configuration_no_longer_loads(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed constructs in every kind of file: none loads, and all are fixed."""
    _remove_in_two(
        monkeypatch,
        dataclasses.replace(ASSESS_QUESTION, removed_in="2.0.0"),
        RULE_GLOBS,
        UPDATE_CHECKS,
    )
    root = _project(
        tmp_path,
        "imports:\n  - flows.yaml\nrules:\n  style:\n    - rules\n",
    )
    (root / "flows.yaml").write_text(LEGACY, encoding="utf-8")
    (root / "rules").mkdir()
    (root / "rules" / "python.md").write_text(
        '---\nglobs:\n  - "**/*.py"\n---\nWrite typed Python.\n', encoding="utf-8"
    )
    (root / "ww.json").write_text(
        json.dumps(
            {"update_checks": False, "projects": [{"name": "api", "path": "api"}]},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _tickets(root)
    (root / "api").mkdir()
    (root / "api" / "ww.json").write_text(
        '{"extensions": {"acme/tickets": {"project": "API"}}}\n', encoding="utf-8"
    )

    assert _ww(root) == 0
    out = capsys.readouterr().out
    assert "The configuration does not load, so its files were found" in out
    assert "flows.yaml:6 [removed, ww migrate --apply fixes it] assess-question" in out
    assert "rules/python.md:2 [removed, ww migrate --apply fixes it] rule-globs" in out
    assert "ww.json:2 [removed, ww migrate --apply fixes it] update-checks" in out
    assert "api/ww.json:1 [automatic, ww migrate --apply fixes it] " in out
    assert "acme/tickets:project-key" in out

    assert _ww(root, "--apply") == 0
    assert (root / "flows.yaml").read_text(encoding="utf-8") == MIGRATED
    assert (
        (root / "rules" / "python.md")
        .read_text(encoding="utf-8")
        .startswith("---\npaths:\n")
    )
    assert (
        json.loads((root / "ww.json").read_text(encoding="utf-8"))["update_check"]
        is False
    )
    assert json.loads((root / "api" / "ww.json").read_text(encoding="utf-8")) == {
        "extensions": {"acme/tickets": {"project_key": "API"}}
    }
    capsys.readouterr()
    assert load_configuration(root / "ww.yaml").workflows[0].name == "task"
    assert _ww(root, "--check", "--deprecations") == 0


def test_an_extensions_settings_are_migrated_by_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, MIGRATED)
    _tickets(root)
    (root / "ww.json").write_text(
        json.dumps({"extensions": {"acme/tickets": {"project": "OLD"}}}, indent=2)
        + "\n",
        encoding="utf-8",
    )

    assert _ww(root, "--apply", "--json") == 0

    report = json.loads(capsys.readouterr().out)
    [finding] = report["findings"]
    assert (finding["file"], finding["migration"], finding["status"]) == (
        "ww.json",
        "acme/tickets:project-key",
        "fixed",
    )
    assert finding["guide"] == "https://acme.example/tickets/upgrading#project-key"
    assert report["written"] == ["ww.json"]
    assert json.loads((root / "ww.json").read_text(encoding="utf-8")) == {
        "extensions": {"acme/tickets": {"project_key": "OLD"}}
    }


# ----- --tasks ---------------------------------------------------------------

FORMATS = Path(__file__).resolve().parents[1] / "fixtures" / "formats"


def _renamed(value: Any, task_id: str) -> Any:
    """The fixture's task ID, ``TASK-1``, replaced by ``task_id`` everywhere."""
    if isinstance(value, dict):
        return {key: _renamed(item, task_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_renamed(item, task_id) for item in value]
    return task_id if value == "TASK-1" else value


def _stored_task(
    root: Path, task_id: str, *, current_snapshot: bool = True
) -> dict[str, str]:
    """The version 1 sample task stored as ``task_id``; its files' texts.

    The run's nested plan snapshot is brought to the current schema, with the
    plan digest it now has, unless ``current_snapshot`` is false, which keeps
    it as the sample wrote it.
    """
    directory = root / ".ww" / "tasks" / task_id
    files = {
        directory / "state.json": FORMATS / "task-state" / "v1.json",
        directory / "runs" / "01-task" / "state.9.json": (
            FORMATS / "run-state" / "v1.json"
        ),
    }
    texts: dict[str, str] = {}
    for target, sample in files.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        document = _renamed(json.loads(sample.read_text(encoding="utf-8")), task_id)
        if current_snapshot and "run" in document:
            run = document["run"]
            run["snapshot"] = data.upgrade("plan snapshot", run["snapshot"])
            run["state"]["plan_digest"] = PlanSnapshot.from_dict(
                run["snapshot"]
            ).plan_digest
        text = json.dumps(document, indent=2) + "\n"
        target.write_text(text, encoding="utf-8")
        texts[str(target.relative_to(root))] = text
    return texts


def _version_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test-only version 2 of the task index and the run documents."""
    for name in ("task state", "run state"):
        monkeypatch.setitem(
            data.FORMATS,
            name,
            DataFormat(name, current=2, migrations={1: lambda document: document}),  # type: ignore[arg-type]
        )


def test_tasks_in_the_current_format_are_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, MIGRATED)
    texts = _stored_task(root, "TASK-1")

    assert _ww(root, "--tasks", "--apply") == 0

    assert capsys.readouterr().out == (
        "Every stored task that reads is in the current format.\n"
    )
    for path, text in texts.items():
        assert (root / path).read_text(encoding="utf-8") == text
    assert not (root / ".ww" / "backups").exists()


def test_a_run_whose_plan_snapshot_is_older_is_rewritten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, MIGRATED)
    _stored_task(root, "TASK-1", current_snapshot=False)

    assert _ww(root, "--tasks", "--apply", "--json") == 0

    report = json.loads(capsys.readouterr().out)
    assert [(task["task_id"], task["rewritten"]) for task in report["tasks"]] == [
        ("TASK-1", True)
    ]
    run_file = root / ".ww" / "tasks" / "TASK-1" / "runs" / "01-task" / "state.10.json"
    snapshot = json.loads(run_file.read_text(encoding="utf-8"))["run"]["snapshot"]
    assert snapshot["schema_version"] == data.FORMATS["plan snapshot"].current
    [research] = [
        item for item in snapshot["plan"]["items"] if item.get("artifact_dependencies")
    ]
    assert research["artifact_dependencies"] == ["research"]
    assert "artifact_dependency" not in research


def test_tasks_in_an_older_format_are_listed_then_rewritten_after_a_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, MIGRATED)
    texts = {**_stored_task(root, "TASK-1"), **_stored_task(root, "TASK-1/sub")}
    broken = root / ".ww" / "tasks" / "TASK-2" / "state.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{", encoding="utf-8")
    _version_two(monkeypatch)

    assert _ww(root, "--tasks") == 0
    out = capsys.readouterr().out
    assert out.startswith(
        "2 tasks hold state in an older format. ww migrate --tasks --apply "
        "rewrites them, after copying their state to .ww/backups/:\n"
        "  TASK-1: .ww/tasks/TASK-1/state.json, "
        ".ww/tasks/TASK-1/runs/01-task/state.9.json\n"
        "  TASK-1/sub: .ww/tasks/TASK-1/sub/state.json, "
        ".ww/tasks/TASK-1/sub/runs/01-task/state.9.json\n"
    )
    assert "Left as they are, since they do not read:\n  TASK-2: " in out
    for path, text in texts.items():
        assert (root / path).read_text(encoding="utf-8") == text

    assert _ww(root, "--tasks", "--apply", "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert [(task["task_id"], task["rewritten"]) for task in report["tasks"]] == [
        ("TASK-1", True),
        ("TASK-1/sub", True),
    ]
    assert [task["task_id"] for task in report["unreadable"]] == ["TASK-2"]
    backup = root / report["backup"]
    assert backup.parent == root / ".ww" / "backups"
    for path, text in texts.items():
        saved = backup / Path(path).relative_to(".ww/tasks")
        assert saved.read_text(encoding="utf-8") == text
    for task_id in ("TASK-1", "TASK-1/sub"):
        directory = root / ".ww" / "tasks" / task_id
        index = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        assert (index["schema_version"], index["written_by"]) == (2, __version__)
        assert index["runs"] == [{"id": "01-task", "revision": 10}]
        assert not (directory / "runs" / "01-task" / "state.9.json").exists()
        run = json.loads(
            (directory / "runs" / "01-task" / "state.10.json").read_text(
                encoding="utf-8"
            )
        )
        assert (run["schema_version"], run["written_by"]) == (2, __version__)
    assert broken.read_text(encoding="utf-8") == "{"

    assert _ww(root, "--tasks") == 0
    assert capsys.readouterr().out.startswith(
        "Every stored task that reads is in the current format.\n"
    )
