# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration migrations: key paths, changes, file edits and load-time use."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from ww.errors import ConfigurationError
from ww.migrations.config import (
    Change,
    ConfigFile,
    ConfigMigration,
    KeyPath,
    Location,
    MigrationConflictError,
    MigrationSet,
    Option,
    TextEdit,
    YamlText,
    delete_key,
    migrate_file,
    migrate_loaded,
    move_key,
    rename_key,
    set_value,
    write_files,
)
from ww.migrations.config.catalog import ASSESS_QUESTION


def _migration(*changes: Change, **fields: Any) -> ConfigMigration:
    return ConfigMigration(
        **{
            "id": "test",
            "deprecated_in": "1.0.0",
            "why": "the old shape is gone",
            "anchor": "test",
            "files": ("workflows", "rules", "settings"),
            "changes": changes,
            **fields,
        }
    )


def _set(*migrations: ConfigMigration, version: str = "1.5.0") -> MigrationSet:
    return MigrationSet(migrations, version)


def _migrate(
    tmp_path: Path,
    text: str,
    *migrations: ConfigMigration,
    kind: str = "workflows",
    name: str = "ww.yaml",
    choices: dict[str, str] | None = None,
):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return migrate_file(
        ConfigFile(path, kind, name),  # type: ignore[arg-type]
        sets=(_set(*migrations),),
        choices=choices,
    )


# ----- key paths -------------------------------------------------------------


def test_a_key_path_finds_keys_through_mappings_lists_and_any_depth() -> None:
    document = {
        "workflows": [
            {"name": "a", "steps": [{"assess": {"question": "Q?"}}]},
            {
                "name": "b",
                "hooks": {"after": [{"steps": [{"assess": {"question": "R?"}}]}]},
            },
        ]
    }

    assert KeyPath.parse("workflows[*].steps[*].assess.question").find(document) == (
        ("workflows", 0, "steps", 0, "assess", "question"),
    )
    assert KeyPath.parse("**.assess.question").find(document) == (
        ("workflows", 0, "steps", 0, "assess", "question"),
        ("workflows", 1, "hooks", "after", 0, "steps", 0, "assess", "question"),
    )
    assert KeyPath.parse("workflows[*].*").find(document)[:2] == (
        ("workflows", 0, "name"),
        ("workflows", 0, "steps"),
    )


def test_a_key_path_filter_keeps_only_matching_mappings() -> None:
    document = {"steps": [{"name": "assess", "question": "Q?"}, {"question": "no"}]}

    assert KeyPath.parse("**[name=assess].question").find(document) == (
        ("steps", 0, "question"),
    )


def test_a_key_path_must_end_with_a_key_and_reads_back() -> None:
    with pytest.raises(ValueError, match="must end with a key"):
        KeyPath.parse("workflows[*]")
    assert str(KeyPath.parse("**[name=assess].question")) == "**[name=assess].question"
    assert str(KeyPath.parse("a.b").under(("extensions", "acme/x"))) == (
        "extensions.acme/x.a.b"
    )


def test_a_key_path_does_not_follow_a_recursive_document_forever() -> None:
    loop: dict[str, Any] = {"assess": {"question": "Q?"}}
    loop["self"] = loop

    assert KeyPath.parse("**.assess.question").find(loop) == (("assess", "question"),)


# ----- changes in memory -----------------------------------------------------


def test_rename_keeps_the_key_in_place_and_is_idempotent() -> None:
    migration = _migration(rename_key("a.old", "new"))
    document = {"a": {"first": 1, "old": 2, "last": 3}}

    migrated = migration.transform(document)

    assert list(migrated["a"]) == ["first", "new", "last"]
    assert document == {"a": {"first": 1, "old": 2, "last": 3}}
    assert migration.transform(migrated) == migrated
    assert migration.find(migrated) == ()


def test_delete_set_and_move_change_a_parsed_document() -> None:
    document = {
        "settings": {"timeout": 5, "mode": "fast", "keep": True},
        "limits": {"fixes": 3},
    }

    assert _migration(delete_key("settings.keep")).transform(document) == {
        "settings": {"timeout": 5, "mode": "fast"},
        "limits": {"fixes": 3},
    }
    renamed = _migration(set_value("settings.mode", {"fast": "quick"}))
    assert renamed.transform(document)["settings"]["mode"] == "quick"
    moved = _migration(move_key("settings.timeout", "limits.timeout", up=1)).transform(
        document
    )
    assert moved["limits"] == {"fixes": 3, "timeout": 5}
    assert "timeout" not in moved["settings"]
    created = _migration(move_key("settings.timeout", "time.limit")).transform(document)
    assert created["settings"]["time"] == {"limit": 5}


