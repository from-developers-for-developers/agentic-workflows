# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from ww.actions import DefinedAction, Prompt
from ww.core_workflows import CATCHALL_WORKFLOW, with_core_workflows
from ww.errors import ConfigurationError
from ww.plan import compile_workflow_plan
from ww.project_config import ProjectConfig
from ww.workflow_config import (
    HandlerDefinition,
    HookDefinition,
    ModeDefinition,
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


def test_rejects_workflow_boundary_hook_filtered_by_step() -> None:
    configuration = _configuration(
        global_hooks=(
            HookDefinition(
                "before_start_workflow",
                HandlerDefinition(
                    "prepare", action=DefinedAction("prompt", Prompt("Prepare."))
                ),
                step_names=("develop",),
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
                workflow_names=("delivery",),
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


def test_the_core_catchall_follows_the_configured_workflows() -> None:
    validated = validate_configuration(_configuration())

    assert [workflow.name for workflow in validated.workflows] == ["task", "catchall"]
    assert validated.workflows[-1] == CATCHALL_WORKFLOW
    # With the catch-all, a project that configures no workflow can still
    # record its changes.
    assert validate_configuration(_configuration(workflows=())).workflows == (
        CATCHALL_WORKFLOW,
    )


def test_a_configured_catchall_replaces_the_core_one() -> None:
    own = WorkflowDefinition("catchall", steps=(StepDefinition("record"),))

    validated = validate_configuration(_configuration(workflows=(own,)))

    assert validated.workflows == (own,)


def test_a_switched_off_core_workflow_is_not_added() -> None:
    configuration = _configuration()
    config = ProjectConfig(disabled_workflows=frozenset({"catchall"}))

    assert with_core_workflows(configuration, config) == configuration
