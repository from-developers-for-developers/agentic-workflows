# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration migrations as a project meets them: load, warn, lint, migrate."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from ww.cli import main
from ww.config import load_configuration
from ww.config.composition import compose_configuration
from ww.errors import ConfigurationError
from ww.extensions import ExtensionRegistry
from ww.migrations.config import (
    ConfigMigration,
    catalog,
    migrate_files,
    rename_key,
    write_files,
)
from ww.migrations.config.catalog import ASSESS_QUESTION
from ww.migrations.config.project import project_files
from ww.project_config import load_project_config

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

# A test-only settings migration: ww.json has none of its own yet.
UPDATE_CHECKS = ConfigMigration(
    id="update-checks",
    deprecated_in="1.0.0b20",
    why="update_checks is now update_check",
    anchor="update-checks",
    files=("settings",),
    changes=(rename_key("update_checks", "update_check"),),
)


def _project(root: Path, workflows: str = LEGACY) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    return root


def _question(path: Path) -> str | None:
    return load_configuration(path).workflows[0].steps[0].assessment_question


def test_assess_question_is_read_warned_listed_and_migrated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _question(root / "ww.yaml") == "Are there issues?"
    assert main(["--root", str(root), "workflows"]) == 0
    assert (
        "ww warning: Deprecated: ww.yaml:workflows[0].steps[0].assess.question "
        "(line 6): an assessment's prompt key question is now assess (deprecated "
        "in ww 1.0.0b12); run ww migrate to fix. See "
        "ww docs upgrade#assessment-prompt-key-question."
    ) in capsys.readouterr().err

    assert main(["--root", str(root), "lint"]) == 0
    linted = capsys.readouterr().out
    assert (
        "Deprecated: ww.yaml:workflows[0].steps[0].assess.question (line 6)"
    ) in linted
    assert linted.count("Deprecated:") == 1
    assert "[auto-fixable by ww migrate]" in linted

    migrations = migrate_files(project_files(root / "ww.yaml", root / "ww.json"))
    assert write_files(migrations) == (root / "ww.yaml",)
    assert (root / "ww.yaml").read_text(encoding="utf-8") == LEGACY.replace(
        "question: Are", "assess: Are"
    )
    assert _question(root / "ww.yaml") == "Are there issues?"
    assert main(["--root", str(root), "lint"]) == 0
    assert "Deprecated:" not in capsys.readouterr().out


def test_an_imported_file_is_migrated_and_named_in_its_warning(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, "imports:\n  - flows.yaml\n")
    (root / "flows.yaml").write_text(LEGACY, encoding="utf-8")

    composed = compose_configuration(root / "ww.yaml")

    assert _question(root / "ww.yaml") == "Are there issues?"
    assert any(
        notice.startswith(
            "Deprecated: flows.yaml:workflows[0].steps[0].assess.question (line 6)"
        )
        for notice in composed.notices
    )
    files = project_files(root / "ww.yaml", root / "ww.json")
    assert [file.label for file in files] == ["flows.yaml", "ww.yaml"]


def test_both_prompt_keys_at_once_are_refused(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        LEGACY.replace(
            "question: Are there issues?",
            "question: Are there issues?\n          assess: Another?",
        ),
    )

    with pytest.raises(ConfigurationError, match="cannot combine assess and question"):
        load_configuration(root / "ww.yaml")


def test_a_removed_construct_is_an_error_naming_its_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    removed = dataclasses.replace(ASSESS_QUESTION, removed_in="2.0.0")
    monkeypatch.setattr(catalog, "CONFIG_MIGRATIONS", (removed,))
    monkeypatch.setattr(catalog, "__version__", "2.0.0")

    with pytest.raises(
        ConfigurationError,
        match=r"ww.yaml:workflows\[0\]\.steps\[0\]\.assess\.question \(line 6\): "
        r"an assessment's prompt key question is now assess; removed in ww 2\.0\.0: "
        r"run ww migrate to fix\. See "
        r"ww docs upgrade#assessment-prompt-key-question",
    ):
        load_configuration(root / "ww.yaml")

    [migration] = migrate_files(project_files(root / "ww.yaml", root / "ww.json"))
    assert (migration.findings[0].kind, migration.findings[0].fixed) == (
        "removed",
        True,
    )


