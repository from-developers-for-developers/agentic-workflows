# SPDX-License-Identifier: GPL-3.0-or-later
"""Parsing and validating the ww.yaml workflow configuration."""

from dataclasses import replace
from pathlib import Path

import pytest

from tests.config_helpers import task_steps, task_workflow
from ww.actions import Commands, Prompt
from ww.config import load_configuration, parse_yaml_configuration, parse_yaml_text
from ww.errors import ConfigurationError
from ww.operations import ChildWorkflowRun, WorkflowHandoff
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
        tmp_path / "ww.yaml",
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
            tmp_path / "ww.yaml",
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


def test_rejects_nested_workflows_at_yaml_boundary(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "ww.yaml",
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
        tmp_path / "ww.yaml",
        """workflows:
  - name: task
    steps:
      - name: consume
        artifact_from: produce
      - name: produce
""",
    )

    with pytest.raises(ConfigurationError, match="not an earlier step"):
        load_configuration(path)


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param(
            """      - name: group
        steps:
          - name: consume
            artifact_from: produce
      - name: produce
""",
            id="later-upper-level-step",
        ),
        pytest.param(
            """      - name: review
        loop:
          - name: consume
            artifact_from: review
            break: Done
""",
            id="running-loop",
        ),
        pytest.param(
            """      - name: group
        steps:
          - name: inner
            steps:
              - name: consume
                artifact_from: inner
""",
            id="enclosing-group",
        ),
        pytest.param(
            """      - assess:
          question: Is it good?
          outcomes:
            positive:
              handler: ship
            negative:
              steps:
                - name: consume
                  artifact_from: positive
""",
            id="other-outcome",
        ),
    ],
)
def test_depends_on_rejects_steps_that_have_not_run(tmp_path: Path, steps: str) -> None:
    path = _write(
        tmp_path / "ww.yaml",
        "handlers:\n  - name: ship\n    description: Ship it.\n    kind: prompt\n"
        "workflows:\n  - name: task\n    steps:\n" + steps,
    )

    with pytest.raises(ConfigurationError, match="not an earlier step"):
        load_configuration(path)


@pytest.mark.parametrize(
    ("container", "name"),
    [
        pytest.param(
            """      - name: group
        steps:
          - name: work
            artifact: false
          - name: inner
            steps:
              - name: note
                artifact: false
""",
            "group",
            id="group",
        ),
        pytest.param(
            """      - assess:
          question: Is it good?
          artifact: false
          outcomes:
            positive:
              steps:
                - name: note
                  artifact: false
            negative:
              stop_workflow: true
""",
            "assess",
            id="assessment",
        ),
    ],
)
def test_artifact_from_rejects_a_container_where_nothing_saves_an_artifact(
    tmp_path: Path, container: str, name: str
) -> None:
    path = _write(
        tmp_path / "ww.yaml",
        "workflows:\n  - name: task\n    steps:\n"
        + container
        + f"      - name: consume\n        artifact_from: {name}\n",
    )

    with pytest.raises(ConfigurationError, match="does not produce an artifact"):
        load_configuration(path)


def test_artifact_from_accepts_a_group_or_assessment_with_an_artifact_inside(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """handlers:
  - name: analyse
    steps:
      - assess:
          question: Is the cause clear?
          artifact: false
          outcomes:
            positive:
              steps:
                - investigate: Investigate.
            negative:
              steps:
                - research: Research.
workflows:
  - name: task
    steps:
      - name: analysis
        handler: analyse
      - name: develop
        artifact_from: analysis
      - name: group
        steps:
          - name: loop-inside
            artifact: false
            loop:
              - name: round
                break: Done.
      - name: review
        artifact_from: group
""",
        )
    )

    steps = configuration.workflows[0].steps
    assert [step.artifact_dependency for step in steps] == [
        None,
        "analysis",
        None,
        "group",
    ]


