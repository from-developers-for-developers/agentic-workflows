# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from ww.actions import Commands, Prompt
from ww.config import load_configuration, parse_yaml_configuration, parse_yaml_text
from ww.errors import ConfigurationError
from ww.workflow_config import (
    HandlerDefinition,
    ProvidedVariable,
    SavedMetadata,
    StepDefinition,
)
from ww.workflow_validation import validate_configuration


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_yaml_frontend_only_translates_before_shared_validation(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        """workflows:
  - name: task
    modes: [missing]
    steps:
      - name: work
""",
    )

    configuration = parse_yaml_configuration(path)

    assert configuration.workflows[0].modes == ("missing",)
    with pytest.raises(ConfigurationError, match="unknown mode"):
        validate_configuration(configuration)


def test_workflow_supports_named_entry_shorthand(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - quick: A short workflow.
    steps:
      - work: Do the work.
""",
        )
    )

    workflow = configuration.workflows[0]
    assert (workflow.name, workflow.description) == ("quick", "A short workflow.")
    assert (workflow.steps[0].name, workflow.steps[0].description) == (
        "work",
        "Do the work.",
    )


def test_task_format_is_normalized_and_validated(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """task_format: WORK-{timestamp}-{digit}
workflows:
  - name: task
    steps:
      - name: work
""",
        )
    )

    assert configuration.task_format == "WORK-{timestamp}-{digit}"


def test_task_format_accepts_uuid(tmp_path: Path) -> None:
    path = tmp_path / "workflows.yaml"
    path.write_text(
        "task_format: TASK-{uuid}\nworkflows:\n  - name: task\n    steps: []\n",
        encoding="utf-8",
    )

    assert load_configuration(path).task_format == "TASK-{uuid}"

    with pytest.raises(ConfigurationError, match="unknown placeholder"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                """task_format: WORK-{random}
workflows:
  - name: task
    steps: [{name: work}]
""",
            )
        )


def test_rejects_nested_workflows_at_yaml_boundary(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        """workflows:
  - name: parent
    workflows:
      - name: child
        steps:
          - name: work
""",
    )

    with pytest.raises(ConfigurationError, match="nested workflows.*not supported"):
        parse_yaml_configuration(path)


def test_depends_on_requires_an_earlier_artifact_step(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        """workflows:
  - name: task
    steps:
      - name: consume
        depends_on: produce
      - name: produce
""",
    )

    with pytest.raises(ConfigurationError, match="unknown or later sibling"):
        load_configuration(path)


def test_parses_normalized_records_and_command_forms(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        """modes:
  - name: economy
    description: Use fewer tokens.
handlers:
  - name: check
    command:
      command:
        - argv: [printf, first]
        - argv: [printf, second]
      assert:
        operator: eq
        expected: second
  - name: write
    command:
      argv: [printf, hello]
  - name: ask
    prompt: true
    description: Explain the work.
workflows:
  - name: task
    modes: [economy]
    steps:
      - name: prepare
        provide:
          - name: decision
            description: Chosen workflow.
      - name: finish
        description: Finish the work.
""",
    )

    configuration = load_configuration(path)

    assert configuration.modes[0].description == ("Use fewer tokens.",)
    check, write, ask = configuration.handlers
    assert tuple(command.argv for command in check.action.payload.commands) == (
        ("printf", "first"),
        ("printf", "second"),
    )
    assert (
        check.action.payload.assertion
        and check.action.payload.assertion.operator == "eq"
    )
    assert write.action.payload.commands[0].argv == ("printf", "hello")
    assert ask.action.identifier == "prompt"
    assert configuration.workflows[0].steps[0].provide[0].name == "decision"


def test_modes_accept_named_entry_shorthand(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """modes:
  - economy: Use fewer tokens.
  - careful: ~
workflows:
  - task: ~
    modes: [economy, careful]
    steps: []
""",
        )
    )

    assert [(mode.name, mode.description) for mode in configuration.modes] == [
        ("economy", ("Use fewer tokens.",)),
        ("careful", ()),
    ]


def test_parses_saved_task_metadata_declarations(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - name: task
    steps:
      - name: create-issue
        description: Create an issue.
        update_metadata:
          - name: jira_id
            key: integrations.jira.issue_id
            description: The created Jira issue ID.
""",
        )
    )

    saved = configuration.workflows[0].steps[0].save_metadata[0]
    assert (saved.name, saved.key, saved.description) == (
        "jira_id",
        "integrations.jira.issue_id",
        "The created Jira issue ID.",
    )


