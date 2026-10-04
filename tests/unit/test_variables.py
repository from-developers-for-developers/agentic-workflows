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
        "T-1 in task,catchall: read {{ww.documents.plan}} and {{ww.metadata.key}}."
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


def test_item_values_use_stable_string_representations() -> None:
    from ww.items import WorkItem
    from ww.variables import item_variable_values

    values = item_variable_values(
        WorkItem("c1", "Text", resolved=True, fields=(("reply_id", "r1"),)),
        ("ww.item.field.reply_id", "ww.item.field.unset"),
    )

    assert values == {
        "ww.item.id": "c1",
        "ww.item.text": "Text",
        "ww.item.processed_item": "",
        "ww.item.proposed_solution": "",
        "ww.item.actual_solution": "",
        "ww.item.resolved": "true",
        "ww.item.reported": "false",
        "ww.item.reference_to_id": "",
        "ww.item.field.reply_id": "r1",
        "ww.item.field.unset": "",
    }


def test_item_context_errors_tell_the_three_cases_apart() -> None:
    from ww.items import WorkItem
    from ww.variables import item_binding_values, item_context_error

    missing = ("ww.item.id",)
    unbound = item_context_error(missing, item_binding_values(None, None))
    gone = item_context_error(missing, item_binding_values("c9", None))
    bound = item_binding_values("c1", WorkItem("c1", "T"))
    unknown = item_context_error(("ww.item.actual_soluton",), bound)

    assert unbound is not None and "no work item is bound" in unbound
    assert gone is not None and "'c9'" in gone and "no longer in the run" in gone
    assert unknown is not None and "ww.item.actual_soluton" in unknown
    assert "ww.item.actual_solution" in unknown
    assert item_context_error(("other.name",), bound) is None
