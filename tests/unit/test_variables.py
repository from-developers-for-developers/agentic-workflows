# SPDX-License-Identifier: GPL-3.0-or-later
"""Step variables and ww's own ``{{ww.…}}`` values."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.config_helpers import task_steps, task_workflow
from ww.config import load_configuration, parse_yaml_text
from ww.errors import ConfigurationError
from ww.executable import printed_executable
from ww.plan import WorkflowPlanCompiler
from ww.variables import EXECUTABLE, runtime_variable_values, unavailable_ww_values


def test_variables_take_descriptions_or_bare_returned_names() -> None:
    (step,) = task_steps(
        task_workflow(
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
            task_workflow("      - work: Work.\n        variables: [{ww.task.x: X}]\n")
        )


def test_ww_values_compile_in_descriptions(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        "documents:\n  - plan: The plan.\n    path: plans/{{ww.task.id}}.md\n"
        + task_workflow(
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
        "T-1 in task: read {{ww.documents.plan}} and {{ww.metadata.key}}."
    )
    assert plan.documents[0].path == "plans/{{ww.task.id}}.md"


def test_ww_executable_is_left_for_the_page_and_names_the_printed_command(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        task_workflow("      - work: Run `{{ww.executable}} discover`.\n"),
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


def test_task_slug_is_the_task_id_without_slashes(tmp_path: Path) -> None:
    from ww.variables import (
        CORE_VARIABLE_NAMES,
        TASK_SLUG,
        compile_variable_values,
        task_slug,
    )

    assert task_slug("TASK-42") == "TASK-42"
    assert task_slug("TASK-42/A") == "TASK-42-A"
    assert TASK_SLUG in CORE_VARIABLE_NAMES
    assert compile_variable_values(("task",), "TASK-42/A")[TASK_SLUG] == "TASK-42-A"
    assert TASK_SLUG not in compile_variable_values(("task",), None)
    assert runtime_variable_values(tmp_path, "TASK-42/A")[TASK_SLUG] == "TASK-42-A"

    path = tmp_path / "ww.yaml"
    path.write_text(
        task_workflow("      - work: Notes in notes/{{ww.task.slug}}.md\n"),
        encoding="utf-8",
    )
    plan = WorkflowPlanCompiler(
        load_configuration(path), tmp_path, "codex", "TASK-42/A"
    ).compile("task")
    work = next(item for item in plan.items if item.name == "work")
    assert work.description == "Notes in notes/TASK-42-A.md"


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


def test_item_references_allow_whitespace_inside_the_braces() -> None:
    from ww.errors import ConfigurationError
    from ww.variables import validate_item_references

    assert validate_item_references("{{ ww.item.id }} {{ww.item.text}}", "c") == (
        "ww.item.id",
        "ww.item.text",
    )
    with pytest.raises(ConfigurationError, match="unknown ww.item reference"):
        validate_item_references("{{ww.item}}", "c")
    with pytest.raises(ConfigurationError, match="unknown item field"):
        validate_item_references("{{ ww.item.nope }}", "c")