def test_parses_project_metadata_scope(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - name: task
    steps:
      - name: discover
        update_metadata:
          - name: url
            key: environments.staging.url
            scope: project
""",
        )
    )

    assert configuration.workflows[0].steps[0].save_metadata[0].scope == "project"


def test_expands_named_entry_shorthand_for_provide_and_save_metadata(
    tmp_path: Path,
) -> None:
    configuration = parse_yaml_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - name: task
    steps:
      - name: choose
        provide:
          - workflow: The selected workflow.
          - reason: ~
        update_metadata:
          - last_auto_refactored_at: Save the current timestamp.
            key: last_auto_refactored_at
            scope: project
          - result: ~
            key: task.result
""",
        )
    )

    step = configuration.workflows[0].steps[0]
    assert step.provide == (
        ProvidedVariable("workflow", "The selected workflow."),
        ProvidedVariable("reason", ""),
    )
    assert step.save_metadata == (
        SavedMetadata(
            "last_auto_refactored_at",
            "last_auto_refactored_at",
            "Save the current timestamp.",
            "project",
        ),
        SavedMetadata("result", "task.result", "", "task"),
    )


def test_explicit_name_preserves_provide_and_save_metadata_long_form(
    tmp_path: Path,
) -> None:
    configuration = parse_yaml_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - name: task
    steps:
      - name: work
        provide:
          - description: Existing provided value.
            name: result
        update_metadata:
          - key: task.result
            description: Existing metadata value.
            name: result
""",
        )
    )

    step = configuration.workflows[0].steps[0]
    assert step.provide[0] == ProvidedVariable("result", "Existing provided value.")
    assert step.save_metadata[0] == SavedMetadata(
        "result", "task.result", "Existing metadata value.", "task"
    )


def test_allows_the_same_metadata_path_in_different_scopes(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - name: task
    steps:
      - name: discover
        update_metadata:
          - name: task_url
            key: environment.url
          - name: project_url
            key: environment.url
            scope: project
""",
        )
    )

    assert len(configuration.workflows[0].steps[0].save_metadata) == 2


@pytest.mark.parametrize("key", ("metadata/foo", "foo:bar", ".foo", "foo."))
def test_rejects_non_dotted_task_metadata_keys(tmp_path: Path, key: str) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        f"""workflows:
  - name: task
    steps:
      - name: work
        update_metadata:
          - name: result
            key: {key}
""",
    )

    with pytest.raises(ConfigurationError, match="dotted metadata path"):
        load_configuration(path)


def test_parses_structured_argv_and_explicit_shell_actions(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - name: run
    command:
      - argv: [printf, "%s", "{{value}}"]
      - shell: printf '%s' "$VALUE" > result.txt
        env:
          VALUE: "{{value}}"
        args: ["{{value}}"]
workflows:
  - name: task
    steps:
      - name: work
""",
        )
    )

    argv, shell = configuration.handlers[0].action.payload.commands
    assert argv.argv == ("printf", "%s", "{{value}}")
    assert shell.shell == "printf '%s' \"$VALUE\" > result.txt"
    assert shell.env == (("VALUE", "{{value}}"),)
    assert shell.args == ("{{value}}",)


def test_parses_explicit_inline_handler_command(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """hooks:
  before_start_workflow:
    - command:
        argv: [printf, ready]
workflows:
  - name: task
    steps:
      - name: work
""",
        )
    )

    handler = configuration.global_hooks[0].handler
    assert not isinstance(handler, str)
    assert handler.name == "inline-command"
    assert handler.action.payload.commands[0].argv == ("printf", "ready")


def test_normalizes_root_step_and_hook_handler_syntax(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - name: check
    argv: [printf, ready]
hooks:
  before_start_workflow:
    - name: check
  before_complete:
    - steps: [work]
      name: gated-check
      argv: [printf, done]
      description: Is the work ready?
workflows:
  - name: task
    steps:
      - name: work
        shell: printf done
""",
        )
    )

    root = configuration.handlers[0]
    step = configuration.workflows[0].steps[0]
    reference, inline = configuration.global_hooks
    assert root.action.payload.commands[0].argv == ("printf", "ready")
    assert step.action.payload.commands[0].shell == "printf done"
    assert reference.handler == HandlerDefinition("check")
    assert inline.handler.action.payload.commands[0].argv == ("printf", "done")
    assert inline.handler.gate_prompt is None


def test_rejects_string_prompt_value(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="prompt must be true"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                """workflows:
  - task: ~
    steps:
      - name: work
        prompt: Is this needed?
""",
            )
        )


