# SPDX-License-Identifier: GPL-3.0-or-later
"""Semantic validation shared by parsed and programmatic workflows."""

from dataclasses import replace
from pathlib import Path

import pytest

from ww.actions import DefinedAction, Prompt
from ww.builtin_workflows import builtin_workflows, with_builtin_workflows
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.plan import compile_workflow_plan
from ww.project_config import ProjectConfig
from ww.workflow_config import (
    HandlerDefinition,
    HookDefinition,
    ModeDefinition,
    NameFilter,
    StepDefinition,
    WorkflowConfiguration,
    WorkflowDefinition,
)
from ww.workflow_validation import validate_configuration


def _configuration(**changes: object) -> WorkflowConfiguration:
    values = {
        "modes": (),
        "profiles": (),
        "handlers": (),
        "global_hooks": (),
        "workflows": (WorkflowDefinition("task", steps=(StepDefinition("work"),)),),
        **changes,
    }
    return WorkflowConfiguration(**values)  # type: ignore[arg-type]


def test_shared_validator_rejects_invalid_programmatic_definitions() -> None:
    configuration = _configuration(
        modes=(ModeDefinition("same"), ModeDefinition("same"))
    )

    with pytest.raises(ConfigurationError, match="duplicate mode"):
        validate_configuration(configuration)


def test_compiler_always_enforces_shared_semantic_validation(tmp_path: Path) -> None:
    configuration = _configuration(
        workflows=(
            WorkflowDefinition(
                "task",
                modes=("missing",),
                steps=(StepDefinition("work"),),
            ),
        )
    )

    with pytest.raises(ConfigurationError, match="unknown mode"):
        compile_workflow_plan(configuration, tmp_path, "task", "codex")


def test_init_is_a_reserved_step_name() -> None:
    configuration = _configuration(
        workflows=(
            WorkflowDefinition(
                "task",
                steps=(StepDefinition("init"), StepDefinition("work")),
            ),
        )
    )

    with pytest.raises(ConfigurationError, match="'init' is reserved"):
        validate_configuration(configuration)


@pytest.mark.parametrize(
    "container",
    [
        StepDefinition("group", child_steps=(StepDefinition("child"),)),
    ],
)
def test_interactive_is_rejected_on_pure_structural_containers(
    container: StepDefinition,
) -> None:
    configuration = _configuration(
        workflows=(
            WorkflowDefinition("task", steps=(replace(container, interactive=True),)),
        )
    )

    with pytest.raises(ConfigurationError, match="cannot be interactive"):
        validate_configuration(configuration)


def test_interactive_item_collector_remains_valid() -> None:
    from ww.workflow_config import ItemFlow

    configuration = _configuration(
        workflows=(
            WorkflowDefinition(
                "task",
                steps=(StepDefinition("collect", interactive=True, items=ItemFlow()),),
            ),
        )
    )

    validate_configuration(configuration)


def test_rejects_workflow_boundary_hook_filtered_by_step() -> None:
    configuration = _configuration(
        global_hooks=(
            HookDefinition(
                "before_start_workflow",
                HandlerDefinition(
                    "prepare", action=DefinedAction("prompt", Prompt("Prepare."))
                ),
                steps=NameFilter.of(("develop",)),
                path="hooks.before_start_workflow[0]",
            ),
        ),
        workflows=(
            WorkflowDefinition(
                "task",
                steps=(StepDefinition("fetch"), StepDefinition("develop")),
            ),
        ),
    )

    with pytest.raises(ConfigurationError, match="cannot filter.*boundary by step"):
        validate_configuration(configuration)


def test_global_workflow_boundary_hook_may_filter_by_workflow() -> None:
    configuration = _configuration(
        global_hooks=(
            HookDefinition(
                "before_start_workflow",
                HandlerDefinition(
                    "prepare", action=DefinedAction("prompt", Prompt("Prepare."))
                ),
                workflows=NameFilter.of(("delivery",)),
            ),
        ),
        workflows=(
            WorkflowDefinition(
                "research", steps=(StepDefinition("fetch"), StepDefinition("develop"))
            ),
            WorkflowDefinition("delivery", steps=(StepDefinition("develop"),)),
        ),
    )

    validated = validate_configuration(configuration)
    assert validated.workflows[:2] == configuration.workflows


def test_rejects_step_local_before_start_hook() -> None:
    configuration = _configuration(
        workflows=(
            WorkflowDefinition(
                "task",
                steps=(
                    StepDefinition(
                        "develop",
                        hooks=(
                            HookDefinition(
                                "before_start_workflow",
                                HandlerDefinition(
                                    "prepare",
                                    action=DefinedAction("prompt", Prompt("Prepare.")),
                                ),
                                scope="step",
                                path="workflows[0].steps[0].hooks.before_start_workflow[0]",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(ConfigurationError, match="workflow boundary hooks belong"):
        validate_configuration(configuration)


@pytest.mark.usefixtures("shipped_builtins")
def test_the_builtin_workflows_follow_the_configured_workflows() -> None:
    validated = validate_configuration(_configuration())

    assert [workflow.name for workflow in validated.workflows] == [
        "task",
        *(workflow.name for workflow in builtin_workflows()),
    ]
    # A project that configures no workflow still has ww's own.
    assert validate_configuration(_configuration(workflows=())).workflows == (
        builtin_workflows()
    )


@pytest.mark.usefixtures("shipped_builtins")
def test_a_configured_workflow_replaces_the_builtin_one_of_its_name() -> None:
    own = WorkflowDefinition("ww-suggest", steps=(StepDefinition("record"),))

    validated = validate_configuration(_configuration(workflows=(own,)))

    assert own in validated.workflows
    assert [workflow.name for workflow in validated.workflows].count("ww-suggest") == 1


@pytest.mark.usefixtures("shipped_builtins")
def test_a_switched_off_builtin_workflow_is_not_added() -> None:
    configuration = _configuration()
    config = ProjectConfig(
        disabled_workflows=frozenset(workflow.name for workflow in builtin_workflows())
    )

    assert with_builtin_workflows(configuration, config) == configuration


STEP_TREE_HANDLER = """handlers:
  - run-tests:
    steps:
      - test: Run the tests and fix the failures.

"""


@pytest.mark.parametrize(
    "hooked",
    [
        # Global.
        """hooks:
  before_complete_workflow:
    - handlers:
        - run-tests: ~
workflows:
  - name: task
    steps:
      - develop: Develop.
""",
        # Workflow.
        """workflows:
  - name: task
    hooks:
      before_complete_workflow:
        - handlers:
            - run-tests: ~
    steps:
      - develop: Develop.
""",
        # Step.
        """workflows:
  - name: task
    steps:
      - develop: Develop.
        hooks:
          after_complete:
            - handlers:
                - run-tests: ~
""",
    ],
    ids=["global", "workflow", "step"],
)
def test_a_hook_may_not_run_a_handler_that_is_a_step_tree(
    tmp_path: Path, hooked: str
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(STEP_TREE_HANDLER + hooked, encoding="utf-8")

    # A hook cannot run an agent step tree; an ordinary step can.
    with pytest.raises(ConfigurationError, match="use 'run-tests' as a workflow step"):
        load_configuration(path)
    path.write_text(
        STEP_TREE_HANDLER
        + "workflows:\n  - name: task\n    steps:\n      - run-tests: ~\n",
        encoding="utf-8",
    )
    assert load_configuration(path).workflows[0].steps[0].child_steps
