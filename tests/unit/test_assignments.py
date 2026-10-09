# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural assignment membership rules."""

from pathlib import Path

import pytest

from ww.assignments import active_assignment, assignment_at, completion_window
from ww.config import load_configuration
from ww.errors import StateError
from ww.plan import compile_workflow_plan


@pytest.mark.parametrize("conflicting", [False, True])
def test_completion_inputs_share_matching_requests_and_identify_conflicts(
    tmp_path: Path, conflicting: bool
) -> None:
    other_description = "A different message." if conflicting else "The message."
    path = tmp_path / "ww.yaml"
    path.write_text(
        f"""handlers:
  - name: first
    argv: [echo, "{{{{message}}}}"]
    variables:
      - message: The message.
  - name: second
    argv: [echo, "{{{{message}}}}"]
    variables:
      - message: {other_description}
hooks:
  before_complete_workflow:
    - name: first
workflows:
  - name: task
    hooks:
      before_complete_workflow:
        - name: second
    steps:
      - record: Record it.
""",
        encoding="utf-8",
    )
    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")
    cursor = next(
        index for index, item in enumerate(plan.items) if item.name == "record"
    )
    if conflicting:
        with pytest.raises(StateError) as error:
            completion_window(plan, cursor)
        assert "conflicting provided variable 'message'" in str(error.value)
        assert "first (global," in str(error.value)
        assert "second (workflow," in str(error.value)
    else:
        values, context = completion_window(plan, cursor)
        assert [value.name for value in values] == ["message"]
        assert context == ("first", "second")


def test_parent_preparation_is_separate_and_parent_tail_follows_leaf(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """handlers:
  - name: parent-preparation
    description: Prepare the parent.
  - name: parent-finish
    description: Finish the parent.
  - name: workflow-finish
    description: Finish the workflow.
hooks:
  before_complete_workflow:
    - name: workflow-finish
workflows:
  - name: task
    steps:
      - name: parent
        hooks:
          before_start:
            - name: parent-preparation
          after_complete:
            - name: parent-finish
        steps:
          - name: leaf
      - name: sibling
""",
        encoding="utf-8",
    )
    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")
    by_name = {item.name: index for index, item in enumerate(plan.items)}

    preparation = assignment_at(plan, by_name["parent-preparation"], runtime="auto")
    assert preparation is not None
    assert preparation.stop == by_name["leaf"]

    leaf = assignment_at(plan, by_name["leaf"], runtime="auto")
    assert leaf is not None
    assert [item.name for item in plan.items[leaf.start : leaf.stop]] == [
        "leaf",
        "parent-finish",
    ]
    assert leaf.stop == by_name["sibling"]
    assert active_assignment(plan, leaf.first_item_id, runtime="auto") == leaf

    sibling = assignment_at(plan, by_name["sibling"], runtime="auto")
    assert sibling is not None
    assert [item.name for item in plan.items[sibling.start : sibling.stop]] == [
        "sibling",
        "workflow-finish",
        "update-workflow-summary",
    ]


def _names(plan, assignment) -> list[str]:
    return [item.name for item in plan.items[assignment.start : assignment.stop]]