def test_step_handler_copies_a_catalog_handler_with_step_identity(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - handler_name: Run the shared check.
    argv: [printf, ready]
    provide:
      - value: A supplied value.
    agent: reviewer
workflows:
  - name: task
    steps:
      - some_step: Do this particular check.
        handler: handler_name
""",
        )
    )

    step = configuration.workflows[0].steps[0]
    assert step.name == "some_step"
    assert step.description == "Do this particular check."
    assert step.action.payload.commands[0].argv == ("printf", "ready")
    assert step.provide[0].name == "value"
    assert step.agent == "reviewer"


def test_step_handler_allows_explicit_step_overrides(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - name: shared
    argv: [printf, shared]
    agent: reviewer
workflows:
  - name: task
    steps:
      - name: work
        handler: shared
        argv: [printf, local]
        agent: developer
""",
        )
    )

    step = configuration.workflows[0].steps[0]
    assert step.action.payload.commands[0].argv == ("printf", "local")
    assert step.agent == "developer"


def test_step_handler_overrides_prompt_text_and_retains_command_permissions(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - name: ask
    prompt: true
    description: Original instruction.
  - name: restricted
    argv: [printf, inherited]
workflows:
  - name: task
    steps:
      - name: prompt-override
        handler: ask
        description: Replacement instruction.
      - name: command-override
        handler: restricted
        argv: [printf, replacement]
""",
        )
    )

    prompt, command = configuration.workflows[0].steps
    assert prompt.action is not None
    assert prompt.action.payload == Prompt("Replacement instruction.")
    assert command.action is not None
    assert isinstance(command.action.payload, Commands)
    assert command.action.payload.commands[0].argv == ("printf", "replacement")


def test_step_handler_explicit_prompt_retains_inherited_prompt_text(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - name: ask
    prompt: true
    description: Original instruction.
workflows:
  - name: task
    steps:
      - name: ask-again
        handler: ask
        prompt: true
""",
        )
    )

    step = configuration.workflows[0].steps[0]
    assert step.action is not None
    assert step.action.payload == Prompt("Original instruction.")


def test_step_handler_empty_prompt_description_uses_the_step_name(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - name: ask
    prompt: true
    description: Original instruction.
workflows:
  - name: task
    steps:
      - name: ask-again
        handler: ask
        description: ""
""",
        )
    )

    step = configuration.workflows[0].steps[0]
    assert step.action is not None
    assert step.action.payload == Prompt("ask-again")


def test_step_handler_inherits_a_named_loop_step(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - code-review:
      loop:
        - code-review: Perform the review.
          break: No meaningful remarks remain.
          profile: code-reviewer
        - fix: Fix the review findings.
          profile: developer
workflows:
  - name: task
    steps:
      - code-review: ~
        handler: code-review
""",
        )
    )

    handler = configuration.handlers[0]
    step = configuration.workflows[0].steps[0]
    assert isinstance(handler, StepDefinition)
    assert step.name == "code-review"
    assert step.loop_steps == handler.loop_steps
    assert [child.name for child in step.loop_steps] == ["code-review", "fix"]
    assert step.loop_steps[0].loop_break == "No meaningful remarks remain."


@pytest.mark.parametrize("handler", ("missing", [], None))
def test_step_handler_requires_a_known_handler_name(
    tmp_path: Path, handler: object
) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        """handlers:
  - name: shared
    prompt: true
workflows:
  - name: task
    steps:
      - name: work
        handler: PLACEHOLDER
""".replace("PLACEHOLDER", repr(handler) if handler is not None else "null"),
    )

    with pytest.raises(ConfigurationError, match="handler"):
        load_configuration(path)


def test_expands_named_entry_shorthand_for_handlers_steps_and_hooks(
    tmp_path: Path,
) -> None:
    configuration = parse_yaml_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - update-docs: Update the YAML specification.
  - no-description: ~
hooks:
  before_start_workflow:
    - handlers:
        - update-docs: ~
        - grouped-inline: Run the grouped hook.
    - direct-inline: Run the direct hook.
workflows:
  - name: task
    steps:
      - develop: Implement and test the change.
      - verify: ~
""",
        )
    )

    documented, undocumented = configuration.handlers
    assert documented.name == "update-docs"
    assert documented.description == "Update the YAML specification."
    assert documented.gate_prompt is None
    assert undocumented == HandlerDefinition("no-description")

    develop, verify = configuration.workflows[0].steps
    assert (develop.name, develop.description) == (
        "develop",
        "Implement and test the change.",
    )
    assert (verify.name, verify.description) == ("verify", "")

    reference, grouped, direct = configuration.global_hooks
    assert reference.handler == HandlerDefinition("update-docs")
    assert (grouped.handler.name, grouped.handler.description) == (
        "grouped-inline",
        "Run the grouped hook.",
    )
    assert (direct.handler.name, direct.handler.description) == (
        "direct-inline",
        "Run the direct hook.",
    )
    assert all(not hook.step_names for hook in configuration.global_hooks)


@pytest.mark.parametrize("value", ("[]", "{}", "true"))
def test_rejects_non_string_shorthand_descriptions(tmp_path: Path, value: str) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        f"""workflows:
  - name: task
    steps:
      - work: {value}