def test_parses_normalized_records_and_command_forms(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "ww.yaml",
        """modes:
  - name: economy
    description: Use fewer tokens.
handlers:
  - name: check
    argv: [printf, second]
    assert: [{equals: second}]
  - name: write
    argv: [printf, hello]
  - name: ask
    kind: prompt
    description: Explain the work.
workflows:
  - name: task
    modes: [economy]
    steps:
      - name: prepare
        variables:
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
        ("printf", "second"),
    )
    assert check.action.payload.assertion is not None
    assert check.action.payload.assertion.to_data() == [{"equals": "second"}]
    assert write.action.payload.commands[0].argv == ("printf", "hello")
    assert ask.action.identifier == "prompt"
    assert configuration.workflows[0].steps[0].provide[0].name == "decision"


def test_modes_accept_named_entry_shorthand(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
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
            tmp_path / "ww.yaml",
            """workflows:
  - name: task
    steps:
      - name: create-issue
        description: Create an issue.
        saves:
          - metadata.integrations.jira.issue_id: The created Jira issue ID.
""",
        )
    )

    saved = configuration.workflows[0].steps[0].save_metadata[0]
    assert (saved.name, saved.key, saved.description) == (
        "integrations.jira.issue_id",
        "integrations.jira.issue_id",
        "The created Jira issue ID.",
    )


def test_parses_project_metadata_scope(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """workflows:
  - name: task
    steps:
      - name: discover
        saves:
          - project_metadata.environments.staging.url: ~
""",
        )
    )

    assert configuration.workflows[0].steps[0].save_metadata[0].scope == "project"


def test_expands_named_entry_shorthand_for_variables_and_saves(
    tmp_path: Path,
) -> None:
    configuration = parse_yaml_configuration(
        _write(
            tmp_path / "ww.yaml",
            """workflows:
  - name: task
    steps:
      - name: choose
        variables:
          - workflow: The selected workflow.
          - reason: ~
        saves:
          - project_metadata.last_auto_refactored_at: Save the current timestamp.
          - metadata.task.result: ~
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
            "project_metadata.last_auto_refactored_at",
            "last_auto_refactored_at",
            "Save the current timestamp.",
            "project",
        ),
        SavedMetadata("task.result", "task.result", "", "task"),
    )


def test_explicit_name_preserves_variables_and_saves_long_form(
    tmp_path: Path,
) -> None:
    configuration = parse_yaml_configuration(
        _write(
            tmp_path / "ww.yaml",
            """workflows:
  - name: task
    steps:
      - name: work
        variables:
          - description: Existing provided value.
            name: result
        saves:
          - description: Existing metadata value.
            name: metadata.task.result
""",
        )
    )

    step = configuration.workflows[0].steps[0]
    assert step.provide[0] == ProvidedVariable("result", "Existing provided value.")
    assert step.save_metadata[0] == SavedMetadata(
        "task.result", "task.result", "Existing metadata value.", "task"
    )


def test_allows_the_same_metadata_path_in_different_scopes(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """workflows:
  - name: task
    steps:
      - name: discover
        saves:
          - metadata.environment.url: ~
          - project_metadata.environment.url: ~
""",
        )
    )

    assert len(configuration.workflows[0].steps[0].save_metadata) == 2


@pytest.mark.parametrize("key", ("metadata/foo", "foo:bar", ".foo", "foo."))
def test_rejects_non_dotted_task_metadata_keys(tmp_path: Path, key: str) -> None:
    path = _write(
        tmp_path / "ww.yaml",
        f"""workflows:
  - name: task
    steps:
      - name: work
        saves:
          - metadata.{key}: ~
""",
    )

    with pytest.raises(ConfigurationError, match="dotted metadata path"):
        load_configuration(path)


def test_parses_structured_argv_and_explicit_shell_actions(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """handlers:
  - name: run
    shell: printf '%s' "$VALUE" > result.txt
    env:
      VALUE: "{{value}}"
    args: ["{{value}}"]
  - name: run-argv
    argv: [printf, "%s", "{{value}}"]
workflows:
  - name: task
    steps:
      - name: work
""",
        )
    )

    (shell,) = configuration.handlers[0].action.payload.commands
    (argv,) = configuration.handlers[1].action.payload.commands
    assert argv.argv == ("printf", "%s", "{{value}}")
    assert shell.shell == "printf '%s' \"$VALUE\" > result.txt"
    assert shell.env == (("VALUE", "{{value}}"),)
    assert shell.args == ("{{value}}",)