def test_set_value_only_finds_the_values_it_maps() -> None:
    change = set_value("mode", {"fast": "quick"})

    assert change.locations({"mode": "slow"}) == ()
    assert change.locations({"mode": True}) == ()
    assert change.locations({"mode": "fast"}) == (("mode",),)


def test_both_shapes_at_once_is_a_conflict() -> None:
    with pytest.raises(MigrationConflictError, match="cannot combine new and old"):
        _migration(rename_key("old", "new")).transform({"old": 1, "new": 2})


def test_a_migration_declares_changes_options_or_detection() -> None:
    with pytest.raises(ValueError, match="needs changes or options"):
        _migration()
    with pytest.raises(ValueError, match="needs detect paths"):
        _migration(changes=(), options=(Option("drop", "it is lost"),))
    with pytest.raises(ValueError, match="every option of a choice"):
        _migration(
            changes=(),
            options=(
                Option("a", "x", (delete_key("a"),)),
                Option("b", "y"),
            ),
            detect=("a",),
        )


def test_a_migration_is_deprecated_then_removed_then_forgotten() -> None:
    migration = _migration(rename_key("old", "new"), removed_in="2.0.0")

    assert migration.status("1.9.0") == "deprecated"
    assert migration.status("2.0.0") == "removed"
    # The removal lands in the first prerelease of the version that removes it.
    assert migration.status("2.0.0b1") == "removed"
    assert migration.status("2.0.0.dev3") == "removed"
    assert migration.status("2.4.1") == "removed"
    assert migration.status("3.0.0") == "expired"
    assert migration.status("dev") == "deprecated"
    assert _migration(rename_key("old", "new")).status("9.0.0") == "deprecated"


# ----- in memory at load -----------------------------------------------------


def test_loading_migrates_a_deprecated_construct_with_a_notice() -> None:
    text = "a:\n  b: 1\n  old: 2\n"
    migrated = migrate_loaded(
        yaml.safe_load(text),
        "ww.yaml",
        (_set(_migration(rename_key("a.old", "new"))),),
        text=text,
    )

    assert migrated.data == {"a": {"b": 1, "new": 2}}
    assert migrated.notices == (
        "Deprecated: ww.yaml:a.old (line 3): the old shape is gone (deprecated in "
        "ww 1.0.0); run ww migrate to fix. See "
        "ww docs upgrade#test.",
    )


def test_loading_refuses_a_removed_construct_by_name() -> None:
    migration = _migration(rename_key("old", "new"), removed_in="2.0.0")

    with pytest.raises(
        ConfigurationError,
        match=r"ww.json:old: the old shape is gone; removed in ww 2.0.0: run ww "
        r"migrate to fix. See ww docs upgrade#test",
    ):
        migrate_loaded({"old": 1}, "ww.json", (_set(migration, version="2.1.0"),))
    forgotten = migrate_loaded(
        {"old": 1}, "ww.json", (_set(migration, version="3.0.0"),)
    )
    assert forgotten.data == {"old": 1}
    assert forgotten.notices == ()


def test_loading_refuses_both_shapes_at_once() -> None:
    with pytest.raises(ConfigurationError, match="ww.yaml: cannot combine new"):
        migrate_loaded(
            {"old": 1, "new": 2},
            "ww.yaml",
            (_set(_migration(rename_key("old", "new"))),),
        )


def test_a_manual_migration_only_warns_and_a_choice_applies_its_first_option() -> None:
    manual = _migration(
        changes=(),
        options=(Option("split", "two workflows run instead of one"),),
        detect=("legacy",),
        id="manual",
    )
    choice = _migration(
        changes=(),
        options=(
            Option("keep", "behaves as before", (rename_key("mode", "old_mode"),)),
            Option("drop", "the mode is lost", (delete_key("mode"),)),
        ),
        id="choice",
    )

    migrated = migrate_loaded(
        {"legacy": 1, "mode": "x"}, "ww.yaml", (_set(manual, choice),)
    )

    assert migrated.data == {"legacy": 1, "old_mode": "x"}
    assert (
        "change legacy by hand: split (two workflows run instead of one)"
        in migrated.notices[0]
    )
    assert (
        "run ww migrate to choose: keep (behaves as before); drop"
        in migrated.notices[1]
    )


# ----- YAML files ------------------------------------------------------------

_COMMENTED = """\
# The project's workflows.
workflows:
  - name: task   # the main one

    steps:
      - review: Review it.
        assess:
          "question": 'Are there issues?'  # kept as it is
          positive:
            steps:
              - fix: Fix them.
      - name: assess
        question: Done?
"""