""",
    )

    with pytest.raises(ConfigurationError, match="shorthand description"):
        parse_yaml_configuration(path)


def test_explicit_name_keeps_the_existing_handler_syntax(tmp_path: Path) -> None:
    configuration = parse_yaml_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - description: Existing description.
    name: existing
workflows:
  - name: task
    steps:
      - description: Existing step description.
        name: work
""",
        )
    )

    assert configuration.handlers[0].name == "existing"
    assert configuration.handlers[0].description == "Existing description."
    assert configuration.workflows[0].steps[0].name == "work"


def test_rejects_handler_wrapper_and_non_mapping_group_entries(tmp_path: Path) -> None:
    wrapped = _write(
        tmp_path / "wrapped.yaml",
        """hooks:
  before_start_workflow:
    - name: actual
      handler: check
workflows:
  - name: task
    steps: [{name: work}]
""",
    )
    with pytest.raises(ConfigurationError, match="unknown key.*handler"):
        load_configuration(wrapped)

    grouped = _write(
        tmp_path / "grouped.yaml",
        """hooks:
  before_start_workflow:
    - handlers: [check]
workflows:
  - name: task
    steps: [{name: work}]
""",
    )
    with pytest.raises(ConfigurationError, match=r"handlers\[0\] must be a mapping"):
        load_configuration(grouped)


