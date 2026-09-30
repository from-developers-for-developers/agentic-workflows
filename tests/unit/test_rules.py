# SPDX-License-Identifier: GPL-3.0-or-later
"""Rules: rule files, the root mapping, step entries, hooks, and compilation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ww.actions import (
    AssertionDefinition,
    CommandDefinition,
    CommandOutcome,
    Commands,
)
from ww.actions.command import CommandAction
from ww.config import load_configuration, parse_yaml_configuration, parse_yaml_text
from ww.config.composition import compose_configuration
from ww.config.rules import (
    looks_like_reference,
    parse_rule_file,
    rule_summary,
    rule_text_hash,
)
from ww.errors import ConfigurationError
from ww.execution_models import PLAN_SCHEMA_VERSION, PlanSnapshot
from ww.execution_models.plan_codec import _plan_from_dict
from ww.extensions import Extension, ExtensionRegistry, RuleGroupContribution
from ww.plan import PlanItem, WorkflowPlan, WorkflowPlanCompiler
from ww.project_config import ProjectConfig
from ww.workflow_config import (
    ALL,
    RuleDefinition,
    RuleGroup,
    RuleGroupRef,
    RuleHints,
    StepDefinition,
    WorkflowConfiguration,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _config(root: Path, yaml: str) -> Path:
    return _write(root / "ww-agentic-workflows.yaml", yaml)


def _rule(root: Path, relative: str, content: str) -> Path:
    return _write(root / relative, content)


def _step(
    configuration: WorkflowConfiguration, workflow: str = "task", index: int = 0
) -> StepDefinition:
    return configuration.workflows_by_name[workflow].steps[index]


def _groups(root: Path) -> dict[str, RuleGroup]:
    return load_configuration(root / "ww-agentic-workflows.yaml").rule_groups_by_name


def _compile(root: Path, workflow: str = "task", **kwargs: Any) -> WorkflowPlan:
    configuration = load_configuration(root / "ww-agentic-workflows.yaml")
    return WorkflowPlanCompiler(
        configuration, root, "codex", "TASK-1", **kwargs
    ).compile(workflow)


def _item(plan: WorkflowPlan, step: str, phase: str = "step") -> PlanItem:
    return next(
        item for item in plan.items if item.step == step and item.phase == phase
    )


# Rule files ---------------------------------------------------------------


def test_rule_file_frontmatter_sets_paths_check_fixes_and_hints(tmp_path: Path) -> None:
    path = _rule(
        tmp_path,
        "rules/services.md",
        """---
paths: ["src/**/*.php"]
check:
  shell: find $WW_STEP_CHANGED_FILES -name '*Service.php'
  assert: { operator: empty }
max_fixes: 5
model: big
---
Put every `*Service.php` under `src/Service/`, one class per file.

