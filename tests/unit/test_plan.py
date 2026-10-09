# SPDX-License-Identifier: GPL-3.0-or-later
"""Compiling a workflow configuration into an ordered plan of items."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.plan_helpers import plan_item
from ww.actions import (
    CommandDefinition,
    Commands,
    Extension,
    Mcp,
    PlannedAction,
    Prompt,
    Skill,
)
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.execution_models import PLAN_SCHEMA_VERSION, PlanSnapshot
from ww.extensions import ExtensionRegistry
from ww.output import render_plan
from ww.plan import (
    ChildWorkflowRun,
    PlanCompilationOptions,
    WorkflowHandoff,
    WorkflowPlan,
    WorkflowPlanCompiler,
    compile_workflow_plan,
    number_step_paths,
    step_label,
)
from ww.plan.constructs import (
    ExpansionResult,
    PlanningContext,
    builtin_construct_planners,
    normalize_construct,
)
from ww.workflow_config import INIT_STEP_PROMPT, ProvidedVariable, StepDefinition


@dataclass(frozen=True)
class _TestSequenceDefinition:
    step: StepDefinition
    body: tuple[StepDefinition, ...]


class _TestSequencePlanner:
    """Test-only planner proving registry dispatch uses the shared lifecycle."""

    def expand(
        self, definition: _TestSequenceDefinition, context: PlanningContext
    ) -> ExpansionResult:
        scope = context.derive_scope(
            parent=context.scope.path,
            parent_ancestors=context.scope.ancestors,
        )
        return ExpansionResult(
            available_values=context.compile_steps(definition.body, scope)
        )


def test_every_workflow_starts_with_the_implicit_init_step(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """hooks:
  before_complete:
    - steps: [init]
      argv: [printf, saved]
workflows:
  - name: task
    steps:
      - name: work
        artifact_from: init
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    init, hook, work, summary = plan.items
    assert init.name == init.step == "init"
    assert init.payload_as(Prompt).text == INIT_STEP_PROMPT
    assert init.artifact
    assert hook.phase == "before_complete" and hook.step == "init"
    assert work.artifact_dependency == "init"
    assert summary.summary


