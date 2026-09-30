# SPDX-License-Identifier: GPL-3.0-or-later
"""``role``: who performs a step, inherited like the profile."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.config.composition import compose_configuration
from ww.errors import ConfigurationError
from ww.plan import WorkflowPlanCompiler


def _roles(tmp_path: Path, workflows: str) -> dict[str, str]:
    path = tmp_path / "ww-agentic-workflows.yaml"
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


def test_a_group_or_loop_sets_the_role_of_its_body_and_a_step_overrides_it(
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
      - name: review-loop
        role: manager
        loop:
          - review: Review.
            break: No findings.
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
    path = tmp_path / "ww-agentic-workflows.yaml"
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
        "review in ww-agentic-workflows.yaml has role: manager, so model has no "
        "effect on it" in notice
        for notice in notices
    )
