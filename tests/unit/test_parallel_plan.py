# SPDX-License-Identifier: GPL-3.0-or-later
"""``parallel: true``: a group's steps become branches of one plan item."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.execution_models.plan_codec import _plan_from_dict
from ww.plan import ParallelBranch, PlanItem, WorkflowPlan, WorkflowPlanCompiler
from ww.plan.constructs import PARALLEL_PROMPT

REVIEW = """workflows:
  - name: task
    model: claude-sonnet-5-5
    steps:
      - develop: Develop.
      - name: review
        description: Merge the reviews.
        parallel: true
        artifact_from: develop
        steps:
          - security: Review {{ww.task.id}} for security issues.
            model: claude-opus-5-5
            reasoning: high
          - performance: Review for performance regressions.
            model: auto
          - quality: Review readability and tests.
            profile: reviewer
      - fix: Fix the findings.
        artifact_from: review
"""


def _compiled(tmp_path: Path, workflows: str) -> WorkflowPlan:
    path = tmp_path / "ww.yaml"
    path.write_text(workflows, encoding="utf-8")
    return WorkflowPlanCompiler(
        load_configuration(path), tmp_path, "claudecode", "TASK-1"
    ).compile("task")


def _plan(tmp_path: Path, workflows: str) -> tuple[PlanItem, ...]:
    return _compiled(tmp_path, workflows).items


def _steps(items: tuple[PlanItem, ...]) -> dict[str, PlanItem]:
    return {item.step: item for item in items if item.phase == "step"}


def test_a_parallel_group_is_one_manager_item_carrying_its_branches(
    tmp_path: Path,
) -> None:
    steps = _steps(_plan(tmp_path, REVIEW))

    review = steps["review"]
    assert "review/security" not in steps
    assert review.role == "manager"
    assert review.owner == "agent"
    assert review.artifact_dependencies == ("develop",)
    assert [branch.name for branch in review.parallel_branches] == [
        "security",
        "performance",
        "quality",
    ]
    security, performance, quality = review.parallel_branches
    assert security.description == "Review TASK-1 for security issues."
    assert (security.requested_model, security.requested_reasoning) == (
        "claude-opus-5-5",
        "high",
    )
    # ``auto`` stops inheritance; an unset model inherits the workflow's.
    assert performance.requested_model == "auto"
    assert quality.requested_model == "claude-sonnet-5-5"
    assert quality.profile == "reviewer"
    assert steps["fix"].artifact_dependencies == ("review",)


def test_a_group_without_a_description_reports_what_each_branch_found(
    tmp_path: Path,
) -> None:
    review = _steps(
        _plan(
            tmp_path,
            """workflows:
  - name: task
    steps:
      - name: review
        parallel: true
        steps:
          - a: A.
          - b: B.
""",
        )
    )["review"]

    assert review.operation.payload.text == PARALLEL_PROMPT  # type: ignore[union-attr]


def test_the_branches_survive_the_plan_snapshot(tmp_path: Path) -> None:
    plan = _compiled(tmp_path, REVIEW)

    assert _plan_from_dict(plan.to_dict()) == plan
    assert "parallel_branches" not in _steps(plan.items)["fix"].to_dict()


def test_a_branch_reads_back_only_its_own_shape() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        ParallelBranch.from_dict({"name": "a", "description": "A.", "hooks": []}, "b")
    assert ParallelBranch.from_dict({"name": "a", "description": "A."}, "b") == (
        ParallelBranch("a", "A.")
    )


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        (
            """      - name: review
        parallel: true
        steps:
          - a: A.
""",
            "needs at least two branches",
        ),
        (
            """      - name: review
        parallel: true
""",
            "parallel needs steps",
        ),
        (
            """      - name: review
        parallel: maybe
        steps:
          - a: A.
          - b: B.
""",
            "parallel must be true or false",
        ),
        (
            """      - name: review
        parallel: true
        role: worker
        steps:
          - a: A.
          - b: B.
""",
            "role: worker contradicts it",
        ),
        (
            """      - name: review
        parallel: true
        steps:
          - a: A.
            hooks:
              after_complete:
                - tidy: Tidy.
          - b: B.
""",
            "branch 'a' of parallel step 'review' in workflow 'task' sets hooks",
        ),
        (
            """      - name: review
        parallel: true
        steps:
          - a: A.
            artifact_from: init
          - b: B.
""",
            "sets artifact_from",
        ),
        (
            """      - name: review
        parallel: true
        steps:
          - a: A.
            steps:
              - inner: Inner.
          - b: B.
""",
            "sets steps, items or children",
        ),
        (
            """      - name: review
        parallel: true
        steps:
          - name: a
            argv: [echo, hi]
          - b: B.
""",
            "runs a command",
        ),
        (
            """      - name: collect
        items:
          steps:
            - name: review
              parallel: true
              steps:
                - a: A.
                - b: B.
""",
            "sits in an items body",
        ),
    ],
)
def test_invalid_parallel_groups_are_refused(
    tmp_path: Path, steps: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _plan(tmp_path, f"workflows:\n  - name: task\n    steps:\n{steps}")


def test_a_parallel_group_contradicts_an_inherited_subagents_false(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="subagents: false forbids"):
        _plan(
            tmp_path,
            """workflows:
  - name: task
    subagents: false
    steps:
      - name: review
        parallel: true
        steps:
          - a: A.
          - b: B.
""",
        )


def test_the_group_keeps_its_hooks_and_assessment(tmp_path: Path) -> None:
    items = _plan(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: review
        parallel: true
        assess: Are the findings serious?
        hooks:
          after_complete:
            - tidy: Tidy up.
        steps:
          - a: A.
          - b: B.
""",
    )

    review = _steps(items)["review"]
    assert review.assessment_attached
    assert review.parallel_branches
    assert any(
        item.step == "review" and item.phase == "after_complete" for item in items
    )