def test_a_yaml_edit_changes_only_the_keys_and_keeps_everything_else(
    tmp_path: Path,
) -> None:
    migration = _migrate(tmp_path, _COMMENTED, ASSESS_QUESTION)

    assert migration.text == _COMMENTED.replace('"question"', '"assess"').replace(
        "        question: Done?", "        assess: Done?"
    )
    assert [
        (finding.line, finding.kind, finding.fixed) for finding in migration.findings
    ] == [(8, "automatic", True), (13, "automatic", True)]
    assert migration.findings[0].where == (
        "ww.yaml:workflows[0].steps[0].assess.question (line 8)"
    )
    assert migration.diff().startswith("--- a/ww.yaml\n+++ b/ww.yaml\n")
    assert yaml.safe_load(migration.text) == ASSESS_QUESTION.transform(
        yaml.safe_load(_COMMENTED)
    )


def test_a_migrated_file_has_nothing_left_to_change(tmp_path: Path) -> None:
    first = _migrate(tmp_path, _COMMENTED, ASSESS_QUESTION)
    second = _migrate(tmp_path, first.text, ASSESS_QUESTION)

    assert second.findings == ()
    assert not second.changed


_SETTINGS = """\
# head
settings:
  timeout: 5   # seconds
  other: 1

  # about mode
  mode: fast
limits:
  fixes: 3
"""


def test_a_yaml_delete_removes_the_entry_and_its_inline_comment(
    tmp_path: Path,
) -> None:
    migration = _migrate(
        tmp_path, _SETTINGS, _migration(delete_key("settings.timeout"))
    )

    assert migration.text == _SETTINGS.replace("  timeout: 5   # seconds\n", "")


def test_a_yaml_value_is_replaced_in_its_place(tmp_path: Path) -> None:
    migration = _migrate(
        tmp_path, _SETTINGS, _migration(set_value("settings.mode", {"fast": "quick"}))
    )

    assert migration.text == _SETTINGS.replace("mode: fast", "mode: quick")


def test_a_yaml_move_takes_the_entry_to_an_existing_or_new_mapping(
    tmp_path: Path,
) -> None:
    into_limits = _migrate(
        tmp_path,
        _SETTINGS,
        _migration(move_key("settings.timeout", "limits.timeout", up=1)),
    )
    into_new = _migrate(
        tmp_path, _SETTINGS, _migration(move_key("settings.timeout", "time.limit"))
    )

    assert into_limits.text == (
        _SETTINGS.replace("  timeout: 5   # seconds\n", "")
        + "  timeout: 5   # seconds\n"
    )
    assert into_new.text == _SETTINGS.replace("  timeout: 5   # seconds\n", "").replace(
        "  mode: fast\n", "  mode: fast\n  time:\n    limit: 5   # seconds\n"
    )


def test_a_yaml_move_reindents_a_block_value(tmp_path: Path) -> None:
    text = "a:\n  b:\n    - x\n    - y  # last\n  c: 1\n"

    migration = _migrate(tmp_path, text, _migration(move_key("a.b", "d.b", up=1)))

    assert migration.text == "a:\n  c: 1\nd:\n  b:\n    - x\n    - y  # last\n"


def test_a_key_after_a_list_dash_is_deleted_without_the_dash(tmp_path: Path) -> None:
    text = "steps:\n  - question: x\n    name: assess\n"

    migration = _migrate(tmp_path, text, _migration(delete_key("steps[*].question")))

    assert migration.text == "steps:\n  - name: assess\n"


@pytest.mark.parametrize(
    ("text", "change", "reason", "line"),
    [
        (
            "a: {timeout: 1, b: 2}\n",
            delete_key("a.timeout"),
            "is in a flow mapping",
            1,
        ),
        (
            "steps:\n  - assess: {question: Flow}\n",
            rename_key("**.assess.question", "assess"),
            "is in a flow mapping",
            2,
        ),
        (
            "a: &shared\n  timeout: 1\n  b: 2\nc: *shared\n",
            delete_key("a.timeout"),
            "anchor or alias",
            2,
        ),
        (
            "a:\n  timeout: |\n    long\n  b: 2\n",
            delete_key("a.timeout"),
            "block scalar",
            2,
        ),
        (
            "a:\n  mode: |\n    fast\n",
            set_value("a.mode", {"fast\n": "quick"}),
            "not a one-line scalar",
            2,
        ),
        (
            "a:\n  b: 1\n---\na:\n  b: 2\n",
            rename_key("a.b", "c"),
            "more than one YAML document",
            2,
        ),
    ],
)
def test_an_unsafe_yaml_edit_is_left_for_the_operator_with_its_line(
    tmp_path: Path, text: str, change: Change, reason: str, line: int
) -> None:
    migration = _migrate(tmp_path, text, _migration(change))

    assert migration.text == text
    [finding] = migration.findings
    assert (finding.kind, finding.fixed, finding.line) == ("manual", False, line)
    assert reason in finding.message
    assert f"at line {line} by hand" in finding.message