def test_expands_grouped_hook_handlers_using_the_shared_handler_shape(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - name: update-architecture
    prompt: true
hooks:
  before_complete:
    - workflows: [task]
      steps: [develop]
      handlers:
        - name: update-architecture
        - name: update-readme
          description: Update the README.
        - name: publish-docs
          mcp: github
          description: Publish the documentation.
workflows:
  - name: task
    steps:
      - name: develop
""",
        )
    )

    hooks = configuration.global_hooks
    assert len(hooks) == 3
    assert all(hook.workflow_names == ("task",) for hook in hooks)
    assert all(hook.step_names == ("develop",) for hook in hooks)
    assert all(hook.handler.gate_prompt is None for hook in hooks)
    assert hooks[0].handler.name == "update-architecture"
    readme = hooks[1].handler
    publish = hooks[2].handler
    assert not isinstance(readme, str)
    assert readme.name == "update-readme"
    assert readme.action is None
    assert not isinstance(publish, str)
    assert publish.action.identifier == "mcp"
    assert publish.action.payload.connection == "github"


def test_parses_hierarchical_step_hook_filter(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """hooks:
  before_in_progress:
    - steps: [plan-and-fix/fix]
      command:
        argv: [printf, ready]
workflows:
  - name: task
    steps:
      - name: plan-and-fix
        steps:
          - name: fix
""",
        )
    )

    assert configuration.global_hooks[0].step_names == ("plan-and-fix/fix",)


def test_shell_source_cannot_interpolate_workflow_values(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="shell source cannot interpolate"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                """handlers:
  - name: unsafe
    command:
      shell: echo "{{value}}"
workflows:
  - name: task
    steps:
      - name: work
""",
            )
        )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("tasks: []\nworkflows: []\n", "legacy 'tasks'"),
        (
            """hooks:
  before_start:
    - name: check
workflows:
  - name: task
    steps: []
""",
            "unknown key.*before_start",
        ),
        (
            "workflows:\n  - name: task\n    steps:\n      - init: {}\n",
            "shorthand description",
        ),
        (
            "workflows:\n  - name: task\n    steps:\n      - bad/name: invalid\n",
            "normalized name",
        ),
        (
            """workflows:
  - name: task
    steps:
      - name: work
        skill: true
        slash_command: true
""",
            "conflicting kinds",
        ),
        (
            """workflows:
  - name: task
    steps:
      - name: work
        hooks:
          before_start_workflow:
            - steps: [init]
              name: work
""",
            "unknown key",
        ),
        (
            """handlers:
  - name: check
    command:
      command:
        argv: [printf, ok]
      assert:
        operator: ne
        expected: ok
workflows:
  - name: task
    steps:
      - name: work
""",
            "must be eq",
        ),
        (
            """handlers:
  - name: check
    command: "printf ok"
workflows:
  - name: task
    steps:
      - name: work
""",
            "action mappings with argv or shell",
        ),
        (
            """hooks:
  before_complete:
    - handlers: []
workflows:
  - name: task
    steps:
      - name: work
""",
            "handlers must be a non-empty list",
        ),
        (
            """hooks:
  before_complete:
    - name: one
      handlers:
        - name: two
workflows:
  - name: task
    steps:
      - name: work
""",
            "cannot combine handlers with handler key",
        ),
    ],
)
def test_rejects_legacy_and_ambiguous_schema(
    tmp_path: Path, content: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_configuration(_write(tmp_path / "workflows.yaml", content))


def test_hook_filters_are_scoped_and_transition_accepts_interpolation(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        """hooks:
  after_complete:
    - workflows: [chooser]
      steps: [decide]
      name: document
handlers:
  - name: document
    prompt: true
workflows:
  - name: chooser
    handoff: true
    hooks:
      after_complete:
        - steps: [decide]
          workflow: "{{workflow}}"
    steps:
      - name: decide
        provide:
          - name: workflow
""",
    )

    configuration = load_configuration(path)
    transition = configuration.workflows[0].hooks[0].handler
    assert transition.operation is not None
    assert transition.operation.target == "{{workflow}}"
    assert configuration.global_hooks[0].workflow_names == ("chooser",)


@pytest.mark.parametrize("phase", ("before_start_workflow", "before_complete_workflow"))
def test_workflow_boundary_hooks_reject_step_filters(
    tmp_path: Path, phase: str
) -> None:
    content = f"""hooks:
  {phase}:
    - steps: [work]
      name: check
handlers:
  - name: check
    prompt: true
workflows:
  - name: task
    steps:
      - name: work
"""

    with pytest.raises(ConfigurationError, match="unknown key.*steps"):
        load_configuration(_write(tmp_path / "workflows.yaml", content))


def test_parses_mcp_handlers_and_direct_hook_declarations(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        """handlers:
  - name: create-pr
    mcp: github
    description: Create a pull request for this branch.
hooks:
  before_complete:
    - steps: [develop]
      mcp: github
      description: Create a pull request for this branch.
workflows:
  - name: task
    steps:
      - name: develop
        mcp: github
        description: Create a pull request for this branch.
""",
    )

    configuration = load_configuration(path)

    assert configuration.handlers[0].action.identifier == "mcp"
    assert configuration.handlers[0].action.payload.connection == "github"
    hook_handler = configuration.global_hooks[0].handler
    assert not isinstance(hook_handler, str)
    assert hook_handler.action.identifier == "mcp"
    assert hook_handler.action.payload.connection == "github"
    assert configuration.workflows[0].steps[0].action.payload.connection == "github"


def test_parses_profiles_for_workflows_and_steps(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """profiles:
  reviewer: Review implementation changes.
workflows:
  - name: task
    profile: reviewer
    steps:
      - name: develop
        profile:
          name: reviewer
          description: Review this step carefully.
""",
        )
    )

    assert configuration.profiles[0].description == "Review implementation changes."
    assert configuration.workflows[0].profile == "reviewer"
    step = configuration.workflows[0].steps[0]
    assert step.profile == "reviewer"
    assert step.profile_description == "Review this step carefully."


def test_parses_step_subagents_flag(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - name: task
    steps:
      - name: local-work
        subagents: false
""",
        )
    )

    assert configuration.workflows[0].steps[0].subagents is False

    with pytest.raises(ConfigurationError, match="subagents must be true or false"):
        load_configuration(
            _write(
                tmp_path / "invalid.yaml",
                """workflows:
  - name: task
    steps:
      - name: local-work
        subagents: disabled
""",
            )
        )