Controllers must not instantiate services.
""",
    )

    rule = parse_rule_file(path, "php/services", RuleHints(agent="codex"))

    assert rule.id == "php/services"
    assert rule.text.startswith("Put every")
    assert rule.text.endswith("instantiate services.")
    assert rule.summary == (
        "Put every `*Service.php` under `src/Service/`, one class per file."
    )
    assert rule.paths == ("src/**/*.php",)
    assert rule.check is not None
    assert rule.check.commands[0].shell is not None
    assert rule.check.assertion == AssertionDefinition("empty")
    assert rule.max_fixes == 5
    assert rule.hints == RuleHints(agent="codex", model="big")
    assert rule.source == str(path)


def test_rule_file_without_frontmatter_is_all_body(tmp_path: Path) -> None:
    rule = parse_rule_file(
        _rule(tmp_path, "rule.md", "Keep it short.\n"), "group/rule"
    )

    assert rule.text == "Keep it short."
    assert rule.paths == ()
    assert rule.check is None
    assert rule.max_fixes is None


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("---\npaths: [x]\n---\n\n", "has no rule text"),
        ("", "has no rule text"),
        ("---\nscope: all\n---\nText.\n", "unknown key"),
        ("---\npaths: [x]\nText.\n", "does not close its frontmatter"),
        ("---\npaths: []\n---\nText.\n", "paths must be a non-empty list"),
        ("---\nmax_fixes: 0\n---\nText.\n", "max_fixes must be a positive integer"),
        (
            "---\ncheck:\n  argv: [true]\n  idempotent: true\n---\nText.\n",
            "idempotent is not allowed on a check",
        ),
        (
            "---\ncheck:\n  command: {argv: [true]}\n---\nText.\n",
            "command is not allowed on a check",
        ),
        (
            "---\ncheck:\n  assert: {operator: empty}\n---\nText.\n",
            "require argv or shell",
        ),
        ("---\n- a list\n---\nText.\n", "must be a mapping"),
        ("---\nagent: auto\n---\nText.\n", "must not be 'auto'"),
    ],
)
def test_rule_file_errors_name_the_file(
    tmp_path: Path, content: str, message: str
) -> None:
    path = _rule(tmp_path, "bad.md", content)

    with pytest.raises(ConfigurationError, match=message) as raised:
        parse_rule_file(path, "group/bad")

    assert "bad.md" in str(raised.value)


def test_rule_check_accepts_argv_with_an_eq_assertion(tmp_path: Path) -> None:
    rule = parse_rule_file(
        _rule(
            tmp_path,
            "rule.md",
            "---\ncheck:\n  argv: [echo, ok]\n  assert: {operator: eq, expected: ok}\n"
            "---\nSay ok.\n",
        ),
        "group/rule",
    )

    assert rule.check is not None
    assert rule.check.commands[0].argv == ("echo", "ok")
    assert rule.check.assertion == AssertionDefinition("eq", "ok")


@pytest.mark.parametrize(
    ("text", "summary"),
    [
        ("One sentence. And another.", "One sentence."),
        ("Stop! Then go.", "Stop!"),
        ("Is it? Yes.", "Is it?"),
        ("Use v1.2 of the API. Always.", "Use v1.2 of the API."),
        ("A heading line\nwith no end", "A heading line"),
        ("Spans\ntwo lines. Next.", "Spans two lines."),
    ],
)
def test_summary_is_the_first_sentence_or_first_line(text: str, summary: str) -> None:
    assert rule_summary(text) == summary


def test_text_hash_ignores_whitespace_differences() -> None:
    assert rule_text_hash("  Keep  it\n short. ") == rule_text_hash("Keep it short.")
    assert rule_text_hash("Keep it short.") != rule_text_hash("Keep it shorter.")


@pytest.mark.parametrize(
    ("value", "reference"),
    [
        ("php-architecture", True),
        ("rules/php", True),
        ("no-migrations.md", True),
        ("Keep the public CLI unchanged.", False),
        ("tests.", False),
        ("a/b/c", False),
        ("two words", False),
    ],
)
def test_reference_shape(value: str, reference: bool) -> None:
    assert looks_like_reference(value) is reference


# The root mapping ---------------------------------------------------------

_WORKFLOWS = """workflows:
  - name: task
    steps:
      - develop: Develop.
      - review: Review.
  - name: bugfix
    steps:
      - develop: Fix.
"""


def test_root_groups_resolve_directories_files_and_nested_groups(
    tmp_path: Path,
) -> None:
    _rule(tmp_path, "rules/php/b-second.md", "Second.")
    _rule(tmp_path, "rules/php/a-first.md", "First.")
    _rule(tmp_path, "rules/php/notes.txt", "Not a rule.")
    _rule(tmp_path, "rules/docs.md", "Docs.")
    _config(
        tmp_path,
        """rules:
  php: [rules/php/]
  docs:
    rules: [rules/docs.md]
    workflows: [task]
    steps: [review]
    reasoning: high
  engineering: [php, docs]
