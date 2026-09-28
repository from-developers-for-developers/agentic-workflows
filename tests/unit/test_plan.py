# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import dataclass
from pathlib import Path

import pytest

from ww.actions import (
    Commands,
    Mcp,
    Prompt,
)
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.extensions import ExtensionRegistry
from ww.output import render_plan
from ww.plan import (
    ChildWorkflowRun,
    LoopBoundary,
    PlanCompilationOptions,
    WorkflowHandoff,
    WorkflowPlanCompiler,
    compile_workflow_plan,
)
from ww.plan.constructs import (
    ExpansionResult,
    PlanningContext,
    builtin_construct_planners,
    normalize_construct,
)
from ww.project_config import ProjectConfig
from ww.workflow_config import INIT_STEP_PROMPT, StepDefinition


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
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """hooks:
  before_complete:
    - steps: [init]
      command:
        argv: [printf, saved]
workflows:
  - name: task
    steps:
      - name: work
        depends_on: init
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


def test_extension_handler_can_be_used_as_a_step(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
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
    path = tmp_path / "workflows.yaml"
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


def test_step_handler_compiles_a_named_loop_step(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """handlers:
  - code-review:
      loop:
        - code-review: Perform the review.
          break: No meaningful remarks remain.
        - fix: Fix the review findings.
workflows:
  - name: task
    steps:
      - code-review: ~
        handler: code-review
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    assert [
        (item.step, item.kind, item.loop_break)
        for item in plan.items
        if item.step.startswith("code-review") and item.phase == "step"
    ] == [
        ("code-review", "loop", None),
        ("code-review/code-review", "prompt", "No meaningful remarks remain."),
        ("code-review/fix", "prompt", None),
        ("code-review", "loop", None),
    ]


def _configuration(tmp_path: Path):
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """handlers:
  - name: clean
    command:
      argv: [git, status, --porcelain]
  - name: shared-skill
    skill: true
  - name: shared-command
    slash_command: true
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
    handoff: true
    hooks:
      before_in_progress:
        - steps: [develop]
          command:
            argv: [echo, beginning]
    steps:
      - name: setup
        description: Gather constraints.
        provide:
          - name: workflow
      - name: develop
        hooks:
          before_complete:
            - name: shared-command
          after_complete:
            - workflow: "{{workflow}}"
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
        ("inline-command", "before_in_progress", "workflow"),
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
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """handlers:
  - name: collect-note
    prompt: true
    provide:
      - name: note
  - name: print-note
    command:
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
        prompt: true
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
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: initialize
        command:
          argv: [echo, ready]
        provide:
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
    path = tmp_path / "workflows.yaml"
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
    path = tmp_path / "workflows.yaml"
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
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """profiles:
  reviewer: Review the implementation.
  researcher: Investigate the problem.
workflows:
  - name: task
    profile: reviewer
    steps:
      - name: inspect
        prompt: true
      - name: develop
        prompt: true
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
    path = tmp_path / "workflows.yaml"
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
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """workflows:
  - name: task
    handoff: true
    steps:
      - name: setup
        hooks:
          after_complete:
            - workflow: "{{missing}}"
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
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: publish
        hooks:
          after_complete:
            - name: capture-url
              prompt: true
              provide:
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
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """hooks:
  before_start_workflow:
    - command:
        argv: [printf, start]
workflows:
  - name: task
    steps:
      - name: create-issue
        provide:
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
        "inline-command",
        "init",
        "develop",
    ]
    assert [item.position for item in resumed.items] == list(
        range(1, len(resumed.items) + 1)
    )


def test_recursively_flattens_substeps_and_preserves_parent_lifecycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """hooks:
  before_complete:
    - steps: [parent]
      command:
        argv: [printf, parent-complete]
workflows:
  - name: task
    steps:
      - name: parent
        hooks:
          before_in_progress:
            - command:
                argv: [printf, parent-start]
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
        ("inline-command", "parent", None, "before_in_progress"),
        ("leaf", "parent/child/leaf", "parent/child", "step"),
        ("inline-command", "parent", None, "before_complete"),
        ("update-workflow-summary", "parent", None, "before_complete_workflow"),
    ]
    assert plan.items[2].ancestors == ("parent", "parent/child")


def test_registered_construct_planner_uses_shared_lifecycle_wrapper(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """hooks:
  before_in_progress:
    - steps: [custom]
      command:
        argv: [printf, wrapper-start]
  after_complete:
    - steps: [custom]
      command:
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
        ("inline-command", "custom", "before_in_progress"),
        ("nested", "custom/nested", "step"),
        ("inline-command", "custom", "after_complete"),
        ("update-workflow-summary", "custom", "before_complete_workflow"),
    ]
    assert plan.items[2].parent == "custom"
    assert plan.items[2].ancestors == ("custom",)


def test_items_step_hooks_wrap_the_flow_without_collect_annotations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: collect
        items: ~
        hooks:
          before_in_progress:
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
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """hooks:
  before_in_progress:
    - steps: [plan-and-fix/fix]
      command:
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
        ("inline-command", "plan-and-fix/fix"),
        ("fix", "plan-and-fix/fix"),
        ("fix", "verify-and-fix/fix"),
        ("update-workflow-summary", "verify-and-fix"),
    ]


