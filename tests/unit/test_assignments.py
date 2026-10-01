# SPDX-License-Identifier: GPL-3.0-or-later
"""Structural assignment membership rules."""

from pathlib import Path

from ww.assignments import active_assignment, assignment_at
from ww.config import load_configuration
from ww.plan import compile_workflow_plan


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


def _loop_plan(tmp_path: Path, yaml: str):
    path = tmp_path / "ww.yaml"
    path.write_text(yaml, encoding="utf-8")
    return compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")


def _names(plan, assignment) -> list[str]:
    return [item.name for item in plan.items[assignment.start : assignment.stop]]


def test_per_round_keeps_one_round_in_one_assignment(tmp_path: Path) -> None:
    plan = _loop_plan(
        tmp_path,
        """handlers:
  - name: prepare
    description: Prepare the fix.
workflows:
  - name: task
    steps:
      - name: review-and-fix
        loop:
          - name: review
            break: Clean.
          - name: fix
            hooks:
              before_start:
                - name: prepare
          - name: verify
      - name: finish
""",
    )
    by_name = {item.name: index for index, item in enumerate(plan.items)}
    body = [item for item in plan.items if item.loop_id == "review-and-fix"]
    assert [item.name for item in body] == ["review", "prepare", "fix", "verify"]
    assert {item.loop_assignment for item in body} == {"per_round"}
    assert plan.items[by_name["finish"]].loop_id is None

    review = assignment_at(plan, by_name["review"], runtime="auto")
    assert review is not None
    assert _names(plan, review) == ["review", "prepare", "fix", "verify"]
    # The repeat boundary is a coordinator: it always ends the round.
    assert plan.items[review.stop].name == "review-and-fix"

    assert assignment_at(plan, by_name["review"], runtime="single") is not None
    single = assignment_at(plan, by_name["review"], runtime="single")
    assert single is not None and _names(plan, single) == ["review"]


def test_per_step_and_differing_worker_settings_end_the_round_assignment(
    tmp_path: Path,
) -> None:
    plan = _loop_plan(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: stepwise
        assignment: per_step
        loop:
          - name: a
            break: Done.
          - name: b
      - name: profiled
        loop:
          - name: review
            profile: code-reviewer
            break: Clean.
          - name: fix
            profile: developer
          - name: verify
            profile: developer
          - name: local
            role: manager
""",
    )
    by_name = {item.name: index for index, item in enumerate(plan.items)}

    stepwise = assignment_at(plan, by_name["a"], runtime="auto")
    assert stepwise is not None and _names(plan, stepwise) == ["a"]

    review = assignment_at(plan, by_name["review"], runtime="auto")
    assert review is not None and _names(plan, review) == ["review"]
    fix = assignment_at(plan, by_name["fix"], runtime="auto")
    assert fix is not None and _names(plan, fix) == ["fix", "verify"]
    local = assignment_at(plan, by_name["local"], runtime="auto")
    assert local is not None and _names(plan, local) == ["local"]