"""
        + _WORKFLOWS,
    )

    groups = _groups(tmp_path)

    assert [rule.id for rule in groups["php"].rules] == ["php/a-first", "php/b-second"]
    assert groups["php"].workflows == ALL and groups["php"].steps == ALL
    docs = groups["docs"]
    assert [rule.id for rule in docs.rules] == ["docs/docs"]
    assert docs.workflows.names == ("task",) and docs.steps.names == ("review",)
    assert docs.rules[0].hints == RuleHints(reasoning="high")
    assert [rule.id for rule in groups["engineering"].rules] == [
        "php/a-first",
        "php/b-second",
        "docs/docs",
    ]


def test_a_file_listed_in_two_groups_takes_each_groups_id(tmp_path: Path) -> None:
    _rule(tmp_path, "rules/shared.md", "Shared.")
    _config(
        tmp_path,
        "rules:\n  one: [rules/shared.md]\n  two: [rules/shared.md]\n" + _WORKFLOWS,
    )

    groups = _groups(tmp_path)

    assert groups["one"].rules[0].id == "one/shared"
    assert groups["two"].rules[0].id == "two/shared"
    assert groups["one"].rules[0].text_hash == groups["two"].rules[0].text_hash


def test_group_filters_take_star_a_list_or_nothing(tmp_path: Path) -> None:
    _rule(tmp_path, "rules/one.md", "One.")
    _config(
        tmp_path,
        """rules:
  starred: {rules: [rules/one.md], workflows: "*", steps: "*"}
  listed: {rules: [rules/one.md], workflows: [bugfix], steps: [develop]}
  named-only: {rules: [rules/one.md], steps: []}
"""
        + _WORKFLOWS,
    )

    groups = _groups(tmp_path)

    starred = groups["starred"]
    assert starred.workflows == ALL and starred.steps == ALL
    assert starred.applies_to("task", "review", "review")
    listed = groups["listed"]
    assert listed.applies_to("bugfix", "develop", "develop")
    assert not listed.applies_to("task", "develop", "develop")
    named_only = groups["named-only"]
    assert named_only.workflows == ALL and named_only.steps.admits_none
    assert not named_only.applies_to("task", "develop", "develop")


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ("  php: [rules/missing/]\n", "item 'rules/missing/' names no group or file"),
        ("  a: [b]\n  b: [a]\n", "rule group cycle"),
        ("  php: []\n", "rules must be a non-empty list"),
        ("  php: rules/\n", "must be a list of items or a mapping"),
        ("  php: {rules: [rules/one.md], scope: all}\n", "unknown key"),
        ("  Bad Name: [rules/one.md]\n", "normalized group names"),
        ("  php: {rules: [rules/one.md], workflows: [nope]}\n", "unknown workflow"),
        ("  php: {rules: [rules/one.md], steps: [nope]}\n", "unknown step"),
        (
            '  php: {rules: [rules/one.md], workflows: ["*", task]}\n',
            r'workflows cannot mix "\*" with names',
        ),
        (
            "  php: {rules: [rules/one.md], steps: develop}\n",
            r"steps must be \"\*\" or a list of names \(write \[develop\]",
        ),
    ],
)
def test_root_mapping_errors(tmp_path: Path, rules: str, message: str) -> None:
    _rule(tmp_path, "rules/one.md", "One.")
    _config(tmp_path, "rules:\n" + rules + _WORKFLOWS)

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(tmp_path / "ww-agentic-workflows.yaml")


def test_two_files_with_one_stem_in_a_group_are_an_error(tmp_path: Path) -> None:
    _rule(tmp_path, "a/style.md", "One.")
    _rule(tmp_path, "b/style.md", "Two.")
    _config(tmp_path, "rules:\n  docs: [a/, b/]\n" + _WORKFLOWS)

    with pytest.raises(ConfigurationError, match="two rules named 'style'"):
        load_configuration(tmp_path / "ww-agentic-workflows.yaml")


def test_an_absolute_rule_path_is_accepted_with_a_notice(tmp_path: Path) -> None:
    rule = _rule(tmp_path / "elsewhere", "rule.md", "Anywhere.")
    project = tmp_path / "project"
    _config(project, f"rules:\n  abs: [{rule}]\n" + _WORKFLOWS)

    configuration = load_configuration(project / "ww-agentic-workflows.yaml")
    notices = compose_configuration(project / "ww-agentic-workflows.yaml").notices

    assert configuration.rule_groups[0].rules[0].text == "Anywhere."
    assert any(
        f"rule path {rule}" in notice and "absolute" in notice for notice in notices
    )


def test_rule_groups_merge_by_name_across_levels(tmp_path: Path) -> None:
    _rule(tmp_path, "rules/repo.md", "Repo.")
    _rule(tmp_path, "rules/local.md", "Local.")
    _config(
        tmp_path,
        "rules:\n  docs: [rules/repo.md]\n  other: [rules/repo.md]\n" + _WORKFLOWS,
    )
    _write(
        tmp_path / "ww-agentic-workflows.local.yaml",
        "rules:\n  docs: [rules/local.md]\n",
    )

    composed = compose_configuration(tmp_path / "ww-agentic-workflows.yaml")
    groups = _groups(tmp_path)

    assert [rule.id for rule in groups["docs"].rules] == ["docs/local"]
    assert [rule.id for rule in groups["other"].rules] == ["other/repo"]
    assert any("rule group 'docs'" in notice for notice in composed.notices)


def test_an_imports_rule_paths_resolve_next_to_the_import(tmp_path: Path) -> None:
    _rule(tmp_path, "shared/rules/one.md", "From the import.")
    _write(
        tmp_path / "shared/rules.yaml",
        """rules:
  shared: [rules/one.md]
