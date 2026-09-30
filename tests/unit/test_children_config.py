# SPDX-License-Identifier: GPL-3.0-or-later
"""Parsing, compilation, and validation of ``children`` steps."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.instructions.text import action_text
from ww.operations import ChildWorkflowRun
from ww.output import render_plan
from ww.plan import compile_workflow_plan

CHILD = "  - name: child\n    steps:\n      - work: Work.\n"


def _load(tmp_path: Path, steps: str, extra: str = CHILD):  # type: ignore[no-untyped-def]
    path = tmp_path / "ww-agentic-workflows.yaml"
    path.write_text(
        f"workflows:\n  - name: parent\n    steps:\n{steps}{extra}", encoding="utf-8"
    )
    return load_configuration(path)


def test_children_mapping_parses_into_one_flow(tmp_path: Path) -> None:
    step = (
        _load(
            tmp_path,
            "      - split: Split it.\n        children:\n"
            "          description: One child per story.\n"
            "          workflow: child\n",
        )
        .workflows[0]
        .steps[0]
    )

    assert step.children is not None
    assert step.children.workflow == "child"
    assert step.children.description == "One child per story."


def test_children_step_owns_the_collection_and_the_run(tmp_path: Path) -> None:
    configuration = _load(
        tmp_path,
        "      - split: Split it.\n        children:\n"
        "          description: One child per story.\n"
        "          workflow: child\n"
        "        hooks:\n          after_complete:\n"
        "            - command:\n                argv: ['true']\n"
        "      - wrap-up: Summarize.\n",
    )
    plan = compile_workflow_plan(configuration, tmp_path, "parent", "codex")
    order = [(item.step, item.phase) for item in plan.items]

    collect = next(item for item in plan.items if item.step == "split")
    assert collect.child_operation == "collect"
    assert collect.split_instruction == "One child per story."
    run = next(item for item in plan.items if item.step == "split/children")
    assert isinstance(run.operation, ChildWorkflowRun)
    assert run.operation.workflow == "child"
    assert (run.name, run.parent, run.owner) == ("children", "split", "ww")
    assert run.child_operation is None
    # The step's completion hooks follow its children; nothing else collects.
    assert order.index(("split/children", "step")) < order.index(
        ("split", "after_complete")
    )
    assert [item.step for item in plan.items if item.child_operation] == ["split"]
    assert "How to split: One child per story." in action_text(collect)
    assert "**Splitting guidance:** One child per story." in render_plan(plan, False)


def test_last_children_step_leaves_the_workflow_summary_alone(tmp_path: Path) -> None:
    configuration = _load(
        tmp_path,
        "      - split: Split it.\n        children:\n          workflow: child\n",
    )
    plan = compile_workflow_plan(configuration, tmp_path, "parent", "codex")

    assert plan.items[-1].name == "update-workflow-summary"
    assert plan.items[-1].child_operation is None


@pytest.mark.parametrize(
    ("steps", "extra", "message"),
    [
        (
            "      - split: Split it.\n        children: ~\n",
            CHILD,
            "`children: ~` was replaced by a mapping",
        ),
        (
            "      - split: Split it.\n        children:\n          workflow: child\n"
            "      - run: ~\n        workflow_per_child: child\n",
            CHILD,
            "workflow_per_child was removed; collect and run the children",
        ),
        (
            "      - split: Split it.\n        children: child\n",
            CHILD,
            "children must be a mapping",
        ),
        (
            "      - split: Split it.\n        children:\n"
            "          description: Stories.\n",
            CHILD,
            "children.workflow must be a workflow name",
        ),
        (
            "      - split: Split it.\n        children:\n"
            "          workflow: child\n          steps: []\n",
            CHILD,
            "unknown key\\(s\\): steps",
        ),
        (
            "      - split: ~\n        workflow: child\n        children:\n"
            "          workflow: child\n",
            CHILD,
            "cannot combine children with a workflow transition",
        ),
        (
            "      - split: Split it.\n        children:\n          workflow: child\n"
            "        steps:\n          - more: More.\n",
            CHILD,
            "cannot combine steps, loop, items, and children",
        ),
        (
            "      - split: Split it.\n        children:\n          workflow: child\n"
            "        items: ~\n",
            CHILD,
            "cannot combine steps, loop, items, and children",
        ),
        (
            "      - split: Split it.\n        children:\n          workflow: child\n"
            "      - again: Again.\n        children:\n          workflow: child\n",
            CHILD,
            "at most one children step; found 'split', 'again'",
        ),
        (
            "      - split: Split it.\n        children:\n"
            "          workflow: nowhere\n",
            CHILD,
            "unknown child workflow 'nowhere'",
        ),
        (
            "      - split: Split it.\n        children:\n          workflow: child\n",
            "  - name: child\n    steps:\n      - split: Split.\n"
            "        children:\n          workflow: leaf\n"
            "  - name: leaf\n    steps:\n      - work: Work.\n",
            "recursive child tasks are not supported",
        ),
    ],
)
def test_invalid_children_are_rejected(
    tmp_path: Path, steps: str, extra: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _load(tmp_path, steps, extra)