def test_exact_wrapper_path_wins_over_nested_leaf_with_the_same_name(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """hooks:
  before_complete:
    - steps: [code-review]
      command:
        argv: [printf, commit]
workflows:
  - name: task
    steps:
      - name: code-review
        loop:
          - name: code-review
            break: No findings remain.
          - name: fix
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    commits = [item for item in plan.items if item.name == "inline-command"]
    assert [(item.step, item.phase) for item in commits] == [
        ("code-review", "before_complete")
    ]


def test_hook_step_path_ignores_dynamic_item_segment(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """hooks:
  before_in_progress:
    - steps: [review/fix]
      command:
        argv: [printf, targeted]
workflows:
  - name: task
    steps:
      - name: review
        items:
          steps:
            - name: fix
              process_item: ~
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    assert [(item.name, item.step, item.item_template) for item in plan.items] == [
        ("init", "init", False),
        ("review", "review", False),
        ("inline-command", "review/{item}/fix", True),
        ("fix", "review/{item}/fix", True),
        ("update-workflow-summary", "review", False),
    ]


def test_handoff_requires_and_marks_a_terminal_transition(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """handlers:
  - name: finalize
    prompt: true
workflows:
  - name: decide
    handoff: true
    hooks:
      before_complete_workflow:
        - name: finalize
    steps:
      - name: choose
        provide:
          - name: workflow
        hooks:
          after_complete:
            - workflow: "{{workflow}}"
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

    path.write_text(
        """workflows:
  - name: invalid
    handoff: true
    steps:
      - name: choose
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="must end with a workflow transition"):
        compile_workflow_plan(load_configuration(path), tmp_path, "invalid", "codex")


def test_workflow_boundary_hooks_run_once_in_scope_order(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """handlers:
  - name: global-start
    prompt: true
  - name: workflow-start
    prompt: true
  - name: global-finish
    prompt: true
  - name: workflow-finish
    prompt: true
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
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        command:
          argv: [printf, "{{__task_id}} {{__workflows}} {{value}}"]
        provide:
          - name: value
""",
        encoding="utf-8",
    )
    configuration = load_configuration(path)
    plan = compile_workflow_plan(configuration, tmp_path, "task", "codex", "TASK-1")

    assert plan.items[1].payload_as(Commands).commands[0].argv == (
        "printf",
        "TASK-1 task,catchall {{value}}",
    )
    assert plan.items[1].dependencies == ("__task_id", "__workflows", "value")
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
    path = tmp_path / "workflows.yaml"
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
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """handlers:
  - name: commit
    command:
      argv: [git, commit, -m, "{{message}}"]
    provide:
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


def test_multi_command_inline_handler_and_explicit_missing_action_are_checked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """workflows:
  - name: task
    hooks:
      before_start_workflow:
        - command:
            - argv: [printf, first]
            - argv: [printf, second]
    steps:
      - name: work
      - name: missing-skill
        skill: true
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
        - command:
            - argv: [printf, first]
            - argv: [printf, second]
    steps:
      - name: work
""",
        encoding="utf-8",
    )
    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")
    assert tuple(
        command.argv for command in plan.items[0].payload_as(Commands).commands
    ) == (
        ("printf", "first"),
        ("printf", "second"),
    )


def test_loop_limit_uses_project_default_and_step_override(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """workflows:
  - task: ~
    steps:
      - project-default: ~
        loop:
          - work: Work.
      - overridden: ~
        loop_max_times: 8
        loop:
          - work: Work.
""",
        encoding="utf-8",
    )

    plan = compile_workflow_plan(
        load_configuration(path),
        tmp_path,
        "task",
        "codex",
        project_config=ProjectConfig(loop_max_times=5),
    )
    boundaries = [item for item in plan.items if item.kind == "loop"]

    assert [
        (item.operation.loop_id, item.operation.max_times)
        for item in boundaries
        if isinstance(item.operation, LoopBoundary)
    ] == [
        ("project-default", 5),
        ("project-default", 5),
        ("overridden", 8),
        ("overridden", 8),
    ]
    markdown = render_plan(plan, False)
    assert "- Maximum iterations: `5`" in markdown
    assert "- Maximum iterations: `8`" in markdown


def test_loop_stop_gate_requires_an_agent_owned_body_step(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """workflows:
  - task: ~
    steps:
      - retry: ~
        loop:
          - check: ~
            argv: [printf, done]
            break: The check succeeded.
""",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigurationError, match="uses break/continue but is not agent-owned"
    ):
        compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")


def test_core_control_keys_compile_to_core_operations(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """workflows:
  - name: choose
    handoff: true
    steps:
      - decide: Decide where to go.
        artifact: false
      - select: ~
        workflow: target
  - name: target
    steps:
      - name: work
  - name: parent
    steps:
      - name: collect
        children: ~
      - name: dispatch
        workflow_per_child: target
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
    dispatch = next(item for item in parent.items if item.name == "dispatch")
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
    with pytest.raises(ConfigurationError, match="is a core control"):
        load_configuration(path)


def test_handoff_hook_is_not_replaced_by_a_handler_named_start_workflow(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        """handlers:
  - name: start-workflow
    description: An unrelated project handler sharing the handoff name.
workflows:
  - name: chooser
    handoff: true
    steps:
      - name: pick
        provide:
          - name: workflow
        hooks:
          after_complete:
            - workflow: "{{workflow}}"
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