workflows:
  - name: imported
    steps:
      - name: work
        rules: [rules/one.md]
""",
    )
    _config(tmp_path, "imports: [shared/rules.yaml]\n" + _WORKFLOWS)

    configuration = load_configuration(tmp_path / "ww-agentic-workflows.yaml")

    assert configuration.rule_groups_by_name["shared"].rules[0].text == (
        "From the import."
    )
    work = configuration.workflows_by_name["imported"].steps[0]
    assert isinstance(work.rules[0], RuleDefinition)
    assert work.rules[0].text == "From the import."


# Step entries -------------------------------------------------------------


def test_step_entries_resolve_groups_paths_and_text(tmp_path: Path) -> None:
    _rule(tmp_path, "rules/php/one.md", "Group rule.")
    _rule(tmp_path, "rules/one-off/no-migrations.md", "No migrations.")
    _config(
        tmp_path,
        """rules:
  php:
    rules: [rules/php/]
    steps: []
workflows:
  - name: task
    steps:
      - name: develop
        rules:
          - Keep the public CLI unchanged.
          - text: Include "foo" in every file you change.
            shell: grep -L foo $WW_STEP_CHANGED_FILES
            assert: { operator: empty }
          - argv: [vendor/bin/phpstan, analyse]
          - rules/one-off/no-migrations.md
          - php
""",
    )

    rules = _step(load_configuration(tmp_path / "ww-agentic-workflows.yaml")).rules

    assert isinstance(rules[0], RuleDefinition)
    assert (rules[0].id, rules[0].text, rules[0].check) == (
        "develop/1",
        "Keep the public CLI unchanged.",
        None,
    )
    assert isinstance(rules[1], RuleDefinition)
    assert rules[1].id == "develop/2" and rules[1].check is not None
    assert isinstance(rules[2], RuleDefinition)
    assert rules[2].id == "develop/3"
    assert rules[2].summary == "vendor/bin/phpstan analyse"
    assert isinstance(rules[3], RuleDefinition)
    assert (rules[3].id, rules[3].text) == ("develop/no-migrations", "No migrations.")
    assert rules[4] == RuleGroupRef("php")


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            "- missing-group",
            "step 'develop' rule 'missing-group' names no group or file",
        ),
        ("- rules/typo", "names no group or file"),
        ("- {max_fixes: 2}", "requires text or a command"),
        ("- {text: Do it., when: always}", "unknown key"),
        ("- {text: ''}", "text must be a non-empty string"),
        ("- {text: Do it., assert: {operator: empty}}", "require argv or shell"),
        ("- 3", "must be a string or a mapping"),
    ],
)
def test_step_entry_errors(tmp_path: Path, entry: str, message: str) -> None:
    _config(
        tmp_path,
        "workflows:\n  - name: task\n    steps:\n      - name: develop\n"
        f"        rules:\n          {entry}\n",
    )

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(tmp_path / "ww-agentic-workflows.yaml")


def test_rules_on_a_step_without_agent_work_are_an_error(tmp_path: Path) -> None:
    _config(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: build
        argv: [make]
        rules: [Build cleanly.]
""",
    )

    with pytest.raises(ConfigurationError, match="no agent work of its own"):
        _compile(tmp_path)