def test_parses_loop_wrapper_with_ordinary_nested_steps(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        loop_max_times: 5
        loop:
          - review: Review the implementation.
            break: There are no meaningful findings.
          - fix: Collect and fix findings.
            items: ~
            break: The findings cannot be fixed meaningfully.
""",
        )
    )

    wrapper = configuration.workflows[0].steps[0]
    assert wrapper.loop_break is None
    assert wrapper.loop_max_times == 5
    assert [step.name for step in wrapper.loop_steps] == ["review", "fix"]
    assert wrapper.loop_steps[0].loop_break == "There are no meaningful findings."
    items = wrapper.loop_steps[1].items
    assert items is not None
    assert [step.name for step in items.steps] == ["handle-item"]
    assert wrapper.loop_steps[1].loop_break == (
        "The findings cannot be fixed meaningfully."
    )


def test_loop_assignment_is_parsed_and_inherited_from_a_handler(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - handle_tests:
    loop_assignment: per_step
    loop:
      - test: Run the tests.
        break: No failures.
      - fix: Fix the failures.
workflows:
  - task: ~
    steps:
      - run-tests:
        handler: handle_tests
      - review-and-fix: ~
        loop:
          - review: Review.
            break: Clean.
          - fix: Fix.
""",
        )
    )

    inherited, plain = configuration.workflows[0].steps
    assert inherited.loop_assignment == "per_step"
    assert plain.loop_assignment is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("loop: []\n        break: Done.", "loop must contain at least one step"),
        ("break: Done.", "uses break outside a loop"),
        ("stop: Done.", "stop is obsolete; use break"),
        ("loop_max_times: 3", "loop_max_times requires a loop"),
        ("loop_assignment: per_step", "loop_assignment requires a loop"),
        (
            "loop_assignment: all_items\n        loop:\n          - work: Do it.",
            "loop_assignment must be one of: per_step, per_iteration",
        ),
        (
            "loop_max_times: 0\n        loop:\n          - work: Do it.",
            "loop_max_times must be a positive integer",
        ),
        (
            "loop_max_times: true\n        loop:\n          - work: Do it.",
            "loop_max_times must be a positive integer",
        ),
    ],
)
def test_rejects_incomplete_loop_syntax(
    tmp_path: Path, body: str, message: str
) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        (
            "workflows:\n  - task: ~\n    steps:\n      - wrapper: ~\n        "
            + body
            + "\n"
        ),
    )

    with pytest.raises(ConfigurationError, match=message):
        validate_configuration(load_configuration(path))


@pytest.mark.parametrize(
    "identifier", ["workflow_transition", "child_workflow", "loop"]
)
def test_core_controls_are_not_selectable_as_action_types(
    tmp_path: Path, identifier: str
) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        f"""workflows:
  - name: task
    steps:
      - name: step
        action:
          type: {identifier}
          workflow: target
""",
    )
    with pytest.raises(ConfigurationError, match="is a core control; use"):
        load_configuration(path)


def test_transition_step_carries_only_its_target(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        """workflows:
  - name: choose
    handoff: true
    steps:
      - select: Hand off.
        workflow: target
        argv: [true]
  - name: target
    steps:
      - name: work
""",
    )
    with pytest.raises(ConfigurationError, match="cannot contain other handler"):
        load_configuration(path)


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        (
            "  - name: choose\n    steps:\n      - go: ~\n        workflow: target\n",
            "must declare handoff: true",
        ),
        (
            "  - name: choose\n    handoff: true\n    steps:\n"
            "      - go: ~\n        workflow: target\n      - after: Never.\n",
            "must place its workflow transition step 'go' last",
        ),
        (
            "  - name: choose\n    handoff: true\n    steps:\n"
            "      - go: ~\n        workflow: target\n"
            "      - again: ~\n        workflow: target\n",
            "more than one workflow transition",
        ),
        (
            "  - name: choose\n    steps:\n      - pick: Pick.\n        hooks:\n"
            "          after_complete:\n            - workflow: target\n",
            "must declare handoff: true",
        ),
    ],
)
def test_misplaced_transitions_are_rejected_when_loading(
    tmp_path: Path, workflow: str, message: str
) -> None:
    path = _write(
        tmp_path / "workflows.yaml",
        "workflows:\n"
        + workflow
        + "  - name: target\n    steps:\n      - work: Work.\n",
    )
    with pytest.raises(ConfigurationError, match=message):
        load_configuration(path)


def test_yaml_text_parses_like_a_file(tmp_path: Path) -> None:
    text = "workflows:\n  - name: task\n    steps:\n      - work: Work.\n"

    assert parse_yaml_text(text) == parse_yaml_configuration(
        _write(tmp_path / "workflows.yaml", text)
    )
    with pytest.raises(ConfigurationError, match="invalid YAML in scenario: "):
        parse_yaml_text("workflows: [", "scenario")
    with pytest.raises(ConfigurationError, match="workflow configuration not found"):
        parse_yaml_configuration(tmp_path / "missing.yaml")