def test_parses_explicit_inline_handler_command(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """hooks:
  before_start_workflow:
    - argv: [printf, ready]
workflows:
  - name: task
    steps:
      - name: work
""",
        )
    )

    handler = configuration.global_hooks[0].handler
    assert not isinstance(handler, str)
    assert handler.name == "inline-argv"
    assert handler.action.payload.commands[0].argv == ("printf", "ready")


def test_normalizes_root_step_and_hook_handler_syntax(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
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


def test_rejects_an_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigurationError, match="kind must be one of: skill, slash_command, prompt"
    ):
        load_configuration(
            _write(
                tmp_path / "ww.yaml",
                """workflows:
  - task: ~
    steps:
      - name: work
        kind: mcp
""",
            )
        )


def test_step_handler_copies_a_catalog_handler_with_step_identity(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """handlers:
  - handler_name: Run the shared check.
    argv: [printf, ready]
    variables:
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
            tmp_path / "ww.yaml",
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
            tmp_path / "ww.yaml",
            """handlers:
  - name: ask
    kind: prompt
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
            tmp_path / "ww.yaml",
            """handlers:
  - name: ask
    kind: prompt
    description: Original instruction.
workflows:
  - name: task
    steps:
      - name: ask-again
        handler: ask
        kind: prompt
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
            tmp_path / "ww.yaml",
            """handlers:
  - name: ask
    kind: prompt
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
            tmp_path / "ww.yaml",
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
        tmp_path / "ww.yaml",
        """handlers:
  - name: shared
    kind: prompt
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
            tmp_path / "ww.yaml",
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
    assert all(hook.steps.admits_all for hook in configuration.global_hooks)


@pytest.mark.parametrize("value", ("[]", "{}", "true"))
def test_rejects_non_string_shorthand_descriptions(tmp_path: Path, value: str) -> None:
    path = _write(
        tmp_path / "ww.yaml",
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
            tmp_path / "ww.yaml",
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
            tmp_path / "ww.yaml",
            """handlers:
  - name: update-architecture
    kind: prompt
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
    assert all(hook.workflows.names == ("task",) for hook in hooks)
    assert all(hook.steps.names == ("develop",) for hook in hooks)
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
            tmp_path / "ww.yaml",
            """hooks:
  before_start:
    - steps: [plan-and-fix/fix]
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

    assert configuration.global_hooks[0].steps.names == ("plan-and-fix/fix",)


_FILTERED_HOOK = """hooks:
  before_start:
    - name: prepare
      workflows: {workflows}
      steps: {steps}
      description: Get ready.
workflows:
  - name: task
    steps:
      - name: develop
      - name: review
  - name: bugfix
    steps:
      - name: develop
"""


@pytest.mark.parametrize("value", ['"*"', "[]"])
def test_a_hook_filter_of_star_or_empty_admits_everything(
    tmp_path: Path, value: str
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            _FILTERED_HOOK.format(workflows=value, steps=value),
        )
    )

    hook = configuration.global_hooks[0]
    assert hook.workflows.admits_all and hook.steps.admits_all
    assert hook.applies_to("bugfix", "develop", "develop")
    assert hook.applies_to("task", "review", "review")


def test_a_hook_filter_list_admits_only_its_names(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            _FILTERED_HOOK.format(workflows="[task]", steps='"*"'),
        )
    )

    hook = configuration.global_hooks[0]
    assert hook.workflows.names == ("task",)
    assert hook.applies_to("task", "review", "review")
    assert not hook.applies_to("bugfix", "develop", "develop")


@pytest.mark.parametrize(
    ("workflows", "steps", "message"),
    [
        ('["*", task]', '"*"', r'workflows cannot mix "\*" with names'),
        ('"*"', '["*", develop]', r'steps cannot mix "\*" with names'),
        ("task", '"*"', r"workflows must be \"\*\" or a list of names \(write \[task"),
        ('"*"', "develop", r"steps must be \"\*\" or a list of names"),
        ('"*"', "7", r"steps must be \"\*\" or a list of names"),
        ('["1task"]', '"*"', "workflows must be .* normalized names"),
    ],
)
def test_a_hook_filter_rejects_a_bare_name_and_star_among_names(
    tmp_path: Path, workflows: str, steps: str, message: str
) -> None:
    path = _write(
        tmp_path / "ww.yaml",
        _FILTERED_HOOK.format(workflows=workflows, steps=steps),
    )

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(path)


def test_a_star_filter_is_not_accepted_where_the_hook_takes_no_filter(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "ww.yaml",
        """hooks:
  before_start_workflow:
    - name: prepare
      steps: "*"
      description: Get ready.
workflows:
  - name: task
    steps:
      - name: develop
""",
    )

    with pytest.raises(ConfigurationError, match="steps"):
        load_configuration(path)


def test_shell_source_cannot_interpolate_workflow_values(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="shell source cannot interpolate"):
        load_configuration(
            _write(
                tmp_path / "ww.yaml",
                """handlers:
  - name: unsafe
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
        kind: skill
        argv: [printf, ok]
""",
            "cannot combine a command and kind",
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
    argv: [printf, ok]
    assert: [{ne: ok}]
workflows:
  - name: task
    steps:
      - name: work
""",
            "unknown key",
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
def test_rejects_ambiguous_schema(tmp_path: Path, content: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_configuration(_write(tmp_path / "ww.yaml", content))


def test_hook_filters_are_scoped_and_transition_accepts_interpolation(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "ww.yaml",
        """hooks:
  after_complete:
    - workflows: [chooser]
      steps: [decide]
      name: document
handlers:
  - name: document
    kind: prompt
workflows:
  - name: chooser
    steps:
      - name: decide
        variables:
          - name: workflow
        hooks:
          after_complete:
            - handoff_to: "{{workflow}}"
""",
    )

    configuration = load_configuration(path)
    transition = configuration.workflows[0].steps[0].hooks[0].handler
    assert transition.operation is not None
    assert transition.operation.target == "{{workflow}}"
    assert configuration.global_hooks[0].workflows.names == ("chooser",)


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
    kind: prompt
workflows:
  - name: task
    steps:
      - name: work
"""

    with pytest.raises(ConfigurationError, match="unknown key.*steps"):
        load_configuration(_write(tmp_path / "ww.yaml", content))


def test_parses_mcp_handlers_and_direct_hook_declarations(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "ww.yaml",
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
            tmp_path / "ww.yaml",
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


def test_parses_step_role(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """workflows:
  - name: task
    role: manager
    steps:
      - name: local-work
        role: worker
      - name: inherits
""",
        )
    )

    workflow = configuration.workflows[0]
    assert workflow.role == "manager"
    assert workflow.steps[0].role == "worker"
    assert workflow.steps[1].role is None

    with pytest.raises(ConfigurationError, match="role must be manager or worker"):
        load_configuration(
            _write(
                tmp_path / "invalid.yaml",
                """workflows:
  - name: task
    steps:
      - name: local-work
        role: operator
""",
            )
        )


def test_subagents_is_a_boolean_that_steps_items_and_workflows_inherit(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """workflows:
  - name: task
    subagents: false
    steps:
      - name: local-work
        subagents: true
      - name: inherits
""",
        )
    )

    workflow = configuration.workflows[0]
    assert workflow.subagents is False
    assert workflow.steps[0].subagents is True
    assert workflow.steps[1].subagents is None

    with pytest.raises(ConfigurationError, match="subagents must be true or false"):
        load_configuration(
            _write(
                tmp_path / "invalid.yaml",
                """workflows:
  - name: task
    steps:
      - name: local-work
        subagents: never
""",
            )
        )


def test_an_interactive_step_cannot_be_a_workers(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="role: worker contradicts it"):
        load_configuration(
            _write(
                tmp_path / "ww.yaml",
                """workflows:
  - name: task
    steps:
      - name: talk
        interactive: true
        role: worker
""",
            )
        )


def test_parses_loop_wrapper_with_ordinary_nested_steps(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        max_rounds: 5
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
    assert wrapper.max_rounds == 5
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
            tmp_path / "ww.yaml",
            """handlers:
  - handle_tests:
    assignment: per_step
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
        ("max_rounds: 3", "max_rounds requires a loop"),
        ("assignment: per_step", "assignment on a step goes beside a loop"),
        (
            "assignment: together\n        loop:\n          - work: Do it.",
            "assignment must be one of: per_round, per_step",
        ),
        (
            "max_rounds: 0\n        loop:\n          - work: Do it.",
            "max_rounds must be a positive integer",
        ),
        (
            "max_rounds: true\n        loop:\n          - work: Do it.",
            "max_rounds must be a positive integer",
        ),
    ],
)
def test_rejects_incomplete_loop_syntax(
    tmp_path: Path, body: str, message: str
) -> None:
    path = _write(
        tmp_path / "ww.yaml",
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
        tmp_path / "ww.yaml",
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
        tmp_path / "ww.yaml",
        """workflows:
  - name: choose
    steps:
      - select: Hand off.
        handoff_to: target
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
            "  - name: choose\n    steps:\n"
            "      - go: ~\n        handoff_to: target\n      - after: Never.\n",
            "must place its handoff_to step 'go' last",
        ),
        (
            "  - name: choose\n    steps:\n"
            "      - go: ~\n        handoff_to: target\n"
            "      - again: ~\n        handoff_to: target\n",
            "more than one handoff_to",
        ),
        (
            "  - name: choose\n    steps:\n      - group: Group.\n        steps:\n"
            "          - go: ~\n            handoff_to: target\n",
            "must place its handoff_to step 'go' last",
        ),
        (
            "  - name: choose\n    steps:\n      - pick: Pick.\n        hooks:\n"
            "          after_complete:\n            - handoff_to: target\n"
            "      - after: Never.\n",
            "must place its handoff_to hook in the after_complete hooks of its "
            "last step 'after'",
        ),
        (
            "  - name: choose\n    steps:\n      - pick: Pick.\n        hooks:\n"
            "          before_complete:\n            - handoff_to: target\n",
            "must place its handoff_to hook in the after_complete hooks of its "
            "last step 'pick'",
        ),
        (
            "  - name: choose\n    steps:\n      - pick: Pick.\n        hooks:\n"
            "          after_complete:\n            - handoff_to: target\n"
            "            - handlers:\n                - note: Too late.\n",
            "handoff workflow 'choose' must end with its handoff_to",
        ),
        (
            "  - name: choose\n    hooks:\n      after_complete:\n"
            "        - handoff_to: target\n    steps:\n      - pick: Pick.\n",
            "is a handoff_to transition at workflow scope",
        ),
    ],
)
def test_misplaced_transitions_are_rejected_when_loading(
    tmp_path: Path, workflow: str, message: str
) -> None:
    path = _write(
        tmp_path / "ww.yaml",
        "workflows:\n"
        + workflow
        + "  - name: target\n    steps:\n      - work: Work.\n",
    )
    with pytest.raises(ConfigurationError, match=message):
        load_configuration(path)


def test_a_global_transition_hook_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "ww.yaml",
        """hooks:
  after_complete:
    - workflows: [choose]
      handoff_to: target
workflows:
  - name: choose
    steps:
      - pick: Pick.
  - name: target
    steps:
      - work: Work.
""",
    )
    with pytest.raises(ConfigurationError, match="transition at global scope"):
        load_configuration(path)


def test_a_global_hook_after_a_transition_step_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "ww.yaml",
        """hooks:
  after_complete:
    - handlers:
        - note: Note it.
workflows:
  - name: choose
    steps:
      - go: ~
        handoff_to: target
  - name: target
    steps:
      - work: Work.
""",
    )
    with pytest.raises(ConfigurationError, match="must end with its handoff_to"):
        load_configuration(path)


def test_yaml_text_parses_like_a_file(tmp_path: Path) -> None:
    text = "workflows:\n  - name: task\n    steps:\n      - work: Work.\n"

    parsed_file = parse_yaml_configuration(_write(tmp_path / "ww.yaml", text))
    assert parse_yaml_text(text) == replace(parsed_file, workflow_provenance={})
    assert parsed_file.workflow_provenance["task"].source == "ww.yaml"
    with pytest.raises(ConfigurationError, match="invalid YAML in scenario: "):
        parse_yaml_text("workflows: [", "scenario")
    with pytest.raises(ConfigurationError, match="workflow configuration not found"):
        parse_yaml_configuration(tmp_path / "missing.yaml")


def test_save_metadata_append_is_parsed(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """workflows:
  - task: ~
    steps:
      - report: Report the outcome.
        saves:
          - metadata.pull_request.handled: Root comment ids handled.
            append: true
          - metadata.pull_request.url: The pull request URL.
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
                tmp_path / "ww.yaml",
                """workflows:
  - task: ~
    steps:
      - report: Report.
        saves:
          - metadata.handled: Ids.
            append: yes please
""",
            )
        )