@dataclass(frozen=True)
class _WrongEdit(Change):
    """A change whose text edit does not match its in-memory form."""

    def apply(self, document: Any, location: Location) -> None:
        document.pop(location[-1])

    def instruction(self, location: Location) -> str:
        return "delete it"

    def edit(self, text: YamlText, location: Location) -> list[TextEdit]:
        # Writes a different value instead of deleting the key.
        node = text.entry(location).value
        return [TextEdit(node.start_mark.index, node.end_mark.index, "2")]


def test_an_edit_that_does_not_read_back_as_the_migration_is_not_written(
    tmp_path: Path,
) -> None:
    text = "keep: 1\nold: 1\n"

    migration = _migrate(tmp_path, text, _migration(_WrongEdit(KeyPath.parse("old"))))

    assert migration.text == text
    [finding] = migration.findings
    assert finding.kind == "manual"
    assert "would not read as the migrated configuration" in finding.message


def test_a_conflict_in_a_file_is_left_for_the_operator(tmp_path: Path) -> None:
    text = "old: 1\nnew: 2\n"

    migration = _migrate(tmp_path, text, _migration(rename_key("old", "new")))

    assert migration.text == text
    assert "cannot combine new and old" in migration.findings[0].message


def test_a_choice_changes_the_file_only_once_chosen(tmp_path: Path) -> None:
    choice = _migration(
        changes=(),
        options=(
            Option("keep", "as before", (rename_key("mode", "old_mode"),)),
            Option("drop", "lost", (delete_key("mode"),)),
        ),
    )
    text = "mode: x\nother: 1\n"

    pending = _migrate(tmp_path, text, choice)
    chosen = _migrate(tmp_path, text, choice, choices={"test": "drop"})

    assert (pending.text, pending.findings[0].kind) == (text, "choice")
    assert chosen.text == "other: 1\n"
    assert chosen.findings[0].fixed


def test_a_rule_files_frontmatter_is_migrated_and_its_body_kept(
    tmp_path: Path,
) -> None:
    text = "---\npaths: ['*.py']\nold_key: 1  # note\n---\nUse old_key: never.\n"

    migration = _migrate(
        tmp_path,
        text,
        _migration(rename_key("old_key", "new_key")),
        kind="rules",
        name="rule.md",
    )

    assert migration.text == text.replace("old_key: 1", "new_key: 1")
    assert migration.findings[0].line == 3


# ----- JSON files ------------------------------------------------------------


def test_a_json_settings_file_is_rewritten_as_ww_writes_it(tmp_path: Path) -> None:
    text = '{"update_checks": false,\n    "limits": {"fixes": 2}}'

    migration = _migrate(
        tmp_path,
        text,
        _migration(rename_key("update_checks", "update_check")),
        kind="settings",
        name="ww.json",
    )

    assert migration.text == (
        json.dumps({"update_check": False, "limits": {"fixes": 2}}, indent=2) + "\n"
    )
    assert migration.findings[0].fixed


def test_an_extensions_migrations_apply_inside_its_section(tmp_path: Path) -> None:
    path = tmp_path / "ww.json"
    path.write_text(
        '{\n  "extensions": {\n    "tickets": {"project": "X"},\n'
        '    "ww/git": {"project": "kept"}\n  }\n}\n',
        encoding="utf-8",
    )
    extension = MigrationSet(
        (
            _migration(
                rename_key("project", "project_key"),
                files=("settings",),
                guide="https://acme.example/upgrading",
            ),
        ),
        "2.0.0",
        "acme/tickets",
    )

    migration = migrate_file(
        ConfigFile(path, "settings", "ww.json"),
        sets=(),
        extensions={"acme/tickets": extension},
    )

    assert json.loads(migration.text)["extensions"] == {
        "tickets": {"project_key": "X"},
        "ww/git": {"project": "kept"},
    }
    [finding] = migration.findings
    assert (finding.location, finding.line) == ("extensions.tickets.project", 3)
    assert "deprecated in acme/tickets 1.0.0" in finding.message
    assert "https://acme.example/upgrading#test" in finding.message


def test_changed_files_are_written_together(tmp_path: Path) -> None:
    changed = _migrate(tmp_path, "old: 1\n", _migration(rename_key("old", "new")))
    unchanged = _migrate(
        tmp_path, "new: 1\n", _migration(rename_key("old", "new")), name="other.yaml"
    )

    assert write_files((changed, unchanged)) == (tmp_path / "ww.yaml",)
    assert (tmp_path / "ww.yaml").read_text(encoding="utf-8") == "new: 1\n"