def test_save_metadata_append_is_parsed(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - task: ~
    steps:
      - report: Report the outcome.
        update_metadata:
          - handled: Root comment ids handled.
            key: pull_request.handled
            append: true
          - url: The pull request URL.
            key: pull_request.url
""",
        )
    )

    handled, url = configuration.workflows[0].steps[0].save_metadata
    assert (handled.key, handled.append) == ("pull_request.handled", True)
    assert (url.key, url.append) == ("pull_request.url", False)
    assert handled.to_dict()["append"] is True
    assert "append" not in url.to_dict()


def test_save_metadata_append_must_be_boolean(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="append must be true or false"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                """workflows:
  - task: ~
    steps:
      - report: Report.
        update_metadata:
          - handled: Ids.
            key: handled
            append: yes please
""",
            )
        )


def test_documents_are_declared_at_the_root_and_updated_by_steps(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """documents:
  - test_cases: The test cases derived from the issue, kept current across runs.
  - conventions: Team conventions every task follows.
    scope: project
workflows:
  - task: ~
    steps:
      - derive: Derive the test cases.
        update_document:
          - test_cases: Save every test case as a checklist item.
      - report: Build the report from {{documents.test_cases}}.
""",
        )
    )

    test_cases, conventions = configuration.documents
    assert (test_cases.name, test_cases.scope) == ("test_cases", "task")
    assert test_cases.description.startswith("The test cases")
    assert (conventions.name, conventions.scope) == ("conventions", "project")
    derive = configuration.workflows[0].steps[0]
    (update,) = derive.update_document
    assert (update.name, update.instruction) == (
        "test_cases",
        "Save every test case as a checklist item.",
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "documents:\n  - notes: Notes.\n    scope: global\n",
            "documents\\[0\\].scope must be 'task' or 'project'",
        ),
        (
            "documents:\n  - notes: Notes.\n  - notes: Again.\n",
            "duplicate document",
        ),
    ],
)
def test_document_declarations_are_validated(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                body + "workflows:\n  - task: ~\n    steps:\n      - work: Work.\n",
            )
        )


def test_update_document_must_name_a_declared_document(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigurationError,
        match="step 'derive' updates undeclared document\\(s\\): test_cases",
    ):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                """workflows:
  - task: ~
    steps:
      - derive: Derive.
        update_document:
          - test_cases: Save them.
""",
            )
        )


def test_the_old_save_metadata_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="save_metadata"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                """workflows:
  - task: ~
    steps:
      - work: Work.
        save_metadata:
          - id: The id.
            key: id
""",
            )
        )


def test_a_bare_step_named_like_a_root_handler_copies_it(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - fetch_requirements: Fetch the issue and its comments.
    mcp: atlassian
    provide:
      - issue_key: The issue key.
  - skim: Skim the issue.
    mcp: atlassian
workflows:
  - task: ~
    steps:
      - fetch_requirements: ~
      - skim:
        profile: issue-manager
      - unrelated: ~
  - other: ~
    steps:
      - skim: Only skim the issue.
""",
        )
    )

    bare, with_profile, unrelated = configuration.workflows[0].steps
    (described,) = configuration.workflows[1].steps
    assert bare.name == "fetch_requirements"
    assert bare.description == "Fetch the issue and its comments."
    assert bare.action is not None and bare.action.identifier == "mcp"
    assert [value.name for value in bare.provide] == ["issue_key"]
    # Settings on the bare step override the copy; the copy is otherwise kept.
    assert with_profile.profile == "issue-manager"
    assert with_profile.action is not None and with_profile.action.identifier == "mcp"
    # Content of its own keeps a step independent of the handler.
    assert described.description == "Only skim the issue."
    assert described.action is None
    assert unrelated.action is None and unrelated.description == ""


def test_document_paths_are_parsed_and_kept_inside_the_project(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """documents:
  - notes: Notes on the branch.
    path: documentation/issues/{task_id}/notes.md
  - glossary: Shared terms.
    scope: project
    path: docs/glossary.md
workflows:
  - task: ~
    steps:
      - work: Work.
""",
        )
    )
    notes, glossary = configuration.documents
    assert notes.path == "documentation/issues/{task_id}/notes.md"
    assert glossary.path == "docs/glossary.md"
    assert notes.to_dict()["path"] == notes.path


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ("    path: /etc/notes.md\n", "must stay inside the project"),
        ("    path: ../notes.md\n", "must stay inside the project"),
        (
            "    scope: project\n    path: docs/{task_id}.md\n",
            "project document path cannot use",
        ),
    ],
)
def test_invalid_document_paths_are_rejected(
    tmp_path: Path, declaration: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                "documents:\n  - notes: Notes.\n"
                + declaration
                + "workflows:\n  - task: ~\n    steps:\n      - work: Work.\n",
            )
        )