def test_a_step_referencing_a_handler_step_inherits_its_rules(tmp_path: Path) -> None:
    _config(
        tmp_path,
        """handlers:
  - name: review
    description: Review it.
    rules: [Be kind.]
workflows:
  - name: task
    steps:
      - name: check
        handler: review
""",
    )

    rules = _step(load_configuration(tmp_path / "ww-agentic-workflows.yaml")).rules

    assert [rule.text for rule in rules if isinstance(rule, RuleDefinition)] == [
        "Be kind."
    ]


# assert: empty ------------------------------------------------------------


def test_assert_empty_parses_and_holds_only_on_blank_output(tmp_path: Path) -> None:
    _config(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: probe
        argv: [echo]
        assert: { operator: empty }
""",
    )

    assertion = _step(load_configuration(tmp_path / "ww-agentic-workflows.yaml")).action
    assert assertion is not None
    assert assertion.payload.assertion == AssertionDefinition("empty")
    assert AssertionDefinition("empty").holds(" \n")
    assert not AssertionDefinition("empty").holds("x")
    decoded = CommandAction().decode(CommandAction().encode(assertion.payload))
    assert decoded.assertion == AssertionDefinition("empty")


@pytest.mark.parametrize(
    ("assertion", "message"),
    [
        ("{operator: empty, expected: x}", "expected is not allowed"),
        ("{operator: eq}", "expected"),
        ("{operator: contains, expected: x}", "must be eq or empty"),
    ],
)
def test_assert_operator_errors(tmp_path: Path, assertion: str, message: str) -> None:
    _config(
        tmp_path,
        "workflows:\n  - name: task\n    steps:\n      - name: probe\n"
        f"        argv: [echo]\n        assert: {assertion}\n",
    )

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(tmp_path / "ww-agentic-workflows.yaml")


def test_an_empty_assertion_fails_a_handler_that_prints() -> None:
    class _Commands:
        def completed(self, segment: int) -> CommandOutcome:
            return CommandOutcome(True, "left over\n", "", 0)

    class _Context:
        commands = _Commands()
        runtime_values: dict[str, str] = {}

    planned = Commands(
        (CommandDefinition(argv=("echo",)),), AssertionDefinition("empty")
    )

    result = CommandAction().execute(planned, _Context())  # type: ignore[arg-type]

    assert not result.ok
    assert "expected no output" in result.error


# Hooks with on_failure ----------------------------------------------------


def _hooks(tmp_path: Path, hooks: str) -> Path:
    return _config(
        tmp_path,
        f"""workflows:
  - name: task
    hooks:
{hooks}
    steps:
      - develop: Develop.
""",
    )


def test_on_failure_is_parsed_on_hooks_and_handler_members(tmp_path: Path) -> None:
    _hooks(
        tmp_path,
        """      before_complete:
        - argv: [pytest]
          on_failure: fix
        - handlers:
            - argv: [ruff]
            - argv: [mypy]
              on_failure: operator
          on_failure: fix
        - argv: ["true"]
""",
    )

    configuration = load_configuration(tmp_path / "ww-agentic-workflows.yaml")
    hooks = configuration.workflows[0].hooks

    assert [hook.on_failure for hook in hooks] == ["fix", "fix", "operator", "operator"]


@pytest.mark.parametrize(
    ("hooks", "message"),
    [
        (
            "      after_complete:\n        - argv: [pytest]\n"
            "          on_failure: fix\n",
            "only valid on before_complete hooks",
        ),
        (
            "      before_complete:\n        - argv: [pytest]\n"
            "          on_failure: skip\n",
            "on_failure must be one of: fix, operator",
        ),
    ],
)
def test_on_failure_errors(tmp_path: Path, hooks: str, message: str) -> None:
    _hooks(tmp_path, hooks)

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(tmp_path / "ww-agentic-workflows.yaml")


def test_on_failure_fix_is_not_valid_on_a_workflow_transition(tmp_path: Path) -> None:
    _config(
        tmp_path,
        """workflows:
  - name: other
    steps:
      - work: Work.
  - name: task
    handoff: true
    steps:
      - name: develop
        description: Develop.
        hooks:
          before_complete:
            - workflow: other
              on_failure: fix
""",
    )

    with pytest.raises(ConfigurationError, match="not valid on a workflow transition"):
        load_configuration(tmp_path / "ww-agentic-workflows.yaml")


def test_a_fix_hook_must_run_a_command(tmp_path: Path) -> None:
    _hooks(
        tmp_path,
        "      before_complete:\n        - name: think\n          prompt: true\n"
        "          on_failure: fix\n",
    )

    with pytest.raises(ConfigurationError, match="requires a command handler"):
        _compile(tmp_path)


# Extensions ---------------------------------------------------------------


def _extension_registry(
    root: Path, rules: tuple[RuleGroupContribution, ...]
) -> ExtensionRegistry:
    return ExtensionRegistry(
        root,
        (Extension("acme", "rules", rules=rules),),
        ProjectConfig(extensions={"acme/rules": {}}),
    )


def test_a_configured_extension_contributes_rule_groups(tmp_path: Path) -> None:
    shipped = _rule(tmp_path / "ext", "rules/typed.md", "Type everything.")
    _config(
        tmp_path,
        """rules:
  local: [acme-python]
workflows:
  - name: task
    steps:
      - name: develop
        rules: [acme-python]
""",
    )
    extensions = _extension_registry(
        tmp_path, (RuleGroupContribution("acme-python", (shipped.parent,), steps=()),)
    )

    configuration = load_configuration(
        tmp_path / "ww-agentic-workflows.yaml", extensions
    )

    groups = configuration.rule_groups_by_name
    names = [group.name for group in configuration.rule_groups]
    assert names == ["acme-python", "local"]
    assert groups["acme-python"].origin == "extension acme/rules"
    assert groups["acme-python"].steps.admits_none
    assert groups["local"].rules[0].id == "acme-python/typed"
    assert _step(configuration).rules == (RuleGroupRef("acme-python"),)


def test_an_unconfigured_extension_contributes_nothing(tmp_path: Path) -> None:
    shipped = _rule(tmp_path / "ext", "rules/typed.md", "Type everything.")
    _config(tmp_path, _WORKFLOWS)
    extensions = ExtensionRegistry(
        tmp_path,
        (
            Extension(
                "acme",
                "rules",
                rules=(RuleGroupContribution("acme-python", (shipped,)),),
            ),
        ),
        ProjectConfig(),
    )

    configuration = load_configuration(
        tmp_path / "ww-agentic-workflows.yaml", extensions
    )

    assert configuration.rule_groups == ()


def test_a_group_name_in_both_yaml_and_an_extension_is_an_error(tmp_path: Path) -> None:
    shipped = _rule(tmp_path / "ext", "rules/typed.md", "Type everything.")
    _rule(tmp_path, "rules/mine.md", "Mine.")
    _config(tmp_path, "rules:\n  python: [rules/mine.md]\n" + _WORKFLOWS)
    extensions = _extension_registry(
        tmp_path, (RuleGroupContribution("python", (shipped,)),)
    )

    with pytest.raises(ConfigurationError, match="shipped by extension acme/rules"):
        load_configuration(tmp_path / "ww-agentic-workflows.yaml", extensions)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "Bad Name", "items": (Path("/x"),)}, "normalized name"),
        ({"name": "g", "items": ()}, "non-empty tuple"),
        ({"name": "g", "items": (Path("relative"),)}, "must be absolute"),
        ({"name": "g", "items": ("two words",)}, "absolute paths or group names"),
    ],
)
def test_rule_group_contribution_validates_itself(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        RuleGroupContribution(**kwargs)


# Compilation --------------------------------------------------------------


def test_groups_apply_where_their_filters_admit_and_dedupe_by_id(
    tmp_path: Path,
) -> None:
    _rule(tmp_path, "rules/all/everywhere.md", "Everywhere.")
    _rule(
        tmp_path,
        "rules/review/careful.md",
        '---\ncheck:\n  argv: ["true"]\nmax_fixes: 7\n---\nBe careful.',
    )
    _config(
        tmp_path,
        """rules:
  all: [rules/all/]
  review:
    rules: [rules/review/]
    workflows: [task]
    steps: [review]
  both: [all]
workflows:
  - name: task
    steps:
      - develop: Develop.
      - name: review
        description: Review.
        rules:
          - all
          - Be brief.
  - name: bugfix
    steps:
      - review: Review the fix.
""",
    )

    plan = _compile(tmp_path, project_config=ProjectConfig(max_fixes=4))
    develop, review = _item(plan, "develop"), _item(plan, "review")
    bugfix_review = _item(_compile(tmp_path, "bugfix"), "review")

    assert [rule.id for rule in develop.rules] == ["all/everywhere"]
    assert [rule.id for rule in review.rules] == [
        "all/everywhere",
        "review/careful",
        "review/2",
    ]
    assert [rule.id for rule in bugfix_review.rules] == ["all/everywhere"]
    assert [(check.id, check.source, check.max_fixes) for check in review.checks] == [
        ("review/careful", "rule", 7)
    ]
    assert review.rules[1].has_command and review.rules[1].max_fixes == 7
    assert review.rules[2].max_fixes == 4
    assert develop.checks == ()


def test_a_named_group_applies_regardless_of_its_filters(tmp_path: Path) -> None:
    _rule(tmp_path, "rules/php/one.md", "PHP rule.")
    _config(
        tmp_path,
        """rules:
  php:
    rules: [rules/php/]
    steps: []
workflows:
  - name: task
    steps:
      - develop: Develop.
      - name: refactor
        description: Refactor.
        rules: [php]
""",
    )

    plan = _compile(tmp_path)

    assert _item(plan, "develop").rules == ()
    assert [rule.id for rule in _item(plan, "refactor").rules] == ["php/one"]


def test_init_hooks_and_the_workflow_summary_get_no_rules(tmp_path: Path) -> None:
    _rule(tmp_path, "rules/one.md", '---\ncheck:\n  argv: ["true"]\n---\nOne.')
    _config(
        tmp_path,
        """rules:
  all: [rules/]
workflows:
  - name: task
    hooks:
      after_complete:
        - name: note
          prompt: true
    steps:
      - develop: Develop.
""",
    )

    plan = _compile(tmp_path)

    carrying = [item.step for item in plan.items if item.rules or item.checks]
    assert carrying == ["develop"]


def test_fix_hooks_become_checks_and_operator_hooks_stay_hook_items(
    tmp_path: Path,
) -> None:
    _config(
        tmp_path,
        """hooks:
  before_complete:
    - argv: [ruff, check]
      steps: [develop]
      on_failure: fix
workflows:
  - name: task
    hooks:
      before_complete:
        - argv: [pytest]
          on_failure: fix
        - name: record
          argv: [echo, recorded]
    steps:
      - name: develop
        description: Develop.
        rules:
          - text: No TODOs.
            shell: grep -l TODO $WW_STEP_CHANGED_FILES
            assert: { operator: empty }
        hooks:
          before_complete:
            - argv: [pytest, -q]
              on_failure: fix
""",
    )

    plan = _compile(tmp_path, project_config=ProjectConfig(max_fixes=2))
    develop = _item(plan, "develop")
    hooks = [
        item.name
        for item in plan.items
        if item.step == "develop" and item.phase == "before_complete"
    ]

    assert [(check.id, check.source) for check in develop.checks] == [
        ("develop/1", "rule"),
        ("develop/ruff", "hook"),
        ("develop/pytest", "hook"),
        ("develop/pytest-2", "hook"),
    ]
    assert all(check.max_fixes == 2 for check in develop.checks)
    assert hooks == ["record"]
    # The init step keeps its fix hook as an ordinary hook: it has no checks.
    init_hooks = [
        item.name
        for item in plan.items
        if item.step == "init" and item.phase == "before_complete"
    ]
    assert init_hooks == ["inline-argv", "record"]


def test_a_rule_check_is_planned_like_a_cli_handler(tmp_path: Path) -> None:
    _config(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: develop
        description: Develop.
        rules:
          - text: Name the task.
            argv: [grep, "{{__task_id}}", NOTES.md]
          - text: Use a known value.
            argv: [echo, "{{unknown}}"]
""",
    )

    with pytest.raises(ConfigurationError, match="check 'develop/2'.*unknown"):
        _compile(tmp_path)


