# SPDX-License-Identifier: GPL-3.0-or-later
"""The v1 configuration and command-line syntax parses as documented."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.actions import AssertionCondition, AssertionDefinition, Commands
from ww.cli import build_parser
from ww.config import load_configuration, parse_yaml_text
from ww.errors import ConfigurationError, StateError
from ww.operations import ChildWorkflowRun, WorkflowHandoff
from ww.plan import WorkflowPlanCompiler
from ww.project_config import load_project_config
from ww.rule_writes import check_mapping
from ww.variables import unavailable_ww_values
from ww.workflow_config import StepDefinition


def _workflow(step: str) -> str:
    return "workflows:\n  - name: task\n    steps:\n" + step


def _steps(text: str) -> tuple[StepDefinition, ...]:
    return parse_yaml_text(text).workflows_by_name["task"].steps


def test_assignment_takes_the_values_of_its_construct() -> None:
    (loop,) = _steps(
        _workflow(
            "      - name: fix\n        loop: [{work: Do it.}]\n"
            "        max_rounds: 4\n        assignment: per_step\n"
        )
    )
    (collect,) = _steps(
        _workflow("      - collect:\n        items:\n          assignment: per_item\n")
    )

    assert (loop.max_rounds, loop.loop_assignment) == (4, "per_step")
    assert collect.items is not None and collect.items.assignment == "per_item"
    with pytest.raises(ConfigurationError, match="must be one of: per_round, per_step"):
        parse_yaml_text(
            _workflow(
                "      - name: fix\n        loop: [{work: Do it.}]\n"
                "        assignment: per_item\n"
            )
        )
    with pytest.raises(ConfigurationError, match="goes beside a loop"):
        parse_yaml_text(
            _workflow("      - work: Work.\n        assignment: per_step\n")
        )


def test_children_assignment_is_per_step_and_per_child_is_reserved() -> None:
    children = (
        "      - split:\n        children:\n          steps:\n"
        "            - implement: {workflow: other}\n"
        "          assignment: {value}\n"
        "  - name: other\n    steps:\n      - work: Work.\n"
    )

    parse_yaml_text(_workflow(children.replace("{value}", "per_step")))
    with pytest.raises(ConfigurationError, match="per_child is reserved"):
        parse_yaml_text(_workflow(children.replace("{value}", "per_child")))
    with pytest.raises(ConfigurationError, match="without steps"):
        parse_yaml_text(
            _workflow(
                "      - split:\n        children:\n          workflow: other\n"
                "          assignment: per_step\n"
                "  - name: other\n    steps:\n      - work: Work.\n"
            )
        )


def test_item_phase_marks_the_stage_and_items_take_phase_guidance() -> None:
    (collect,) = _steps(
        _workflow(
            "      - collect:\n        items:\n          steps:\n"
            "            - fix: Fix it.\n              item_phase: resolve\n"
        )
    )
    (builtin,) = _steps(
        _workflow(
            "      - collect:\n        items:\n          analyze: Read the thread.\n"
            "          report: Reply in the thread.\n"
        )
    )

    assert collect.items is not None
    assert collect.items.steps[0].item_operation == "resolve_item"
    assert builtin.items is not None
    description = builtin.items.steps[0].description
    assert "When analyzing it: Read the thread." in description
    assert "When reporting the outcome: Reply in the thread." in description
    with pytest.raises(ConfigurationError, match="item_phase must be one of"):
        parse_yaml_text(_workflow("      - work: Work.\n        item_phase: fix\n"))


def test_interactive_page_is_the_operator_page_on_item_stages_only() -> None:
    (collect,) = _steps(
        _workflow(
            "      - collect:\n        items:\n          steps:\n"
            "            - answer: Answer it.\n              interactive: page\n"
        )
    )

    assert collect.items is not None
    stage = collect.items.steps[0]
    assert (stage.interactive, stage.ui) == (True, True)
    with pytest.raises(ConfigurationError, match="per-item stages only"):
        parse_yaml_text(_workflow("      - talk: Talk.\n        interactive: page\n"))
    with pytest.raises(ConfigurationError, match="true, false, or page"):
        parse_yaml_text(_workflow("      - talk: Talk.\n        interactive: chat\n"))


def test_kind_chooses_the_agent_action() -> None:
    (skill, prompt) = _steps(
        _workflow(
            "      - review-code:\n        kind: skill\n"
            "      - name: write\n        kind: prompt\n        description: Write.\n"
        )
    )

    assert skill.action is not None and skill.action.identifier == "skill"
    assert prompt.action is not None and prompt.action.identifier == "prompt"
    with pytest.raises(ConfigurationError, match="kind must be one of"):
        parse_yaml_text(_workflow("      - work:\n        kind: mcp\n"))
    with pytest.raises(ConfigurationError, match="cannot combine a command and kind"):
        parse_yaml_text(
            _workflow("      - work:\n        kind: skill\n        argv: [make]\n")
        )


def test_handoff_to_is_the_transition_and_workflow_runs_a_child() -> None:
    configuration = parse_yaml_text(
        _workflow(
            "      - split:\n        children:\n          steps:\n"
            "            - refine: Refine it.\n"
            "            - implement: {workflow: other}\n"
            "      - go:\n        handoff_to: other\n"
            "  - name: other\n    steps:\n      - work: Work.\n"
        )
    )
    split, go = configuration.workflows_by_name["task"].steps

    assert isinstance(go.operation, WorkflowHandoff)
    assert go.operation.target == "other"
    assert split.children is not None
    assert isinstance(split.children.steps[1].operation, ChildWorkflowRun)
    with pytest.raises(ConfigurationError, match="carries handoff_to"):
        parse_yaml_text(
            _workflow(
                "      - split:\n        children:\n          steps:\n"
                "            - implement: {workflow: other}\n"
                "            - go:\n              handoff_to: other\n"
                "  - name: other\n    steps:\n      - work: Work.\n"
            )
        )


def test_assert_is_a_list_of_conditions_that_must_all_hold() -> None:
    (step,) = _steps(
        _workflow(
            "      - check:\n        argv: [git, rev-parse, HEAD]\n"
            "        assert: [{equals: clean}]\n"
        )
    )
    assert step.action is not None
    payload = step.action.payload
    assert isinstance(payload, Commands)
    assert payload.assertion == AssertionDefinition(
        (AssertionCondition("equals", "clean"),)
    )
    both = AssertionDefinition(
        (AssertionCondition("empty"), AssertionCondition("equals", ""))
    )
    assert both.holds("")
    assert not both.holds("x")
    assert both.to_data() == ["empty", {"equals": ""}]
    assert both.describe() == "Command output must be empty and equal to ``."


def test_rules_add_assert_is_repeatable() -> None:
    mapping = check_mapping("make lint", None, ("empty", "equals:ok"))

    assert mapping == {"shell": "make lint", "assert": ["empty", {"equals": "ok"}]}
    with pytest.raises(StateError, match="takes empty or equals:<value>"):
        check_mapping("make lint", None, ("eq:ok",))


def test_saves_names_prefixed_paths() -> None:
    (step,) = _steps(
        "documents:\n  - plan: The plan.\n"
        + _workflow(
            "      - work: Work.\n        saves:\n"
            "          - metadata.jira.issue_id: The issue key.\n"
            "          - project_metadata.labels: Labels.\n"
            "            append: true\n"
            "          - documents.plan: Keep it current.\n"
            "          - item.field.reply_id: The reply.\n"
        )
    )

    task, project = step.save_metadata
    assert (task.name, task.key, task.scope, task.append) == (
        "jira.issue_id",
        "jira.issue_id",
        "task",
        False,
    )
    assert (project.name, project.key, project.scope, project.append) == (
        "project_metadata.labels",
        "labels",
        "project",
        True,
    )
    assert [(update.name, update.instruction) for update in step.update_document] == [
        ("plan", "Keep it current.")
    ]
    assert [field.name for field in step.update_item] == ["reply_id"]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("ww.metadata.x: X.", "saves take no ww. prefix"),
        ("x: X.", "must name metadata.<path>"),
        ("documents.plan: P.\n            append: true", "append applies to metadata"),
        ("metadata.a: A.\n          - metadata.a.b: B.", "cannot overlap"),
    ],
)
def test_saves_rejects_what_it_cannot_save(entry: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        parse_yaml_text(
            "documents:\n  - plan: The plan.\n"
            + _workflow(f"      - work: Work.\n        saves:\n          - {entry}\n")
        )


def test_variables_take_descriptions_or_bare_returned_names() -> None:
    (step,) = _steps(
        _workflow(
            "      - work: Work.\n        variables:\n"
            "          - workflow: Which workflow.\n"
            "          - branch\n"
        )
    )

    assert [(value.name, value.description) for value in step.provide] == [
        ("workflow", "Which workflow.")
    ]
    assert step.outputs == ("branch",)
    with pytest.raises(ConfigurationError, match="reserved"):
        parse_yaml_text(
            _workflow("      - work: Work.\n        variables: [{ww.task.x: X}]\n")
        )


def test_ww_values_compile_in_descriptions(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        "documents:\n  - plan: The plan.\n    path: plans/{{ww.task.id}}.md\n"
        + _workflow(
            "      - work: >-\n          {{ww.task.id}} in {{ww.task.workflows}}: read"
            " {{ww.documents.plan}} and {{ww.metadata.key}}.\n"
        ),
        encoding="utf-8",
    )

    plan = WorkflowPlanCompiler(
        load_configuration(path), tmp_path, "codex", "T-1"
    ).compile("task")

    work = next(item for item in plan.items if item.name == "work")
    # Compile-time values are bound now; the rest resolve when the page is built.
    assert work.description == (
        "T-1 in task,catchall: read {{ww.documents.plan}} and {{ww.metadata.key}}."
    )
    assert plan.documents[0].path == "plans/{{ww.task.id}}.md"


def test_ww_executable_is_left_for_the_page_and_names_the_printed_command(
    tmp_path: Path,
) -> None:
    from ww.executable import printed_executable
    from ww.variables import EXECUTABLE, runtime_variable_values

    path = tmp_path / "ww.yaml"
    path.write_text(
        _workflow("      - work: Run `{{ww.executable}} discover`.\n"),
        encoding="utf-8",
    )

    plan = WorkflowPlanCompiler(
        load_configuration(path), tmp_path, "codex", "T-1"
    ).compile("task")

    work = next(item for item in plan.items if item.name == "work")
    assert work.description == "Run `{{ww.executable}} discover`."
    assert runtime_variable_values(tmp_path, "T-1")[EXECUTABLE] == "./ww"
    with printed_executable("my ww"):
        assert runtime_variable_values(tmp_path, "T-1")[EXECUTABLE] == "'my ww'"


def test_unavailable_values_leave_ww_own_values_out() -> None:
    names = (
        "ww.task.workflows",
        "ww.metadata.key",
        "ww.item.text",
        "ww.documents.plan",
        "ww.git.branch",
        "ww.child.git.branch",
    )

    assert unavailable_ww_values(names, {}) == ("ww.git.branch", "ww.child.git.branch")


def _settings(tmp_path: Path, settings: dict[str, object]) -> Path:
    path = tmp_path / "ww.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    return path


def test_json_limits_are_read(tmp_path: Path) -> None:
    settings = _settings(tmp_path, {"limits": {"rounds": 4}})
    assert load_project_config(settings).limits.rounds == 4


def test_task_format_placeholders_take_double_braces(tmp_path: Path) -> None:
    config = load_project_config(_settings(tmp_path, {"task_format": "T-{{digit}}"}))

    assert config.task_format == "T-{{digit}}"
    with pytest.raises(ConfigurationError, match="has invalid placeholders"):
        load_project_config(_settings(tmp_path, {"task_format": "T-{digit}"}))


def test_the_flags_parse() -> None:
    parser = build_parser()

    start = parser.parse_args(
        ["start", "-w", "task", "-a", "codex", "--requirements", "Do it.",
         "--branch-strategy", "hotfix"]
    )
    interact = parser.parse_args(
        ["interact", "T-1", "--operator-said", "yes", "--agent-said", "ok", "--end"]
    )
    child = parser.parse_args(["start-child", "T-1", "a"])
    forced = parser.parse_args(["next", "T-1", "--force", "--reason", "Skip."])

    assert (start.requirements, start.branch_naming_strategy) == ("Do it.", "hotfix")
    assert (interact.operator, interact.agent, interact.end_interaction) == (
        "yes",
        "ok",
        True,
    )
    assert (child.parent_task_id, child.child_id) == ("T-1", "a")
    assert forced.force_reason == "Skip."