def test_documents_are_declared_at_the_root_and_updated_by_steps(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """documents:
  - test_cases: The test cases derived from the issue, kept current across runs.
  - conventions: Team conventions every task follows.
    scope: project
workflows:
  - task: ~
    steps:
      - derive: Derive the test cases.
        saves:
          - documents.test_cases: Save every test case as a checklist item.
      - report: Build the report from {{ww.documents.test_cases}}.
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
            "documents\\[0\\].scope must be 'task', 'project', or 'user'",
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
                tmp_path / "ww.yaml",
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
                tmp_path / "ww.yaml",
                """workflows:
  - task: ~
    steps:
      - derive: Derive.
        saves:
          - documents.test_cases: Save them.
""",
            )
        )


def test_a_save_metadata_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="save_metadata"):
        load_configuration(
            _write(
                tmp_path / "ww.yaml",
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
            tmp_path / "ww.yaml",
            """handlers:
  - fetch_requirements: Fetch the issue and its comments.
    mcp: tracker
    variables:
      - issue_key: The issue key.
  - skim: Skim the issue.
    mcp: tracker
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
            tmp_path / "ww.yaml",
            """documents:
  - notes: Notes on the branch.
    path: documentation/issues/{{ww.task.id}}/notes.md
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
    assert notes.path == "documentation/issues/{{ww.task.id}}/notes.md"
    assert glossary.path == "docs/glossary.md"
    assert notes.to_dict()["path"] == notes.path


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ("    path: /etc/notes.md\n", "must stay inside the project"),
        ("    path: ../notes.md\n", "must stay inside the project"),
        (
            "    scope: project\n    path: docs/{{ww.task.id}}.md\n",
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
                tmp_path / "ww.yaml",
                "documents:\n  - notes: Notes.\n"
                + declaration
                + "workflows:\n  - task: ~\n    steps:\n      - work: Work.\n",
            )
        )


def test_interactive_is_parsed_inherited_and_folded_into_items(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
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
    with pytest.raises(
        ConfigurationError, match="interactive must be true, false, or page"
    ):
        load_configuration(
            _write(
                tmp_path / "ww.yaml",
                "workflows:\n  - task: ~\n    steps:\n      - a: A.\n"
                "        interactive: maybe\n",
            )
        )


def test_explicit_is_inherited_from_a_reusable_step_handler(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """handlers:
  - name: inspect
    description: Inspect the change.
    explicit: true
  - name: plain-group
    steps:
      - child: Child work.
workflows:
  - name: task
    steps:
      - name: first
        handler: inspect
        explicit: false
      - name: second
        handler: inspect
""",
        )
    )

    first, second = configuration.workflows_by_name["task"].steps
    assert first.explicit is False
    assert second.explicit is True
    assert configuration.handlers_by_name["plain-group"].explicit is False


def test_reusing_an_interactive_structural_handler_does_not_bypass_validation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="cannot be interactive"):
        load_configuration(
            _write(
                tmp_path / "ww.yaml",
                """handlers:
  - name: group
    interactive: true
    role: manager
    steps:
      - actual: Actual work.
workflows:
  - task: ~
    steps:
      - name: reused
        handler: group
""",
            )
        )


def test_a_workflow_may_declare_its_runtime(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """workflows:
  - testing: Manual testing.
    runtime: single
    steps:
      - test: Test it.
  - task: ~
    steps:
      - work: Work.
""",
        )
    )
    testing, task, _catchall = configuration.workflows
    assert (testing.runtime, task.runtime) == ("single", None)
    with pytest.raises(ConfigurationError, match="runtime must be one of: single"):
        load_configuration(
            _write(
                tmp_path / "ww.yaml",
                "workflows:\n  - task: ~\n    runtime: parallel\n    steps:\n"
                "      - work: Work.\n",
            )
        )


def test_choices_are_labels_with_descriptions_and_need_an_interactive_step(
    tmp_path: Path,
) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
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
                tmp_path / "ww.yaml",
                "workflows:\n  - task: ~\n    steps:\n      - verify: Verify.\n"
                "        choices:\n          - pass: Passed.\n",
            )
        )


def test_interactive_page_is_declared_on_per_item_stages_only(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
            """workflows:
  - name: manual
    steps:
      - name: collect
        description: Collect the cases.
        items:
          interactive: page
  - name: review
    steps:
      - name: review
        description: Review.
        items:
          steps:
            - name: verify
              description: Verify it.
              interactive: page
            - name: report
              description: Report it.
""",
        )
    )
    (collect,), (review,) = (flow.steps for flow in configuration.workflows[:2])
    assert collect.items is not None and collect.items.steps[0].ui is True
    assert review.items is not None
    assert [stage.ui for stage in review.items.steps] == [True, False]
    with pytest.raises(ConfigurationError, match="per-item stages only"):
        load_configuration(
            _write(
                tmp_path / "ww.yaml",
                "workflows:\n  - task: ~\n    steps:\n      - discuss: Talk.\n"
                "        interactive: page\n",
            )
        )
    with pytest.raises(
        ConfigurationError, match="interactive must be true, false, or page"
    ):
        load_configuration(
            _write(
                tmp_path / "ww.yaml",
                "workflows:\n  - task: ~\n    steps:\n      - collect: Collect.\n"
                "        items:\n          interactive: yes please\n",
            )
        )


def test_one_stage_per_item_flow_is_answered_on_the_page(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigurationError, match="found interactive: page on verify, report"
    ):
        load_configuration(
            _write(
                tmp_path / "ww.yaml",
                """workflows:
  - name: review
    steps:
      - name: review
        description: Review.
        items:
          steps:
            - name: verify
              description: Verify it.
              interactive: page
            - name: report
              description: Report it.
              interactive: page
""",
            )
        )


def test_idempotent_is_declared_where_the_command_is(tmp_path: Path) -> None:
    configuration = load_configuration(
        _write(
            tmp_path / "ww.yaml",
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
        config_path = tmp_path / "ww.yaml"
        load_configuration(_write(config_path, f"handlers:\n{handler}{workflows}"))


def test_assignment_takes_the_values_of_its_construct() -> None:
    (loop,) = task_steps(
        task_workflow(
            "      - name: fix\n        loop: [{work: Do it.}]\n"
            "        max_rounds: 4\n        assignment: per_step\n"
        )
    )
    (collect,) = task_steps(
        task_workflow(
            "      - collect:\n        items:\n          assignment: per_item\n"
        )
    )

    assert (loop.max_rounds, loop.loop_assignment) == (4, "per_step")
    assert collect.items is not None and collect.items.assignment == "per_item"
    with pytest.raises(ConfigurationError, match="must be one of: per_round, per_step"):
        parse_yaml_text(
            task_workflow(
                "      - name: fix\n        loop: [{work: Do it.}]\n"
                "        assignment: per_item\n"
            )
        )
    with pytest.raises(ConfigurationError, match="goes beside a loop"):
        parse_yaml_text(
            task_workflow("      - work: Work.\n        assignment: per_step\n")
        )


def test_kind_chooses_the_agent_action() -> None:
    (skill, prompt) = task_steps(
        task_workflow(
            "      - review-code:\n        kind: skill\n"
            "      - name: write\n        kind: prompt\n        description: Write.\n"
        )
    )

    assert skill.action is not None and skill.action.identifier == "skill"
    assert prompt.action is not None and prompt.action.identifier == "prompt"
    with pytest.raises(ConfigurationError, match="kind must be one of"):
        parse_yaml_text(task_workflow("      - work:\n        kind: mcp\n"))
    with pytest.raises(ConfigurationError, match="cannot combine a command and kind"):
        parse_yaml_text(
            task_workflow("      - work:\n        kind: skill\n        argv: [make]\n")
        )


def test_handoff_to_is_the_transition_and_workflow_runs_a_child() -> None:
    configuration = parse_yaml_text(
        task_workflow(
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
            task_workflow(
                "      - split:\n        children:\n          steps:\n"
                "            - implement: {workflow: other}\n"
                "            - go:\n              handoff_to: other\n"
                "  - name: other\n    steps:\n      - work: Work.\n"
            )
        )


def test_saves_names_prefixed_paths() -> None:
    (step,) = task_steps(
        "documents:\n  - plan: The plan.\n"
        + task_workflow(
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
            + task_workflow(
                f"      - work: Work.\n        saves:\n          - {entry}\n"
            )
        )