def test_interactive_is_parsed_inherited_and_folded_into_items(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - discuss: Discuss the architecture with the operator.
    interactive: true
workflows:
  - task: ~
    steps:
      - discuss: ~
      - build: Build it.
      - manual_tests: Split the test cases into items.
        items:
          interactive: true
""",
        )
    )

    discuss, build, manual = configuration.workflows[0].steps
    assert discuss.interactive is True
    assert build.interactive is False
    assert manual.items is not None
    assert manual.items.steps[0].interactive is True
    with pytest.raises(ConfigurationError, match="interactive must be true or false"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                "workflows:\n  - task: ~\n    steps:\n      - a: A.\n"
                "        interactive: maybe\n",
            )
        )


def test_a_workflow_may_declare_its_runtime(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - manual: Manual testing.
    runtime: single
    steps:
      - test: Test it.
  - task: ~
    steps:
      - work: Work.
""",
        )
    )
    manual, task, _catchall = configuration.workflows
    assert (manual.runtime, task.runtime) == ("single", None)
    with pytest.raises(ConfigurationError, match="runtime must be one of: single"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                "workflows:\n  - task: ~\n    runtime: parallel\n    steps:\n"
                "      - work: Work.\n",
            )
        )


def test_choices_are_labels_with_descriptions_and_need_an_interactive_step(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - task: ~
    steps:
      - verify: Verify with the operator.
        interactive: true
        choices:
          - pass: The test passed.
          - fail and give comment: It failed; the operator explains why.
""",
        )
    )
    (verify,) = configuration.workflows[0].steps
    assert [(c.label, c.description) for c in verify.choices] == [
        ("pass", "The test passed."),
        ("fail and give comment", "It failed; the operator explains why."),
    ]
    with pytest.raises(ConfigurationError, match="require interactive: true"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                "workflows:\n  - task: ~\n    steps:\n      - verify: Verify.\n"
                "        choices:\n          - pass: Passed.\n",
            )
        )


def test_ui_is_declared_on_interactive_per_item_stages_only(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """workflows:
  - name: manual
    steps:
      - name: collect
        description: Collect the cases.
        items:
          interactive: true
          ui: true
  - name: review
    steps:
      - name: review
        description: Review.
        items:
          steps:
            - name: verify
              description: Verify it.
              interactive: true
              ui: true
            - name: report
              description: Report it.
""",
        )
    )
    (collect,), (review,) = (flow.steps for flow in configuration.workflows[:2])
    assert collect.items is not None and collect.items.steps[0].ui is True
    assert review.items is not None
    assert [stage.ui for stage in review.items.steps] == [True, False]
    with pytest.raises(ConfigurationError, match="ui.*requires interactive: true"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                "workflows:\n  - task: ~\n    steps:\n      - collect: Collect.\n"
                "        items:\n          ui: true\n",
            )
        )
    with pytest.raises(ConfigurationError, match="per-item stages only"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                "workflows:\n  - task: ~\n    steps:\n      - discuss: Talk.\n"
                "        interactive: true\n        ui: true\n",
            )
        )
    with pytest.raises(ConfigurationError, match="ui must be true or false"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                "workflows:\n  - task: ~\n    steps:\n      - collect: Collect.\n"
                "        items:\n          interactive: true\n"
                "          ui: yes please\n",
            )
        )


def test_one_stage_per_item_flow_is_answered_on_the_page(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="found ui: true on verify, report"):
        load_configuration(
            _write(
                tmp_path / "workflows.yaml",
                """workflows:
  - name: review
    steps:
      - name: review
        description: Review.
        items:
          steps:
            - name: verify
              description: Verify it.
              interactive: true
              ui: true
            - name: report
              description: Report it.
              interactive: true
              ui: true
""",
            )
        )


def test_idempotent_is_declared_where_the_command_is(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "workflows.yaml",
            """handlers:
  - name: tests
    argv: [pytest, -q]
    idempotent: true
workflows:
  - name: task
    steps:
      - lint:
        shell: ruff check src
        idempotent: true
      - verify: ~
        handler: tests
      - plain:
        argv: [printf, ready]
""",
        )
    )

    assert configuration.handlers[0].action.payload.idempotent
    lint, verify, plain = configuration.workflows[0].steps
    assert lint.action.payload.idempotent
    # A referencing step inherits the declaration with the command.
    assert verify.action.payload.idempotent
    assert not plain.action.payload.idempotent


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (
            "  - name: announce\n    description: Tell them.\n    idempotent: true\n",
            "idempotent require argv or shell",
        ),
        (
            "  - name: tests\n    argv: [pytest]\n    idempotent: always\n",
            "idempotent must be a boolean",
        ),
    ],
)
def test_idempotent_needs_a_command_and_a_boolean(
    tmp_path: Path, handler: str, message: str
) -> None:
    workflows = "workflows:\n  - name: task\n    steps:\n      - work: Work.\n"
    with pytest.raises(ConfigurationError, match=message):
        load_configuration(
            _write(tmp_path / "workflows.yaml", f"handlers:\n{handler}{workflows}")
        )
