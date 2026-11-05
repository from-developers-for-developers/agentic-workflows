# SPDX-License-Identifier: GPL-3.0-or-later
"""The upgrade guide is generated from the migrations, and documented as generated."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.cli import main
from ww.design_docs import read_design_document
from ww.errors import StateError
from ww.migrations.config import (
    ConfigMigration,
    Option,
    catalog,
    delete_key,
    move_key,
    rename_key,
    render_upgrade_guide,
)

DOCUMENTATION = Path(__file__).resolve().parents[2] / "documentation"

CHOICE = ConfigMigration(
    id="retries",
    deprecated_in="1.0.0b20",
    removed_in="2.0.0",
    why="retries moved under limits",
    anchor="retries",
    files=("settings",),
    options=(
        Option(
            "move",
            "the same number of retries applies",
            (move_key("retries", "limits.retries"),),
        ),
        Option(
            "drop", "the default number of retries applies", (delete_key("retries"),)
        ),
    ),
    title="Settings key `retries`",
    example=('{"retries": 2}', '{"limits": {"retries": 2}}'),
)
MANUAL = ConfigMigration(
    id="shell-hooks",
    deprecated_in="1.0.0b20",
    why="shell hooks no longer run",
    anchor="shell-hooks",
    files=("workflows", "rules"),
    detect=("**.shell_hook",),
    options=(Option("command", "the hook runs as a command step"),),
)


def test_the_documented_guide_is_the_generated_one() -> None:
    """Run ``scripts/generate_upgrade_guide.py`` when this fails."""
    assert (DOCUMENTATION / "deprecations.md").read_text(
        encoding="utf-8"
    ) == render_upgrade_guide()


def test_a_section_says_what_each_kind_of_migration_asks() -> None:
    guide = render_upgrade_guide((CHOICE, MANUAL))

    assert (
        '<a id="retries"></a>\n\n'
        "## Settings key `retries`\n\n"
        "Migration `retries`: deprecated in ww 1.0.0b20; removed in ww 2.0.0. "
        "Found in settings files"
    ) in guide
    assert "Why: Retries moved under limits.\n" in guide
    assert "takes `--choose retries=<option>`" in guide
    assert (
        "- `move`: The same number of retries applies. ww will move `retries` to "
        "`limits.retries`, in the mapping holding it.\n"
        "- `drop`: The default number of retries applies. ww will delete `retries`.\n"
    ) in guide
    assert 'Before:\n\n```json\n{"retries": 2}\n```\n' in guide
    assert "## shell-hooks\n" in guide
    assert "no removal is scheduled" in guide
    assert "workflow files (`ww.yaml`" in guide and "rule files' frontmatter" in guide
    assert (
        "Manual: change it by hand, in one of these ways:\n\n"
        "- `command`: The hook runs as a command step.\n"
    ) in guide
    assert render_upgrade_guide(()).endswith(
        "Nothing is deprecated or removed today.\n"
    )


def test_an_automatic_section_lists_its_changes_and_example() -> None:
    section = read_design_document("upgrade#assessment-prompt-key-question")

    assert section.startswith("## Assessment prompt key `question`\n")
    assert "- rename `**.assess.question` to `assess`\n" in section
    assert "```yaml\n- assess:\n    assess: Are there issues?\n" in section
    assert section.endswith("```\n")


def test_docs_prints_the_guide_or_one_section(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        catalog,
        "CONFIG_MIGRATIONS",
        (
            CHOICE,
            MANUAL,
            ConfigMigration(
                id="old",
                deprecated_in="1.0.0b20",
                why="old is new",
                anchor="old",
                files=("workflows",),
                changes=(rename_key("old", "new"),),
            ),
        ),
    )

    assert main(["--root", str(tmp_path), "docs", "upgrade"]) == 0
    assert capsys.readouterr().out == render_upgrade_guide()

    assert main(["--root", str(tmp_path), "docs", "upgrade#shell-hooks"]) == 0
    section = capsys.readouterr().out
    assert section.startswith("## shell-hooks\n")
    assert "retries" not in section and '<a id="old">' not in section

    assert main(["--root", str(tmp_path), "docs", "upgrade#nowhere"]) == 1
    assert "the upgrade document has no section 'nowhere'" in capsys.readouterr().err


def test_a_section_of_any_document_is_found_by_its_heading() -> None:
    section = read_design_document("features#direct-work")

    assert section.startswith("## Direct work\n")
    assert "\n## " not in section
    with pytest.raises(StateError, match="unknown document 'nothing'"):
        read_design_document("nothing#direct-work")