def test_a_settings_file_is_migrated_in_memory_with_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(catalog, "CONFIG_MIGRATIONS", (UPDATE_CHECKS,))
    root = _project(tmp_path, LEGACY.replace("question:", "assess:"))
    (root / "ww.json").write_text('{\n  "update_checks": false\n}\n', encoding="utf-8")

    config = load_project_config(root / "ww.json")

    assert config.update_check is False
    assert config.notices == (
        "Deprecated: ww.json:update_checks (line 2): update_checks is now "
        "update_check (deprecated in ww 1.0.0b20); run ww migrate to fix. See "
        "ww docs upgrade#update-checks.",
    )
    assert main(["--root", str(root), "workflows"]) == 0
    assert "ww warning: Deprecated: ww.json:update_checks" in capsys.readouterr().err


def test_a_configuration_snapshot_is_migrated_in_memory_and_never_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog, "CONFIG_MIGRATIONS", (ASSESS_QUESTION, UPDATE_CHECKS))
    snapshot = tmp_path / ".ww" / "config" / ("a" * 40)
    _project(snapshot)
    settings = '{"update_checks": false}\n'
    (snapshot / "ww.json").write_text(settings, encoding="utf-8")

    assert _question(snapshot / "ww.yaml") == "Are there issues?"
    assert load_project_config(snapshot / "ww.json").update_check is False
    assert any(
        notice.startswith("Deprecated: ww.yaml:workflows[0]")
        for notice in compose_configuration(snapshot / "ww.yaml").notices
    )
    assert (snapshot / "ww.yaml").read_text(encoding="utf-8") == LEGACY
    assert (snapshot / "ww.json").read_text(encoding="utf-8") == settings


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
            removed_in="3.0.0",
            why="the setting project is now project_key",
            guide="https://acme.example/tickets/upgrading",
            anchor="project-key",
            files=("settings",),
            changes=(rename_key("project", "project_key"),),
        ),
    ),
)
"""


def _tickets(root: Path) -> None:
    directory = root / "ext" / "acme" / "tickets"
    directory.mkdir(parents=True)
    (directory / "extension.py").write_text(_TICKETS, encoding="utf-8")


def test_an_extensions_settings_migrate_file_by_file_when_it_loads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, LEGACY.replace("question:", "assess:"))
    _tickets(root)
    (root / "ww.json").write_text(
        json.dumps(
            {"extensions": {"acme/tickets": {"project": "OLD", "board": 7}}}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    # A later level already uses the new key; it still wins over the old one.
    (root / "ww.local.json").write_text(
        '{"extensions": {"acme/tickets": {"project_key": "LOCAL"}}}\n',
        encoding="utf-8",
    )
    registry = ExtensionRegistry.discover(root)

    assert registry.config.extensions["acme/tickets"]["project"] == "OLD"
    assert registry.notices == ()
    assert registry.settings("acme/tickets") == {"project_key": "LOCAL", "board": 7}
    assert registry.notices == (
        "Deprecated: ww.json:extensions.acme/tickets.project: the setting project "
        "is now project_key (deprecated in acme/tickets 2.1.0); run ww migrate to "
        "fix. See https://acme.example/tickets/upgrading#project-key.",
    )
    # A copy a saved plan froze is migrated whenever it is used.
    assert registry.frozen_settings("acme/tickets", {"project": "FROZEN"}) == {
        "project_key": "FROZEN"
    }

    assert main(["--root", str(root), "lint"]) == 0
    linted = capsys.readouterr().out
    assert (
        "Deprecated: ww.json:extensions.acme/tickets.project (line 4): the setting "
        "project is now project_key"
    ) in linted
    assert "[auto-fixable by ww migrate]" in linted


def test_an_unconfigured_extension_is_not_loaded_for_its_migrations(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, LEGACY.replace("question:", "assess:"))
    _tickets(root)
    registry = ExtensionRegistry.discover(root)

    assert registry.settings_migrations() == {}
    assert "acme/tickets" in registry.identifiers
    assert registry._providers["acme/tickets"].loaded is None
