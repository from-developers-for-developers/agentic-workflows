# SPDX-License-Identifier: GPL-3.0-or-later
"""Parsing, compilation, and validation of per-child parent stages."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.extensions import ExtensionRegistry
from ww.operations import ChildWorkflowRun
from ww.output import render_plan
from ww.plan import compile_workflow_plan
from ww.workflow_config import WorkflowConfiguration

CHILD = "  - name: child\n    steps:\n      - work: Work.\n"

STAGES = """      - slices: One child per slice.
        children:
          description: One child per slice of the plan.
          steps:
            - refine: Refine {{ww.child.text}} for {{ww.child.id}}.
              role: manager
            - implement:
                workflow: child
            - review: Review {{ww.child.field.area}} in {{ww.child.project}}.
              artifact_from: implement

            - land: Land {{ww.child.id}}.
"""


def _load(tmp_path: Path, steps: str, extra: str = CHILD) -> WorkflowConfiguration:
    path = tmp_path / "ww.yaml"
    path.write_text(
        f"workflows:\n  - name: parent\n    steps:\n{steps}{extra}", encoding="utf-8"
    )
    return load_configuration(path)


def test_children_steps_parse_with_one_stage_running_the_child(
    tmp_path: Path,
) -> None:
    step = _load(tmp_path, STAGES).workflows[0].steps[0]

    assert step.children is not None
    assert step.children.workflow == "child"
    assert step.children.description == "One child per slice of the plan."
    assert [stage.name for stage in step.children.steps] == [
        "refine",
        "implement",
        "review",
        "land",
    ]
    run = step.children.steps[1]
    assert run.operation == ChildWorkflowRun("child")
    assert run.description == (
        "Run the child task with the `child` workflow and wait for it."
    )


def test_per_child_stages_compile_as_templates_after_the_collection(
    tmp_path: Path,
) -> None:
    plan = compile_workflow_plan(_load(tmp_path, STAGES), tmp_path, "parent", "codex")
    stages = [item for item in plan.items if item.child_stage is not None]

    collect = next(item for item in plan.items if item.step == "slices")
    assert collect.child_operation == "collect"
    assert collect.child_stage is None
    assert [item.step for item in stages] == [
        "slices/{child}/refine",
        "slices/{child}/implement",
        "slices/{child}/review",
        "slices/{child}/land",
    ]
    assert all(item.child_template and item.child_number is None for item in stages)
    assert {item.child_stage for item in stages} == {"slices"}
    run = stages[1]
    assert isinstance(run.operation, ChildWorkflowRun)
    assert run.owner == "ww"
    review = stages[2]
    assert review.artifact_dependency == "slices/{child}/implement"
    assert set(review.dependencies) == {"ww.child.field.area", "ww.child.project"}
    assert "**Per-child stage:** repeats for every collected child" in render_plan(
        plan, False
    )


def test_per_child_stages_read_the_childs_extension_values(tmp_path: Path) -> None:
    (tmp_path / "ww.json").write_text(
        '{"extensions": {"ww/git": {}}}', encoding="utf-8"
    )
    configuration = _load(
        tmp_path,
        "      - slices: Split.\n        children:\n          steps:\n"
        "            - implement:\n                workflow: child\n"
        "            - land: Merge {{ww.child.git.branch}} into {{ww.git.branch}}.\n",
    )
    plan = compile_workflow_plan(
        configuration,
        tmp_path,
        "parent",
        "codex",
        extensions=ExtensionRegistry.discover(tmp_path),
    )

    land = next(item for item in plan.items if item.name == "land")
    assert set(land.dependencies) == {"ww.child.git.branch", "ww.git.branch"}


def test_a_per_child_stage_can_land_the_child_with_merge_branch(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.json").write_text(
        '{"extensions": {"ww/git": {}}}', encoding="utf-8"
    )
    configuration = _load(
        tmp_path,
        "      - slices: Split.\n        children:\n          steps:\n"
        "            - implement:\n                workflow: child\n"
        "            - name: ext/ww/git/handlers:merge-branch\n"
        '              args: ["{{ww.child.git.branch}}", "Land {{ww.child.id}}"]\n',
    )
    plan = compile_workflow_plan(
        configuration,
        tmp_path,
        "parent",
        "codex",
        extensions=ExtensionRegistry.discover(tmp_path),
    )

    land = next(item for item in plan.items if item.name == "merge-branch")
    assert (land.kind, land.owner) == ("extension", "ww")
    assert land.outputs == ("merge_commit",)
    assert set(land.dependencies) == {"ww.child.git.branch", "ww.child.id"}


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        (
            "      - slices: Split.\n        children:\n          steps:\n"
            "            - review: Review.\n",
            "needs exactly one stage with `workflow:`.*found 0",
        ),
        (
            "      - slices: Split.\n        children:\n          steps:\n"
            "            - a:\n                workflow: child\n"
            "            - b:\n                workflow: child\n",
            "needs exactly one stage with `workflow:`.*found 2",
        ),
        (
            "      - slices: Split.\n        children:\n          steps: []\n",
            "children.steps must contain at least one step",
        ),
        (
            "      - slices: Split.\n        children:\n          steps:\n"
            "            - name: group\n              steps:\n"
            "                - name: inner\n                  handoff_to: child\n"
            "            - implement:\n                workflow: child\n",
            "'inner' carries handoff_to",
        ),
        (
            "      - slices: Split.\n        children:\n          steps:\n"
            "            - implement:\n                workflow: child\n"
            "                model: cheap\n",
            "agent, model, and reasoning do not apply",
        ),
        (
            "      - slices: Split.\n        children:\n          steps:\n"
            "            - implement:\n                workflow: child\n"
            "            - more: More.\n              items: ~\n",
            "cannot use items or children",
        ),
        (
            "      - slices: Split.\n        children:\n          steps:\n"
            "            - implement:\n                workflow: nowhere\n",
            "unknown child workflow 'nowhere'",
        ),
        (
            "      - early: Read {{ww.child.id}}.\n"
            "      - slices: Split.\n        children:\n          workflow: child\n",
            "reads \\{\\{ww.child.id\\}\\}, which only a stage under children.steps",
        ),
        (
            "      - slices: Split.\n        children:\n          steps:\n"
            "            - implement:\n                workflow: child\n"
            "            - review: Review {{ww.child.bogus}}.\n",
            "unknown child value\\(s\\): ww.child.bogus",
        ),
    ],
)
def test_invalid_per_child_stages_are_rejected(
    tmp_path: Path, steps: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        compile_workflow_plan(_load(tmp_path, steps), tmp_path, "parent", "codex")


def test_a_children_step_runs_once_inside_an_items_context(tmp_path: Path) -> None:
    plan = compile_workflow_plan(
        _load(
            tmp_path,
            "      - collect: Collect.\n        items:\n          steps:\n"
            "            - split: Split.\n              children:\n"
            "                workflow: child\n",
        ),
        tmp_path,
        "parent",
        "codex",
    )
    split = [item for item in plan.items if item.step.startswith("collect/split")]
    assert split and all(item.item_context == "collect" for item in split)
    assert not any(item.child_template for item in split)


def test_a_stage_before_the_run_cannot_read_child_extension_values(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.json").write_text(
        '{"extensions": {"ww/git": {}}}', encoding="utf-8"
    )
    configuration = _load(
        tmp_path,
        "      - slices: Split.\n        children:\n          steps:\n"
        "            - name: prepare\n              steps:\n"
        "                - refine: Refine {{ww.child.id}} ({{ww.child.field.area}}).\n"
        "                - branch: Look at {{ww.child.git.branch}}.\n"
        "            - implement:\n                workflow: child\n",
    )

    with pytest.raises(
        ConfigurationError,
        match="stage 'prepare' reads \\{\\{ww.child.git.branch\\}\\}, but that "
        "stage runs before the child task exists",
    ):
        compile_workflow_plan(
            configuration,
            tmp_path,
            "parent",
            "codex",
            extensions=ExtensionRegistry.discover(tmp_path),
        )
