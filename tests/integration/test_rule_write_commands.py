# SPDX-License-Identifier: GPL-3.0-or-later
"""The validated rule writes: rules add, add --group, edit, move, filter, promote."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ww.cli import main
from ww.config import load_configuration
from ww.config.rules import rule_text_hash
from ww.rule_store import STORE_FILE, RuleStore

WORKFLOWS = """# The project's workflows.
rules:
  docs: [rules/docs/]
workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        rules:
          - Keep the public CLI unchanged.
      - review: Review it.
"""
HEADER_RULE = """---
# Markdown only.
paths: ["*.md"]
max_fixes: 2
---
Include "foo" in every Markdown file you change.

A marker keeps the documentation searchable.
"""
SERVICES = "Put every service under `src/Service/`."


def _project(root: Path, workflows: str = WORKFLOWS) -> Path:
    (root / "rules/docs").mkdir(parents=True)
    (root / "rules/docs/header.md").write_text(HEADER_RULE, encoding="utf-8")
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    (root / "README.md").write_text("foo\n", encoding="utf-8")
    return root


def _ww(root: Path, *arguments: str) -> int:
    return main(["--root", str(root), *arguments])


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _store(root: Path, rules: dict[str, object], checks: dict[str, object]) -> None:
    (root / STORE_FILE).write_text(
        json.dumps({"schema_version": 1, "rules": rules, "checks": checks}),
        encoding="utf-8",
    )


def _converted(text: str, check: str) -> dict[str, object]:
    return {"text": text, "status": "converted", "check": check}


def _check(covers: list[str], **extra: object) -> dict[str, object]:
    return {
        "shell": "grep -L foo $WW_STEP_CHANGED_FILES || true",
        "assert": ["empty"],
        "config": [],
        "covers": covers,
        "proven": True,
        "status": "converted",
        **extra,
    }


# rules add --------------------------------------------------------------------


def test_add_writes_a_rule_file_into_the_groups_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    (root / "src/Service").mkdir(parents=True)
    (root / "src/Service/Mail.php").write_text("<?php\n", encoding="utf-8")

    assert (
        _ww(
            root,
            "rules",
            "add",
            "docs",
            "--text",
            f"{SERVICES}\n\nOne class per file.",
            "--paths",
            "src/**/*.php",
            "--check-shell",
            "find $WW_STEP_CHANGED_FILES -name '*Service.php' "
            "-not -path 'src/Service/*'",
            "--assert",
            "empty",
        )
        == 0
    )

    out = capsys.readouterr().out
    file = root / "rules/docs/put-every-service-under-src.md"
    assert _text(file) == (
        "---\n"
        "paths: [src/**/*.php]\n"
        "check:\n"
        "  shell: find $WW_STEP_CHANGED_FILES -name '*Service.php' -not -path "
        "'src/Service/*'\n"
        "  assert: [empty]\n"
        "---\n"
        f"{SERVICES}\n\nOne class per file.\n"
    )
    assert "Created rules/docs/put-every-service-under-src.md: rule " in out
    assert "`src/**/*.php` matches 1 file(s) now." in out
    assert "- task: develop, review" in out
    configuration = load_configuration(root / "ww.yaml")
    (group,) = configuration.rule_groups
    rule = next(rule for rule in group.rules if rule.id.endswith("under-src"))
    assert rule.check is not None and rule.paths == ("src/**/*.php",)


def test_add_takes_an_id_and_never_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _ww(root, "rules", "add", "docs", "--text", SERVICES, "--id", "header") == 1

    assert "rules/docs/header.md already exists" in capsys.readouterr().err
    assert _text(root / "rules/docs/header.md") == HEADER_RULE
    assert _ww(root, "rules", "add", "docs", "--text", SERVICES, "--id", "Bad_ID") == 1
    assert "lower-case kebab-case" in capsys.readouterr().err


def test_add_warns_about_a_glob_that_matches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert (
        _ww(root, "rules", "add", "docs", "--text", SERVICES, "--paths", "*.php") == 0
    )

    out = capsys.readouterr().out
    assert "Warning: `*.php` matches no file in the project" in out


def test_add_refuses_a_group_without_a_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(
        tmp_path,
        WORKFLOWS.replace(
            "  docs: [rules/docs/]",
            "  docs: [rules/docs/]\n  all: [docs, rules/docs/header.md]",
        ),
    )

    assert _ww(root, "rules", "add", "all", "--text", SERVICES) == 1

    assert "lists no directory to add a rule file to" in capsys.readouterr().err
    assert _ww(root, "rules", "add", "nothing", "--text", SERVICES) == 1
    assert "no rule group 'nothing'; the groups are all, docs" in (
        capsys.readouterr().err
    )


def test_add_refuses_a_write_the_configuration_would_reject(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(
        tmp_path, WORKFLOWS.replace("[rules/docs/]", "[rules/docs/, rules/more/]")
    )
    (root / "rules/more").mkdir()
    (root / "rules/more/services.md").write_text(SERVICES + "\n", encoding="utf-8")

    # The new file's stem is already a rule of the group, from its other folder.
    assert (
        _ww(root, "rules", "add", "docs", "--text", SERVICES, "--id", "services") == 1
    )

    err = capsys.readouterr().err
    assert "refused: the configuration would not be valid" in err
    assert "two rules named 'services'" in err
    assert "nothing was written" in err
    assert not (root / "rules/docs/services.md").exists()


def test_a_dry_run_validates_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _ww(root, "rules", "add", "docs", "--text", SERVICES, "--dry-run") == 0

    out = capsys.readouterr().out
    assert "Dry run: the configuration would be valid; nothing was written." in out
    assert sorted(path.name for path in (root / "rules/docs").iterdir()) == [
        "header.md"
    ]


def test_add_options_are_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert (
        _ww(root, "rules", "add", "docs", "--text", SERVICES, "--assert", "empty") == 1
    )
    assert "--assert needs --check-shell or --check-argv" in capsys.readouterr().err
    assert (
        _ww(
            root,
            "rules",
            "add",
            "docs",
            "--text",
            SERVICES,
            "--check-argv",
            "x",
            "--assert",
            "ne:1",
        )
        == 1
    )
    assert "--assert takes empty or equals:<value>" in capsys.readouterr().err
    assert _ww(root, "rules", "add", "docs", "--steps", "develop", "--text", "A.") == 1
    assert "--dir, --workflows and --steps go with --group" in capsys.readouterr().err
    assert _ww(root, "rules", "add", "docs") == 1
    assert "rules add takes GROUP with --text" in capsys.readouterr().err


# rules add --group ------------------------------------------------------------


def test_add_group_writes_the_import_file_and_only_adds_the_import(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert (
        _ww(
            root,
            "rules",
            "add",
            "--group",
            "php",
            "--dir",
            "rules/php",
            "--workflows",
            "task",
            "--steps",
            "develop",
        )
        == 0
    )

    out = capsys.readouterr().out
    assert _text(root / "ww.yaml") == WORKFLOWS.replace(
        "rules:\n", "imports:\n  - ww-rules.yaml\nrules:\n", 1
    )
    assert _text(root / "ww-rules.yaml").endswith(
        "rules:\n  php:\n    rules: [rules/php/]\n    workflows: [task]\n"
        "    steps: [develop]\n"
    )
    assert (root / "rules/php").is_dir()
    assert "Added rule group `php` (rules/php/) to ww-rules.yaml." in out
    assert "Added ww-rules.yaml to imports in ww.yaml." in out
    assert "git does not keep an empty directory" in out
    assert "- task: develop" in out
    # A second group joins the same file; the repo file is not touched again.
    before = _text(root / "ww.yaml")
    assert _ww(root, "rules", "add", "--group", "style", "--dir", "rules/docs") == 0
    assert _text(root / "ww.yaml") == before
    groups = load_configuration(root / "ww.yaml").rule_groups
    assert [group.name for group in groups] == ["php", "style", "docs"]
    assert _ww(root, "rules", "add", "php", "--text", SERVICES) == 0
    assert (root / "rules/php/put-every-service-under-src.md").is_file()


@pytest.mark.parametrize(
    ("imports", "expected"),
    [
        (
            "imports:\n  - extra.yaml\n",
            "imports:\n  - extra.yaml\n  - ww-rules.yaml\n",
        ),
        ("imports: [extra.yaml]\n", "imports: [extra.yaml, ww-rules.yaml]\n"),
        (
            "imports:\n- extra.yaml  # shared\n",
            "imports:\n- extra.yaml  # shared\n- ww-rules.yaml\n",
        ),
    ],
)
def test_add_group_extends_an_existing_imports_list(
    tmp_path: Path, imports: str, expected: str
) -> None:
    root = _project(tmp_path, "")
    (root / "extra.yaml").write_text("modes: []\n", encoding="utf-8")
    (root / "ww.yaml").write_text(imports + WORKFLOWS, encoding="utf-8")

    assert _ww(root, "rules", "add", "--group", "php", "--dir", "rules/php") == 0

    assert _text(root / "ww.yaml") == expected + WORKFLOWS


def test_add_group_refuses_bad_filters_and_leaves_everything_as_it_was(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert (
        _ww(
            root,
            "rules",
            "add",
            "--group",
            "php",
            "--dir",
            "rules/php/new",
            "--steps",
            "deploy",
        )
        == 1
    )

    assert "unknown step(s): deploy" in capsys.readouterr().err
    assert _text(root / "ww.yaml") == WORKFLOWS
    assert not (root / "ww-rules.yaml").exists()
    assert not (root / "rules/php").exists()


def test_add_group_refuses_a_name_in_use_and_an_unimported_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _ww(root, "rules", "add", "--group", "docs", "--dir", "rules/x") == 1
    assert "rule group 'docs' already exists" in capsys.readouterr().err
    (root / "ww-rules.yaml").write_text("rules: {}\n", encoding="utf-8")
    assert _ww(root, "rules", "add", "--group", "php", "--dir", "rules/php") == 1
    assert "ww-rules.yaml exists but ww.yaml does not import it" in (
        capsys.readouterr().err
    )
    assert _ww(root, "rules", "add", "--group", "php", "--dir", "../elsewhere") == 1
    assert "is outside the project" in capsys.readouterr().err


# rules edit -------------------------------------------------------------------


def test_edit_replaces_the_body_and_keeps_the_frontmatter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    old = rule_text_hash(HEADER_RULE.split("---\n", 2)[2])
    _store(root, {old: _converted("Include foo.", "foo-header")}, {})

    assert (
        _ww(root, "rules", "edit", "docs/header", "--text", 'Include "bar" instead.')
        == 0
    )

    out = capsys.readouterr().out
    assert _text(root / "rules/docs/header.md") == (
        HEADER_RULE.split("Include")[0] + 'Include "bar" instead.\n'
    )
    assert f"its hash changes from {old[:12]} to " in out
    assert f"store entry {old[:12]} (converted) stops matching this rule" in out
    assert "Its approved check `foo-header` stops running for this rule" in out
    assert "`rules promote foo-header`" in out


def test_edit_replaces_only_the_globs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _ww(root, "rules", "edit", "docs/header", "--paths", "docs/**", "*.md") == 0

    assert _text(root / "rules/docs/header.md") == HEADER_RULE.replace(
        'paths: ["*.md"]\nmax_fixes: 2\n', "max_fixes: 2\npaths: [docs/**, '*.md']\n"
    )
    assert "`*.md` matches 2 file(s) now." in capsys.readouterr().out
    assert _ww(root, "rules", "edit", "docs/header") == 1
    assert "rules edit needs --text, --paths, or both" in capsys.readouterr().err


def test_edit_notes_a_wording_that_keeps_its_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    body = HEADER_RULE.split("---\n", 2)[2]

    assert _ww(root, "rules", "edit", "docs/header", "--text", "  " + body) == 0

    out = capsys.readouterr().out
    assert "The wording is unchanged apart from whitespace." in out
    assert "hash changes" not in out


def test_edit_refuses_a_rule_written_in_the_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _ww(root, "rules", "edit", "develop/1", "--text", "Other.") == 1
    assert "written in a step's own `rules` list" in capsys.readouterr().err
    assert _ww(root, "rules", "edit", "docs/nothing", "--text", "Other.") == 1
    assert "no rule 'docs/nothing' is declared" in capsys.readouterr().err
    assert _text(root / "ww.yaml") == WORKFLOWS


# rules move -------------------------------------------------------------------


def test_move_carries_the_file_unchanged_into_another_group(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(
        tmp_path,
        WORKFLOWS.replace(
            "  docs: [rules/docs/]",
            "  docs: [rules/docs/]\n  style:\n    rules: [rules/style/]\n"
            "    steps: [review]",
        ),
    )
    (root / "rules/style").mkdir()
    (root / "rules/style/tone.md").write_text("Write plainly.\n", encoding="utf-8")

    assert _ww(root, "rules", "move", "docs/header", "style") == 0

    out = capsys.readouterr().out
    assert _text(root / "rules/style/header.md") == HEADER_RULE
    assert not (root / "rules/docs/header.md").exists()
    assert "rule `docs/header` is now `style/header`" in out
    assert "- task: review" in out
    assert _ww(root, "rules", "move", "style/header", "style") == 1
    assert "is already in group 'style'" in capsys.readouterr().err


def test_move_refuses_a_name_the_target_group_has(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(
        tmp_path,
        WORKFLOWS.replace(
            "  docs: [rules/docs/]",
            "  docs: [rules/docs/]\n  more: [rules/more/]",
        ),
    )
    (root / "rules/more").mkdir()
    (root / "rules/more/header.md").write_text("Other.\n", encoding="utf-8")

    assert _ww(root, "rules", "move", "docs/header", "more") == 1

    assert "rules/more/header.md already exists" in capsys.readouterr().err
    assert (root / "rules/docs/header.md").is_file()


def test_move_puts_the_file_back_when_its_old_group_names_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(
        tmp_path,
        WORKFLOWS.replace(
            "  docs: [rules/docs/]",
            "  docs: [rules/docs/]\n  pinned: [rules/docs/header.md]\n"
            "  more: [rules/more/]",
        ),
    )
    (root / "rules/more").mkdir()

    assert _ww(root, "rules", "move", "docs/header", "more") == 1

    assert "refused: the configuration would not be valid" in capsys.readouterr().err
    assert _text(root / "rules/docs/header.md") == HEADER_RULE
    assert not (root / "rules/more/header.md").exists()


# rules filter -----------------------------------------------------------------


def test_filter_rewrites_a_group_of_the_import_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert (
        _ww(
            root,
            "rules",
            "add",
            "--group",
            "php",
            "--dir",
            "rules/docs",
            "--steps",
            "develop",
        )
        == 0
    )
    capsys.readouterr()

    assert (
        _ww(
            root,
            "rules",
            "filter",
            "php",
            "--steps",
            "develop",
            "review",
            "--workflows",
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "Changed the filters of rule group `php` in ww-rules.yaml." in out
    assert "Reaches no step yet" in out
    group = load_configuration(root / "ww.yaml").rule_groups_by_name["php"]
    assert (group.workflows.names, group.steps.names) == ((), ("develop", "review"))
    assert _ww(root, "rules", "filter", "php", "--all-workflows", "--all-steps") == 0
    group = load_configuration(root / "ww.yaml").rule_groups_by_name["php"]
    assert group.workflows.admits_all and group.steps.admits_all
    assert "- task: develop, review" in capsys.readouterr().out


def test_a_star_filter_option_writes_star(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert (
        _ww(
            root,
            "rules",
            "add",
            "--group",
            "php",
            "--dir",
            "rules/docs",
            "--steps",
            "*",
        )
        == 0
    )
    assert yaml.safe_load(_text(root / "ww-rules.yaml"))["rules"]["php"] == {
        "rules": ["rules/docs/"],
        "steps": "*",
    }

    assert _ww(root, "rules", "filter", "php", "--workflows", "*") == 0

    entry = yaml.safe_load(_text(root / "ww-rules.yaml"))["rules"]["php"]
    assert (entry["workflows"], entry["steps"]) == ("*", "*")
    group = load_configuration(root / "ww.yaml").rule_groups_by_name["php"]
    assert group.workflows.admits_all and group.steps.admits_all
    capsys.readouterr()

    assert _ww(root, "rules", "--json") == 0
    listed = json.loads(capsys.readouterr().out)["groups"][0]
    assert (listed["workflows"], listed["steps"]) == ("*", "*")


def test_a_star_filter_option_cannot_mix_with_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _ww(root, "rules", "add", "--group", "php", "--dir", "rules/docs") == 0
    capsys.readouterr()

    assert _ww(root, "rules", "filter", "php", "--steps", "*", "develop") == 1

    assert "--steps takes '*' alone or names, not both" in capsys.readouterr().err


def test_filter_refuses_a_group_declared_by_hand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _ww(root, "rules", "filter", "docs", "--steps", "develop") == 1

    err = capsys.readouterr().err
    assert "declared in ww.yaml, which ww does not rewrite" in err
    assert "rules.docs: steps: [develop]" in err
    assert _ww(root, "rules", "filter", "docs") == 1
    assert "rules filter needs --workflows" in capsys.readouterr().err


def test_filter_refuses_a_group_the_repo_file_declares_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _ww(root, "rules", "add", "--group", "php", "--dir", "rules/docs") == 0
    text = _text(root / "ww.yaml")
    (root / "ww.yaml").write_text(
        text.replace(
            "  docs: [rules/docs/]",
            "  docs: [rules/docs/]\n  php: [rules/docs/]",
        ),
        encoding="utf-8",
    )
    capsys.readouterr()

    assert _ww(root, "rules", "filter", "php", "--steps", "review") == 1

    assert "is declared again in ww.yaml" in capsys.readouterr().err


# rules promote ----------------------------------------------------------------


def test_promote_moves_an_approved_check_into_the_rule_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    (root / "rules/docs/tone.md").write_text(
        "Write plainly.\n\nNo jargon.\n", encoding="utf-8"
    )
    header = rule_text_hash(HEADER_RULE.split("---\n", 2)[2])
    tone = rule_text_hash("Write plainly.\n\nNo jargon.")
    other = rule_text_hash("Keep the public CLI unchanged.")
    _store(
        root,
        {
            header: _converted("Include foo.", "docs-lint"),
            tone: _converted("Write plainly.", "docs-lint"),
            other: {"text": "Keep the public CLI unchanged.", "status": "rejected"},
        },
        {"docs-lint": _check([header, tone], config=["docs-lint.yaml"])},
    )
    assert _ww(root, "rules", "--json") == 0
    listed = json.loads(capsys.readouterr().out)["groups"][0]["rules"]
    assert [rule["store_check"] for rule in listed] == ["docs-lint", "docs-lint"]

    assert _ww(root, "rules", "promote", "docs-lint") == 0

    out = capsys.readouterr().out
    check = (
        "check:\n  shell: grep -L foo $WW_STEP_CHANGED_FILES || true\n"
        "  assert: [empty]\n"
    )
    assert _text(root / "rules/docs/header.md") == HEADER_RULE.replace(
        "max_fixes: 2\n", "max_fixes: 2\n" + check
    )
    assert _text(root / "rules/docs/tone.md") == (
        f"---\n{check}---\nWrite plainly.\n\nNo jargon.\n"
    )
    assert "Promoted check `docs-lint`" in out
    assert "now runs once for each of these 2 rules" in out
    assert "Its configuration stays where it is: docs-lint.yaml" in out
    automation = RuleStore(root).load()
    assert automation.checks == {}
    assert set(automation.rules) == {other}
    groups = load_configuration(root / "ww.yaml").rule_groups
    assert all(rule.check is not None for rule in groups[0].rules)


def test_promote_refuses_what_it_cannot_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    inline = rule_text_hash("Keep the public CLI unchanged.")
    header = rule_text_hash(HEADER_RULE.split("---\n", 2)[2])
    _store(
        root,
        {inline: _converted("Keep the public CLI unchanged.", "cli")},
        {
            "cli": _check([inline]),
            "waiting": _check([header], status="proposed"),
            "gone": _check(["f" * 64]),
        },
    )

    assert _ww(root, "rules", "promote", "cli") == 1
    assert "covers develop/1, written in a step's own `rules` list" in (
        capsys.readouterr().err
    )
    assert _ww(root, "rules", "promote", "waiting") == 1
    assert "check 'waiting' is proposed" in capsys.readouterr().err
    assert _ww(root, "rules", "promote", "gone") == 1
    assert "no declared rule has the wording check 'gone' covers" in (
        capsys.readouterr().err
    )
    assert _ww(root, "rules", "promote", "missing") == 1
    assert "has no check 'missing'" in capsys.readouterr().err
    assert set(RuleStore(root).load().checks) == {"cli", "waiting", "gone"}
    assert _text(root / "rules/docs/header.md") == HEADER_RULE