def test_rules_and_checks_round_trip_through_the_plan_codec(tmp_path: Path) -> None:
    _rule(
        tmp_path,
        "rules/one.md",
        "---\npaths: ['*.py']\nagent: claudecode\ncheck:\n  shell: 'true'\n"
        "  assert: {operator: empty}\n---\nOne.",
    )
    _config(
        tmp_path,
        """rules:
  all: [rules/]
workflows:
  - name: task
    steps:
      - name: develop
        description: Develop.
        rules: [Judged.]
        hooks:
          before_complete:
            - argv: [pytest]
              on_failure: fix
""",
    )
    plan = _compile(tmp_path)

    restored = _plan_from_dict(plan.to_dict())

    assert restored.to_dict() == plan.to_dict()
    item = _item(restored, "develop")
    assert item.rules[0].hints == RuleHints(agent="claudecode")
    assert item.rules[0].paths == ("*.py",)
    # The plan freezes where each rule comes from: its file, relative to the
    # root, or nothing for a rule written in the step's YAML.
    assert [rule.source for rule in item.rules] == ["rules/one.md", None]
    assert item.checks[0].command.assertion == AssertionDefinition("empty")
    snapshot = PlanSnapshot(PLAN_SCHEMA_VERSION, "test", "digest", "now", plan)
    assert PlanSnapshot.from_dict(snapshot.to_dict()).plan.to_dict() == plan.to_dict()
    assert PLAN_SCHEMA_VERSION == 16