def test_depends_on_resolves_the_nearest_earlier_upper_level_step(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: plan
      - name: group
        steps:
          - name: plan
          - name: inner
            steps:
              - name: fix
                artifact_from: plan
      - name: review
        steps:
          - name: check
            artifact_from: plan
          - name: decide
            artifact_from: check

      - assess:
          question: Is it good?
          outcomes:
            positive:
              steps:
                - name: ship
                  artifact_from: assess
            negative:
              steps:
                - name: redo
                  artifact_from: review
      - name: triage
        items:
          steps:
            - name: fix-item
              artifact_from: triage
            - name: report
              artifact_from: fix-item
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    dependencies = {
        item.step: item.artifact_dependency
        for item in plan.items
        if item.artifact_dependency is not None
    }
    assert dependencies == {
        "group/inner/fix": "group/plan",
        "review/check": "plan",
        "review/decide": "review/check",
        "assess/positive/ship": "assess",
        "assess/negative/redo": "review",
        "triage/{item}/fix-item": "triage",
        "triage/{item}/report": "triage/{item}/fix-item",
    }


def test_artifact_from_resolves_a_nested_group_to_its_path(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: prepare
      - name: outer
        steps:
          - name: prepare
            steps:
              - name: draft
          - name: build
            artifact_from: prepare
          - name: inner
            steps:
              - name: first
              - name: fix
                artifact_from: prepare
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    dependencies = {
        item.step: item.artifact_dependency
        for item in plan.items
        if item.artifact_dependency is not None
    }
    assert dependencies == {
        "outer/build": "outer/prepare",
        "outer/inner/fix": "outer/prepare",
    }


def test_extension_handler_can_be_used_as_a_step(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - task: ~
    steps:
      - ext/ww/git/handlers:is-git-clean: ~
""",
        encoding="utf-8",
    )
    extensions = ExtensionRegistry.discover(tmp_path)
    configuration = load_configuration(path, extensions)

    plan = compile_workflow_plan(
        configuration, tmp_path, "task", "codex", extensions=extensions
    )

    step = next(
        item
        for item in plan.items
        if item.phase == "step" and item.name == "is-git-clean"
    )
    assert step.kind == "extension"
    assert step.registered_handler == "ext/ww/git/handlers:is-git-clean"


def test_step_handler_compiles_as_its_own_single_step(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """handlers:
  - name: shared-check
    argv: [printf, ready]
workflows:
  - name: task
    steps:
      - some_step: Perform the check here.
        handler: shared-check
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    step = next(
        item for item in plan.items if item.phase == "step" and item.step == "some_step"
    )
    assert step.name == "some_step"
    assert step.description == "Perform the check here."
    assert step.kind == "cli"
    assert step.payload_as(Commands).commands[0].argv == ("printf", "ready")
    assert step.registered_handler is None


def _configuration(tmp_path: Path):
    path = tmp_path / "ww.yaml"
    path.write_text(
        """handlers:
  - name: clean
    argv: [git, status, --porcelain]
  - name: shared-skill
    kind: skill
  - name: shared-command
    kind: slash_command
hooks:
  before_start_workflow:
    - name: clean
  after_complete:
    - workflows: [task]
      steps: [develop]
      description: Did architecture change?
      name: shared-skill
workflows:
  - name: task
    hooks:
      before_start:
        - steps: [develop]
          argv: [echo, beginning]
    steps:
      - name: setup
        description: Gather constraints.
        variables:
          - name: workflow
      - name: develop
        hooks:
          before_complete:
            - name: shared-command
          after_complete:
            - handoff_to: "{{workflow}}"
""",
        encoding="utf-8",
    )
    skill = tmp_path / ".agents/skills/shared-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    command = tmp_path / ".agents/commands/shared-command.md"
    command.parent.mkdir(parents=True)
    command.write_text("# command\n", encoding="utf-8")
    return load_configuration(path)


def test_compiles_every_effective_handler_in_lifecycle_order(tmp_path: Path) -> None:
    plan = compile_workflow_plan(
        _configuration(tmp_path), tmp_path, "task", "codex", "TASK-7"
    )

    assert [(item.name, item.phase, item.source) for item in plan.items] == [
        ("clean", "before_start_workflow", "global"),
        ("init", "step", "step"),
        ("setup", "step", "step"),
        ("inline-argv", "before_start", "workflow"),
        ("develop", "step", "step"),
        ("shared-command", "before_complete", "step"),
        ("shared-skill", "after_complete", "global"),
        ("start-workflow", "after_complete", "step"),
    ]
    clean, init, _, _, _, slash, skill, transition = plan.items
    assert clean.kind == "cli" and clean.owner == "ww"
    assert clean.execution == "automatic"
    assert not clean.requires_agent_input
    assert init.reasoning == "low" and init.profile is None
    assert slash.kind == "slash_command" and slash.owner == "agent"
    assert slash.execution == "agent_instruction"
    assert skill.kind == "skill"
    assert isinstance(transition.operation, WorkflowHandoff)
    assert transition.operation.target == "{{workflow}}"
    assert transition.dependencies == ("workflow",)
    assert all(item.position == index for index, item in enumerate(plan.items, 1))


def test_compiles_grouped_hook_handlers_in_order_with_shared_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """handlers:
  - name: collect-note
    kind: prompt
    variables:
      - name: note
  - name: print-note
    argv: [printf, "%s", "{{note}}"]
hooks:
  before_complete:
    - workflows: [task]
      steps: [develop]
      handlers:
        - name: collect-note
        - name: print-note
workflows:
  - name: task
    steps:
      - name: develop
        kind: prompt
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    assert [item.name for item in plan.items] == [
        "init",
        "develop",
        "collect-note",
        "print-note",
        "update-workflow-summary",
    ]
    collect, print_note = plan.items[2:4]
    assert collect.source == "global"
    assert collect.provide[0].name == "note"
    assert print_note.payload_as(Commands).commands[0].argv == (
        "printf",
        "%s",
        "{{note}}",
    )
    assert print_note.dependencies == ("note",)


def test_authoritative_task_id_updates_the_derived_input_flag(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: initialize
        argv: [echo, ready]
        variables:
          - name: task_id
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(
        load_configuration(path), tmp_path, "task", "codex", "TASK-1"
    )

    assert plan.items[0].provide == ()
    assert not plan.items[0].requires_agent_input


def test_auto_resolution_prioritizes_skill_then_slash_then_prompt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: both
      - name: command-only
      - name: prose
        description: Explain what to do.
""",
        encoding="utf-8",
    )
    skill = tmp_path / ".agents/skills/both"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    command_dir = tmp_path / ".agents/commands"
    command_dir.mkdir(parents=True)
    (command_dir / "both.md").write_text("# command\n", encoding="utf-8")
    (command_dir / "command-only.md").write_text("# command\n", encoding="utf-8")

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    assert [item.kind for item in plan.items] == [
        "prompt",
        "skill",
        "slash_command",
        "prompt",
        "prompt",
    ]


def test_mcp_handler_renders_as_agent_instruction(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """hooks:
  before_complete:
    - steps: [develop]
      mcp: github
      description: Create a pull request for this branch.
workflows:
  - name: task
    steps:
      - name: develop
        mcp: linear
        description: Update the issue with the implementation summary.
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    _, step, hook, _ = plan.items
    assert step.kind == "mcp"
    assert step.owner == "agent"
    assert step.execution == "agent_instruction"
    assert step.payload_as(Mcp).connection == "linear"
    assert hook.kind == "mcp"
    assert hook.payload_as(Mcp).prompt == "Create a pull request for this branch."

    markdown = render_plan(plan, False)
    assert "For the following work use `github` mcp connection:" in markdown
    assert "Create a pull request for this branch." in markdown


def test_resolves_profiles_from_agent_files_then_configuration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """profiles:
  reviewer: Review the implementation.
  researcher: Investigate the problem.
workflows:
  - name: task
    profile: reviewer
    steps:
      - name: inspect
        kind: prompt
      - name: develop
        kind: prompt
        profile: researcher
""",
        encoding="utf-8",
    )
    profile = tmp_path / ".codex/agents/reviewer.md"
    profile.parent.mkdir(parents=True)
    profile.write_text("# Reviewer\n", encoding="utf-8")

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    init, inspect, develop, _ = plan.items
    assert init.profile is None
    assert inspect.profile_instruction is None
    assert inspect.profile_path == ".codex/agents/reviewer.md"
    assert develop.profile == "researcher"
    assert develop.profile_instruction == "Investigate the problem."


def test_profile_name_without_definition_is_an_instruction(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: develop
        profile: missing
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")
    assert plan.items[1].profile_instruction == "Use the `missing` profile."


def test_validates_interpolation_data_flow_and_supports_nested_steps(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: setup
        hooks:
          after_complete:
            - handoff_to: "{{missing}}"
  - name: nested
    steps:
      - name: parent
        steps:
          - name: child
""",
        encoding="utf-8",
    )
    configuration = load_configuration(path)

    with pytest.raises(ConfigurationError, match="unavailable variable"):
        compile_workflow_plan(configuration, tmp_path, "task", "codex")
    nested = compile_workflow_plan(configuration, tmp_path, "nested", "codex")
    assert nested.items[1].id == "nested:parent/child:step:step:1"
    assert nested.items[1].step == "parent/child"
    assert nested.items[1].parent == "parent"
    assert nested.items[1].ancestors == ("parent",)


def test_hook_provided_values_flow_to_later_hooks_and_steps(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: publish
        hooks:
          after_complete:
            - name: capture-url
              kind: prompt
              variables:
                - name: url
      - name: announce
        description: Announce {{url}}.
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    announce = next(item for item in plan.items if item.name == "announce")
    assert announce.payload_as(Prompt).text == "Announce {{url}}."
    assert announce.dependencies == ("url",)


def test_compilation_options_own_bootstrap_plan_interpretation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """hooks:
  before_start_workflow:
    - argv: [printf, start]
workflows:
  - name: task
    steps:
      - name: create-issue
        variables:
          - name: task_id
      - name: develop
""",
        encoding="utf-8",
    )
    configuration = load_configuration(path)

    explicit = compile_workflow_plan(configuration, tmp_path, "task", "codex", "PROJ-1")
    resumed = compile_workflow_plan(
        configuration,
        tmp_path,
        "task",
        "codex",
        extensions=None,
        options=PlanCompilationOptions(
            task_id="PROJ-1", completed_bootstrap_step="create-issue"
        ),
    )

    assert explicit.items[2].name == "create-issue"
    assert explicit.items[2].provide == ()
    assert [item.name for item in resumed.items[:3]] == [
        "inline-argv",
        "init",
        "develop",
    ]
    assert [item.position for item in resumed.items] == list(
        range(1, len(resumed.items) + 1)
    )


def test_recursively_flattens_substeps_and_preserves_parent_lifecycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """hooks:
  before_complete:
    - steps: [parent]
      argv: [printf, parent-complete]
workflows:
  - name: task
    steps:
      - name: parent
        hooks:
          before_start:
            - argv: [printf, parent-start]
        steps:
          - name: child
            steps:
              - name: leaf
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    assert [(item.name, item.step, item.parent, item.phase) for item in plan.items] == [
        ("init", "init", None, "step"),
        ("inline-argv", "parent", None, "before_start"),
        ("leaf", "parent/child/leaf", "parent/child", "step"),
        ("inline-argv", "parent", None, "before_complete"),
        ("update-workflow-summary", "parent", None, "before_complete_workflow"),
    ]
    assert plan.items[2].ancestors == ("parent", "parent/child")


def test_registered_construct_planner_uses_shared_lifecycle_wrapper(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """hooks:
  before_start:
    - steps: [custom]
      argv: [printf, wrapper-start]
  after_complete:
    - steps: [custom]
      argv: [printf, wrapper-finish]
workflows:
  - name: task
    steps:
      - name: custom
        steps:
          - name: nested
            description: Run nested work.
""",
        encoding="utf-8",
    )
    registry = builtin_construct_planners()
    registry.register(_TestSequenceDefinition, _TestSequencePlanner())

    def normalize(step: StepDefinition) -> object:
        if step.name == "custom":
            return _TestSequenceDefinition(step, step.child_steps)
        return normalize_construct(step)

    plan = WorkflowPlanCompiler(
        load_configuration(path),
        tmp_path,
        "codex",
        construct_planners=registry,
        construct_normalizer=normalize,
    ).compile("task")

    assert [(item.name, item.step, item.phase) for item in plan.items] == [
        ("init", "init", "step"),
        ("inline-argv", "custom", "before_start"),
        ("nested", "custom/nested", "step"),
        ("inline-argv", "custom", "after_complete"),
        ("update-workflow-summary", "custom", "before_complete_workflow"),
    ]
    assert plan.items[2].parent == "custom"
    assert plan.items[2].ancestors == ("custom",)


def test_items_step_hooks_wrap_the_flow_without_collect_annotations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: collect
        items: ~
        hooks:
          before_start:
            - name: prepare
              description: Prepare collection.
          after_complete:
            - name: clean-up
              description: Clean up collection.
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    assert [(item.name, item.item_template) for item in plan.items] == [
        ("init", False),
        ("prepare", False),
        ("collect", False),
        ("handle-item", True),
        ("clean-up", False),
        ("update-workflow-summary", False),
    ]
    # Only the collection itself expands the plan; hooks and the summary that
    # surround the whole item flow must not.
    assert [item.item_operation for item in plan.items] == [
        None,
        None,
        "collect",
        "handle_item",
        None,
        None,
    ]


def test_hook_step_path_targets_only_the_named_substep(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """hooks:
  before_start:
    - steps: [plan-and-fix/fix]
      argv: [printf, targeted]
workflows:
  - name: task
    steps:
      - name: plan-and-fix
        steps:
          - name: fix
      - name: verify-and-fix
        steps:
          - name: fix
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    assert [(item.name, item.step) for item in plan.items] == [
        ("init", "init"),
        ("inline-argv", "plan-and-fix/fix"),
        ("fix", "plan-and-fix/fix"),
        ("fix", "verify-and-fix/fix"),
        ("update-workflow-summary", "verify-and-fix"),
    ]


def test_exact_wrapper_path_wins_over_nested_leaf_with_the_same_name(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """hooks:
  before_complete:
    - steps: [code-review]
      argv: [printf, commit]
workflows:
  - name: task
    steps:
      - name: code-review
        steps:
          - name: code-review

          - name: fix
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    commits = [item for item in plan.items if item.name == "inline-argv"]
    assert [(item.step, item.phase) for item in commits] == [
        ("code-review", "before_complete")
    ]


def test_hook_step_path_ignores_dynamic_item_segment(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """hooks:
  before_start:
    - steps: [review/fix]
      argv: [printf, targeted]
workflows:
  - name: task
    steps:
      - name: review
        items:
          steps:
            - name: fix
              item_phase: analyze
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    assert [(item.name, item.step, item.item_template) for item in plan.items] == [
        ("init", "init", False),
        ("review", "review", False),
        ("inline-argv", "review/{item}/fix", True),
        ("fix", "review/{item}/fix", True),
        ("update-workflow-summary", "review", False),
    ]


def test_a_transition_makes_a_handoff_workflow_ending_with_it(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """handlers:
  - name: finalize
    kind: prompt
workflows:
  - name: decide
    hooks:
      before_complete_workflow:
        - name: finalize
    steps:
      - name: choose
        variables:
          - name: workflow
        hooks:
          after_complete:
            - handoff_to: "{{workflow}}"
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "decide", "codex")
    assert plan.handoff
    assert (plan.items[-2].name, plan.items[-2].phase) == (
        "finalize",
        "before_complete_workflow",
    )
    assert plan.items[-1].kind == "workflow_transition"
    assert "**Handoff workflow:**" in render_plan(plan, False)


def test_workflow_boundary_hooks_run_once_in_scope_order(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """handlers:
  - name: global-start
    kind: prompt
  - name: workflow-start
    kind: prompt
  - name: global-finish
    kind: prompt
  - name: workflow-finish
    kind: prompt
hooks:
  before_start_workflow:
    - workflows: [task]
      name: global-start
  before_complete_workflow:
    - workflows: [task]
      name: global-finish
workflows:
  - name: task
    hooks:
      before_start_workflow:
        - name: workflow-start
      before_complete_workflow:
        - name: workflow-finish
    steps:
      - name: first
      - name: last
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    boundary_items = [
        (item.name, item.phase, item.source)
        for item in plan.items
        if "workflow" in item.phase
    ]
    assert boundary_items == [
        ("global-start", "before_start_workflow", "global"),
        ("workflow-start", "before_start_workflow", "workflow"),
        ("global-finish", "before_complete_workflow", "global"),
        ("workflow-finish", "before_complete_workflow", "workflow"),
        ("update-workflow-summary", "before_complete_workflow", "internal"),
    ]
    assert [item.name for item in plan.items].index("global-finish") > [
        item.name for item in plan.items
    ].index("last")


def test_plan_json_and_markdown_preserve_unresolved_and_bound_variables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        argv: [printf, "{{ww.task.id}} {{ww.task.workflows}} {{value}}"]
        variables:
          - name: value
""",
        encoding="utf-8",
    )
    configuration = load_configuration(path)
    plan = compile_workflow_plan(configuration, tmp_path, "task", "codex", "TASK-1")

    assert plan.items[1].payload_as(Commands).commands[0].argv == (
        "printf",
        "TASK-1 task {{value}}",
    )
    assert plan.items[1].dependencies == ("ww.task.id", "ww.task.workflows", "value")
    assert '"task_id": "TASK-1"' in render_plan(plan, True)
    markdown = render_plan(plan, False)
    assert "# Workflow plan — `task`" in markdown
    assert "**Stage:** Perform this workflow step" in markdown
    assert "**Workflow step:** `work`" in markdown
    assert "**Owner:** `ww`" in markdown
    assert "**Execution:** `automatic`" in markdown
    assert "**Automated by ww:**" in markdown
    assert "```sh" in markdown


def test_markdown_does_not_repeat_a_prompt_handler_description(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: research
        description: Research the problem carefully.
""",
        encoding="utf-8",
    )

    markdown = render_plan(
        compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex"),
        False,
    )

    assert markdown.count("Research the problem carefully.") == 1


def test_automatic_cli_handler_can_wait_for_agent_input(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """handlers:
  - name: commit
    argv: [git, commit, -m, "{{message}}"]
    variables:
      - name: message
        description: Commit message.
workflows:
  - name: task
    steps:
      - name: develop
        hooks:
          before_complete:
            - name: commit
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")
    commit = plan.items[-2]

    assert commit.execution == "automatic"
    assert commit.requires_agent_input
    markdown = render_plan(plan, False)
    assert "**Required input from the agent**" in markdown
    assert (
        "ww runs this command automatically after these values are supplied" in markdown
    )


def test_several_hook_commands_and_explicit_missing_action_are_checked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    hooks:
      before_start_workflow:
        - handlers:
            - argv: [printf, first]
            - argv: [printf, second]
    steps:
      - name: work
      - name: missing-skill
        kind: skill
""",
        encoding="utf-8",
    )
    configuration = load_configuration(path)

    with pytest.raises(ConfigurationError, match="configured skill not found"):
        compile_workflow_plan(configuration, tmp_path, "task", "codex")

    path.write_text(
        """workflows:
  - name: task
    hooks:
      before_start_workflow:
        - handlers:
            - argv: [printf, first]
            - argv: [printf, second]
    steps:
      - name: work
""",
        encoding="utf-8",
    )
    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")
    assert tuple(
        command.argv
        for item in plan.items[:2]
        for command in item.payload_as(Commands).commands
    ) == (
        ("printf", "first"),
        ("printf", "second"),
    )


def test_core_control_keys_compile_to_core_operations(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: choose
    steps:
      - decide: Decide where to go.
        artifact: false
      - select: ~
        handoff_to: target
  - name: target
    steps:
      - name: work
  - name: parent
    steps:
      - name: collect
        children:
          workflow: target
""",
        encoding="utf-8",
    )
    configuration = load_configuration(path)

    handoff = compile_workflow_plan(configuration, tmp_path, "choose", "codex")
    transition = handoff.items[-1]
    assert transition.name == "select"
    assert transition.kind == "workflow_transition"
    assert transition.execution == "workflow_transition"
    assert isinstance(transition.operation, WorkflowHandoff)
    assert transition.operation.target == "target"

    parent = compile_workflow_plan(configuration, tmp_path, "parent", "codex")
    dispatch = next(item for item in parent.items if item.name == "children")
    assert dispatch.step == "collect/children"
    assert dispatch.parent == "collect"
    assert dispatch.kind == "child_workflow"
    assert (dispatch.owner, dispatch.execution) == ("ww", "agent_instruction")
    assert isinstance(dispatch.operation, ChildWorkflowRun)
    assert dispatch.operation.workflow == "target"

    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: odd
        action:
          type: loop
          loop_id: odd
          operation: enter
          max_times: 2
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="workflow loops were removed"):
        load_configuration(path)


def test_handoff_hook_is_not_replaced_by_a_handler_named_start_workflow(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """handlers:
  - name: start-workflow
    description: An unrelated project handler sharing the handoff name.
workflows:
  - name: chooser
    steps:
      - name: pick
        variables:
          - name: workflow
        hooks:
          after_complete:
            - handoff_to: "{{workflow}}"
  - name: target
    steps:
      - name: work
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "chooser", "codex")
    transition = plan.items[-1]
    assert transition.kind == "workflow_transition"
    assert transition.registered_handler is None
    assert isinstance(transition.operation, WorkflowHandoff)
    assert transition.operation.target == "{{workflow}}"


def test_plan_item_rejects_contradictory_automatic_action() -> None:
    with pytest.raises(ValueError, match="conflicts with owner/execution"):
        plan_item(execution="automatic")


@pytest.mark.parametrize(
    "changes, payload",
    [
        ({"operation": PlannedAction("prompt", Skill("unexpected"))}, "payload"),
        (
            {
                "operation": PlannedAction(
                    "prompt", Commands((CommandDefinition(argv=("true",)),))
                )
            },
            "payload",
        ),
        (
            {
                "operation": PlannedAction(
                    "prompt", Extension("ext/acme/example/handlers:run")
                )
            },
            "payload",
        ),
    ],
)
def test_plan_item_rejects_payloads_for_another_action_kind(
    changes: dict[str, object], payload: str
) -> None:
    with pytest.raises(ValueError, match=rf"action {payload}"):
        plan_item(**changes)


def test_plan_item_rejects_inconsistent_agent_input_flag() -> None:
    with pytest.raises(ValueError, match="requires_agent_input"):
        plan_item(
            operation=PlannedAction(
                "cli", Commands((CommandDefinition(argv=("true",)),))
            ),
            owner="ww",
            execution="automatic",
            provide=(ProvidedVariable("answer"),),
        )


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"phase": "later"}, "invalid plan item phase"),
        ({"item_operation": "archive"}, "invalid item operation"),
        ({"child_operation": "dispatch"}, "invalid child operation"),
    ],
)
def test_plan_item_rejects_unknown_closed_values(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        plan_item(**changes)


def test_a_compiled_plan_keeps_its_item_ids_and_operations_through_a_reload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: cycle
        steps:
          - review: Review it.

      - name: split
        children:
          workflow: child
      - name: choose
        hooks:
          after_complete:
            - handoff_to: child
  - name: child
    steps:
      - work: Work.
""",
        encoding="utf-8",
    )

    def compiled() -> WorkflowPlan:
        return WorkflowPlanCompiler(
            load_configuration(path), tmp_path, "codex", "TASK-1"
        ).compile("task")

    plan = compiled()
    snapshot = PlanSnapshot(
        schema_version=PLAN_SCHEMA_VERSION,
        compiler_version="test",
        configuration_digest="digest",
        compiled_at="2026-01-01T00:00:00Z",
        plan=plan,
    )
    loaded = PlanSnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict()))).plan

    assert [item.id for item in compiled().items] == [item.id for item in plan.items]
    assert loaded == plan
    kinds = {type(item.operation) for item in loaded.items}
    assert {ChildWorkflowRun, WorkflowHandoff} <= kinds


def test_plan_snapshot_persists_explicit_and_reads_legacy_items(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - task: ~
    explicit: true
    steps:
      - inspect: Inspect the change.
""",
        encoding="utf-8",
    )
    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")
    snapshot = PlanSnapshot(
        schema_version=PLAN_SCHEMA_VERSION,
        compiler_version="test",
        configuration_digest="digest",
        compiled_at="2026-01-01T00:00:00Z",
        plan=plan,
    )
    raw = json.loads(json.dumps(snapshot.to_dict()))
    assert next(item for item in plan.items if item.step == "inspect").explicit is True
    assert "Explicit work guidance:** enabled" in render_plan(plan, False)
    for plan_name in ("plan", "template_plan"):
        assert any(
            item.get("explicit") is True
            for item in raw[plan_name]["items"]
            if item["step"] == "inspect"
        )
        for item in raw[plan_name]["items"]:
            item.pop("explicit", None)

    legacy = PlanSnapshot.from_dict(raw)

    assert all(item.explicit is False for item in legacy.plan.items)


