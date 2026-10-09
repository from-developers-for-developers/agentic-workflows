# SPDX-License-Identifier: GPL-3.0-or-later
"""``role``: who performs a step, inherited like the profile."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.config.composition import compose_configuration
from ww.errors import ConfigurationError
from ww.plan import PlanItem, WorkflowPlanCompiler


def _roles(tmp_path: Path, workflows: str) -> dict[str, str]:
    path = tmp_path / "ww.yaml"
    path.write_text(workflows, encoding="utf-8")
    plan = WorkflowPlanCompiler(
        load_configuration(path), tmp_path, "codex", "TASK-1"
    ).compile("task")
    return {item.name: item.role for item in plan.items if item.owner == "agent"}


def test_steps_are_workers_unless_they_say_otherwise(tmp_path: Path) -> None:
    roles = _roles(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - develop: Develop.
      - review: Review.
        role: manager
""",
    )

    assert roles["develop"] == "worker"
    assert roles["review"] == "manager"


def test_a_group_sets_the_role_of_its_body_and_a_step_overrides_it(
    tmp_path: Path,
) -> None:
    roles = _roles(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: wrap-up
        role: manager
        steps:
          - reconcile: Reconcile.
          - land: Land.
      - name: review-group
        role: manager
        steps:
          - review: Review.

          - fix: Fix.
            role: worker
""",
    )

    assert roles["reconcile"] == roles["land"] == roles["review"] == "manager"
    assert roles["fix"] == "worker"


def test_a_workflow_role_reaches_every_step(tmp_path: Path) -> None:
    roles = _roles(
        tmp_path,
        """workflows:
  - name: task
    role: manager
    steps:
      - plan: Plan.
      - build: Build.
        role: worker
""",
    )

    assert roles["plan"] == "manager"
    assert roles["build"] == "worker"


def test_an_interactive_step_is_the_managers(tmp_path: Path) -> None:
    roles = _roles(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - talk: Talk it through.
        interactive: true
""",
    )

    assert roles["talk"] == "manager"


def test_a_step_ww_runs_cannot_take_a_role(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="role applies to agent steps only"):
        _roles(
            tmp_path,
            """workflows:
  - name: task
    steps:
      - build:
        argv: [make]
        role: manager
""",
        )


def test_an_inherited_role_passes_over_steps_ww_runs(tmp_path: Path) -> None:
    roles = _roles(
        tmp_path,
        """workflows:
  - name: task
    role: manager
    steps:
      - build:
        argv: [make]
      - review: Review.
""",
    )

    assert roles["review"] == "manager"


def test_lint_notes_worker_settings_on_a_managers_step(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: task
    steps:
      - review: Review.
        role: manager
        model: opus
""",
        encoding="utf-8",
    )

    notices = compose_configuration(path).notices

    assert any(
        "review in ww.yaml has role: manager, so model has no effect on it" in notice
        for notice in notices
    )


def _items(tmp_path: Path, workflows: str) -> dict[str, PlanItem]:
    path = tmp_path / "ww.yaml"
    path.write_text(workflows, encoding="utf-8")
    plan = WorkflowPlanCompiler(
        load_configuration(path), tmp_path, "codex", "TASK-1"
    ).compile("task")
    return {item.name: item for item in plan.items if item.owner == "agent"}


def test_subagents_false_reaches_every_step_of_a_group_and_keeps_their_models(
    tmp_path: Path,
) -> None:
    items = _items(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: develop
        model: opus
        subagents: false
        steps:
          - research: Research.
          - implement: Implement.
            model: sonnet
          - review: Review.
            subagents: true
      - document: Document.
""",
    )

    assert not items["research"].subagents
    assert not items["implement"].subagents
    assert items["review"].subagents
    assert items["document"].subagents
    # Delegation is untouched: the steps stay workers, with their own models.
    assert items["research"].role == items["implement"].role == "worker"
    assert items["research"].requested_model == "opus"
    assert items["implement"].requested_model == "sonnet"


def test_a_managers_step_keeps_its_subagents_rule(tmp_path: Path) -> None:
    items = _items(
        tmp_path,
        """workflows:
  - name: task
    subagents: false
    steps:
      - review: Review.
        role: manager
""",
    )

    assert items["review"].role == "manager"
    assert not items["review"].subagents


def test_a_containers_subagents_reaches_its_items_substeps(tmp_path: Path) -> None:
    items = _items(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: triage
        subagents: false
        items:
          steps:
            - analyze: Analyze.
            - report: Report.
              subagents: true
""",
    )

    assert not items["triage"].subagents
    assert not items["analyze"].subagents
    assert items["report"].subagents