def test_a_plan_without_rules_serializes_as_before(tmp_path: Path) -> None:
    _config(tmp_path, _WORKFLOWS)

    data = _compile(tmp_path).to_dict()

    assert all("rules" not in item and "checks" not in item for item in data["items"])


def test_rule_groups_filtered_to_a_workflow_also_apply_to_its_heirs(
    tmp_path: Path,
) -> None:
    _rule(tmp_path, "rules/one.md", "One.")
    _config(
        tmp_path,
        """rules:
  task-only:
    rules: [rules/one.md]
    workflows: [task]
workflows:
  - name: task
    steps:
      - develop: Develop.
  - name: hotfix
    inherit: task
""",
    )

    plan = _compile(tmp_path, "hotfix")

    assert [rule.id for rule in _item(plan, "develop").rules] == ["task-only/one"]


def test_parse_without_a_file_resolves_paths_against_the_given_base(
    tmp_path: Path,
) -> None:
    _rule(tmp_path, "rules/one.md", "One.")

    configuration = parse_yaml_text(
        "rules:\n  all: [rules/]\n" + _WORKFLOWS, base=tmp_path
    )

    assert configuration.rule_groups[0].rules[0].id == "all/one"


def test_the_yaml_loader_reads_extension_groups_itself(tmp_path: Path) -> None:
    shipped = _rule(tmp_path / "ext", "rules/typed.md", "Type everything.")
    _config(tmp_path, _WORKFLOWS)
    extensions = _extension_registry(
        tmp_path, (RuleGroupContribution("acme-python", (shipped,)),)
    )

    configuration = parse_yaml_configuration(
        tmp_path / "ww-agentic-workflows.yaml", extensions
    )

    assert configuration.rule_groups[0].name == "acme-python"