def test_step_label_numbers_nested_steps_over_the_top_level_count() -> None:
    items = number_step_paths(
        (
            plan_item(id="w:init:1", step="init"),
            plan_item(id="w:build/a:1", step="build/a", ancestors=("build",)),
            plan_item(id="w:build/b:1", step="build/b", ancestors=("build",)),
            plan_item(id="w:deploy:1", step="deploy"),
        )
    )

    assert [step_label(item.step_ordinals, items) for item in items] == [
        ("1", 3),
        ("2.1", 3),
        ("2.2", 3),
        ("3", 3),
    ]


def test_step_label_total_survives_item_expansion() -> None:
    template = plan_item(
        id="w:collect/{item}/process:1",
        step="collect/{item}/process",
        ancestors=("collect", "collect/{item}"),
        item_template=True,
    )
    before = number_step_paths((plan_item(id="w:init:1", step="init"), template))
    expanded = number_step_paths(
        (
            plan_item(id="w:init:1", step="init"),
            *(
                plan_item(
                    id=f"w:collect/{segment}/process:1",
                    step=f"collect/{segment}/process",
                    ancestors=("collect", f"collect/{segment}"),
                )
                for segment in ("item-1", "item-2")
            ),
        )
    )

    assert step_label(before[1].step_ordinals, before) == ("2.1.1", 2)
    assert [step_label(item.step_ordinals, expanded) for item in expanded] == [
        ("1", 2),
        ("2.1.1", 2),
        ("2.2.1", 2),
    ]


def test_step_label_counts_a_bootstrap_step_the_run_plan_dropped() -> None:
    plan = number_step_paths(
        (
            plan_item(id="w:init:1", step="init"),
            plan_item(id="w:create-jira:1", step="create-jira"),
            plan_item(id="w:develop:1", step="develop"),
        )
    )
    run_items = tuple(item for item in plan if item.step != "create-jira")

    assert step_label(plan[1].step_ordinals, run_items) == ("2", 3)
    assert step_label((2,), plan[:1]) == ("2", 2)
