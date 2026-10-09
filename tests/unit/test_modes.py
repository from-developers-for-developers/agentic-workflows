# SPDX-License-Identifier: GPL-3.0-or-later
"""Modes: parsed with their filters, resolved per agent step, frozen in the plan."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.execution_models.plan_codec import _plan_from_dict
from ww.plan import (
    PlanCompilationOptions,
    PlanItem,
    PlannedMode,
    WorkflowPlan,
    WorkflowPlanCompiler,
)
from ww.workflow_config import ALL, NameFilter

WORKFLOWS = """modes:
  - economy: Keep it short.
  - name: tdd
    description:
      - Write the failing test first.
      - Then make it pass.
    steps: [develop]
  - careful: Double-check everything.
    workflows: [bugfix]
workflows:
  - name: task
    modes: [economy]
    hooks:
      after_complete:
        - note: Note it.
    steps:
      - develop: Develop.
      - review: Review.
  - name: bugfix
    steps:
      - develop: Fix.
  - name: hotfix
    inherit: bugfix
"""


def _write(tmp_path: Path, text: str = WORKFLOWS) -> Path:
    path = tmp_path / "ww.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _plan(
    tmp_path: Path,
    workflow: str = "task",
    text: str = WORKFLOWS,
    modes: tuple[str, ...] | None = None,
) -> WorkflowPlan:
    return WorkflowPlanCompiler(
        load_configuration(_write(tmp_path, text)),
        tmp_path,
        "codex",
        "TASK-1",
        options=PlanCompilationOptions(task_id="TASK-1", modes=modes),
    ).compile(workflow)


def _step(plan: WorkflowPlan, name: str) -> PlanItem:
    return next(
        item for item in plan.items if item.phase == "step" and item.name == name
    )


def _names(item: PlanItem) -> list[str]:
    return [mode.name for mode in item.modes]


def test_a_mode_parses_its_filters(tmp_path: Path) -> None:
    modes = {mode.name: mode for mode in load_configuration(_write(tmp_path)).modes}

    assert not modes["economy"].automatic
    assert modes["economy"].workflows is None and modes["economy"].steps is None
    assert modes["tdd"].automatic
    assert modes["tdd"].workflows is None
    assert modes["tdd"].steps == NameFilter(("develop",))
    # A mode filtered to a workflow also applies to its heirs.
    assert modes["careful"].workflows == NameFilter(("bugfix", "hotfix"))
    assert modes["tdd"].to_dict() == {
        "name": "tdd",
        "description": ["Write the failing test first.", "Then make it pass."],
        "steps": ["develop"],
    }


def test_an_empty_mode_filter_admits_everything_and_star_too(tmp_path: Path) -> None:
    modes = load_configuration(
        _write(
            tmp_path,
            """modes:
  - everywhere: Always.
    workflows: []
  - star: Always too.
    steps: "*"
workflows:
  - name: task
    steps:
      - develop: Develop.
""",
        )
    ).modes

    assert [(mode.workflows, mode.steps) for mode in modes] == [
        (ALL, None),
        (None, ALL),
    ]
    assert all(mode.automatic for mode in modes)


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ("steps: develop", r'steps must be "\*" or a list of names'),
        ('steps: ["*", develop]', r'cannot mix "\*" with names'),
        ("steps: [missing]", r"mode 'tdd' references unknown step\(s\): missing"),
        (
            "workflows: [missing]",
            r"mode 'tdd' references unknown workflow\(s\): missing",
        ),
        ("when: always", r"unknown key"),
    ],
)
def test_bad_mode_filters_are_rejected(
    tmp_path: Path, filters: str, message: str
) -> None:
    text = f"""modes:
  - tdd: Test first.
    {filters}
workflows:
  - name: task
    steps:
      - develop: Develop.
"""
    with pytest.raises(ConfigurationError, match=message):
        load_configuration(_write(tmp_path, text))


def test_selected_modes_come_first_then_automatic_ones_per_step(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)

    assert plan.modes == ("economy",)
    develop = _step(plan, "develop")
    assert develop.modes == (
        PlannedMode("economy", ("Keep it short.",)),
        PlannedMode(
            "tdd",
            ("Write the failing test first.", "Then make it pass."),
            automatic=True,
        ),
    )
    assert _names(_step(plan, "review")) == ["economy"]


def test_an_automatic_mode_follows_its_workflow_filter_and_heirs(
    tmp_path: Path,
) -> None:
    assert _names(_step(_plan(tmp_path, "bugfix"), "develop")) == ["tdd", "careful"]
    assert _names(_step(_plan(tmp_path, "hotfix"), "develop")) == ["tdd", "careful"]
    assert "careful" not in _names(_step(_plan(tmp_path, "task"), "develop"))


def test_selecting_modes_replaces_only_the_defaults(tmp_path: Path) -> None:
    plan = _plan(tmp_path, modes=("careful",))

    assert plan.modes == ("careful",)
    # ``tdd`` applies by itself; ``careful`` is selected, so it is listed once.
    assert [(mode.name, mode.automatic) for mode in _step(plan, "develop").modes] == [
        ("careful", False),
        ("tdd", True),
    ]
    assert _names(_step(plan, "review")) == ["careful"]


def test_selecting_no_modes_keeps_the_automatic_ones(tmp_path: Path) -> None:
    plan = _plan(tmp_path, modes=())

    assert plan.modes == ()
    assert _names(_step(plan, "develop")) == ["tdd"]
    assert _step(plan, "review").modes == ()


def test_modes_reach_only_agent_steps(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    for item in plan.items:
        if item.phase != "step" or item.summary or item.step == "init":
            assert item.modes == (), item.id


def test_item_stages_and_group_steps_get_their_modes(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        text="""modes:
  - economy: Keep it short.
    steps: [analyze, fix]
workflows:
  - name: task
    steps:
      - triage: Triage.
        items:
          steps:
            - analyze: Analyze.
      - name: review-group
        steps:
          - review: Review.

          - fix: Fix.
""",
    )

    assert _names(_step(plan, "analyze")) == ["economy"]
    assert _names(_step(plan, "fix")) == ["economy"]
    assert _step(plan, "triage").modes == ()
    assert _step(plan, "review").modes == ()


def test_step_modes_round_trip_through_the_saved_plan(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    assert _plan_from_dict(plan.to_dict()) == plan
    # A step without modes writes no key, so plans without modes keep their form.
    plain = _plan(tmp_path, modes=(), text=WORKFLOWS.replace("steps: [develop]", ""))
    assert not any("modes" in item.to_dict() for item in plain.items)


def test_a_saved_mode_of_another_shape_is_rejected() -> None:
    with pytest.raises(ValueError, match="name, description, automatic"):
        PlannedMode.from_dict({"name": "economy"}, "modes[0]")
    with pytest.raises(ValueError, match="invalid"):
        PlannedMode.from_dict(
            {"name": "economy", "description": "short", "automatic": False},
            "modes[0]",
        )
