# SPDX-License-Identifier: GPL-3.0-or-later
"""Parsing, compilation, and validation of ``items`` steps."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.config_helpers import task_steps, task_workflow
from ww.config import load_configuration, parse_yaml_text
from ww.errors import ConfigurationError
from ww.output import render_plan
from ww.plan import compile_workflow_plan


def _load(tmp_path: Path, steps: str, handlers: str = ""):  # type: ignore[no-untyped-def]
    path = tmp_path / "ww.yaml"
    path.write_text(
        f"{handlers}workflows:\n  - name: task\n    steps:\n{steps}", encoding="utf-8"
    )
    return load_configuration(path)


def _compile(tmp_path: Path, steps: str, handlers: str = ""):  # type: ignore[no-untyped-def]
    return compile_workflow_plan(
        _load(tmp_path, steps, handlers), tmp_path, "task", "codex"
    )


def test_bare_items_get_one_built_in_stage(tmp_path: Path) -> None:
    flow = (
        _load(tmp_path, "      - review: Review.\n        items: ~\n")
        .workflows[0]
        .steps[0]
        .items
    )

    assert flow is not None
    assert flow.description is None
    assert flow.assignment == "together"
    assert [(step.name, step.item_operation) for step in flow.steps] == [
        ("handle-item", "handle_item")
    ]


def test_items_text_is_splitting_guidance(tmp_path: Path) -> None:
    plan = _compile(
        tmp_path,
        "      - review: Review the pull request.\n"
        "        items: Split by pull request comment.\n",
    )

    collector = next(item for item in plan.items if item.item_operation == "collect")
    assert collector.name == "review"
    assert collector.description == "Review the pull request."
    assert collector.split_instruction == "Split by pull request comment."
    assert [item.split_instruction for item in plan.items if item.item_template] == [
        None
    ]


def test_collect_only_items_have_no_templates(tmp_path: Path) -> None:
    plan = _compile(
        tmp_path, "      - review: Review.\n        items:\n          steps: []\n"
    )

    assert not any(item.item_template for item in plan.items)
    assert [item.item_operation for item in plan.items if item.name == "review"] == [
        "collect"
    ]


def test_item_settings_cascade_from_step_then_items_then_stage(tmp_path: Path) -> None:
    plan = _compile(
        tmp_path,
        """      - review: Review.
        model: opus
        profile: reviewer
        items:
          model: sonnet
          reasoning: low
          steps:
            - analyze: Analyze.
            - fix: Fix.
              model: haiku
              profile: developer
""",
    )

    by_name = {item.name: item for item in plan.items}
    assert (by_name["review"].requested_model, by_name["review"].profile) == (
        "opus",
        "reviewer",
    )
    analyze, fix = by_name["analyze"], by_name["fix"]
    assert (analyze.requested_model, analyze.requested_reasoning, analyze.profile) == (
        "sonnet",
        "low",
        "reviewer",
    )
    assert (fix.requested_model, fix.requested_reasoning, fix.profile) == (
        "haiku",
        "low",
        "developer",
    )


def test_items_role_manager_applies_to_its_stages(tmp_path: Path) -> None:
    plan = _compile(
        tmp_path,
        """      - review: Review.
        items:
          role: manager
          steps:
            - analyze: Analyze.
""",
    )

    by_name = {item.name: item for item in plan.items}
    assert by_name["review"].role == "worker"
    assert by_name["analyze"].role == "manager"


def test_item_assignment_is_carried_by_every_stage_and_hook(tmp_path: Path) -> None:
    plan = _compile(
        tmp_path,
        """      - review: Review.
        items:
          assignment: together
          steps:
            - analyze: Analyze.
              hooks:
                after_complete:
                  - name: note
                    description: Note it.
""",
    )

    assert {
        (item.name, item.item_assignment) for item in plan.items if item.item_template
    } == {("analyze", "together"), ("note", "together")}
    assert next(
        item for item in plan.items if item.name == "review"
    ).item_assignment == ("per_step")


def test_handler_reference_inherits_the_item_flow(tmp_path: Path) -> None:
    plan = _compile(
        tmp_path,
        "      - review:\n        handler: triage\n",
        handlers="""handlers:
  - name: triage
    description: Triage the findings.
    items:
      assignment: per_item
      steps:
        - analyze: Analyze.
        - fix: Fix.
""",
    )

    assert [(item.name, item.item_template) for item in plan.items][1:4] == [
        ("review", False),
        ("analyze", True),
        ("fix", True),
    ]


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        (
            "      - review: Review.\n        items: ~\n"
            "      - work:\n        steps_per_item:\n          - fix: Fix.\n",
            "unknown key\\(s\\): steps_per_item",
        ),
        (
            "      - review: Review.\n        items: 3\n",
            "must be null, splitting guidance text, or a mapping",
        ),
        (
            "      - review: Review.\n"
            "        items:\n          assignment: batch\n",
            "items.assignment must be one of: together, per_item, per_step",
        ),
        (
            "      - review: Review.\n        items:\n          unknown: 1\n",
            "unknown key\\(s\\): unknown",
        ),
        (
            "      - review: Review.\n        items:\n          steps: []\n"
            "          model: sonnet\n",
            "collects without per-item steps, so model has no effect",
        ),
        (
            "      - review: Review.\n        items: ~\n"
            "        steps:\n          - fix: Fix.\n",
            "cannot combine steps, loop, items, and children",
        ),
        (
            "      - review: Review.\n        items: ~\n        item_phase: analyze\n",
            "cannot combine items with item_phase",
        ),
        (
            "      - review: Review.\n        item_phase: triage\n",
            "item_phase must be one of: analyze, resolve, report",
        ),
        (
            "      - review: Review.\n        items: ~\n"
            "      - triage: Triage.\n        items: ~\n",
            "at most one items step; found 'review', 'triage'",
        ),
        (
            "      - outer:\n        steps:\n"
            "          - review: Review.\n            items: ~\n"
            "          - triage: Triage.\n            items: ~\n",
            "at most one items step",
        ),
    ],
)
def test_invalid_items_are_rejected(tmp_path: Path, steps: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _compile(tmp_path, steps)


@pytest.mark.parametrize("assignment", ["per_item", "together"])
def test_differing_stage_settings_compile_under_a_shared_assignment(
    tmp_path: Path, assignment: str
) -> None:
    plan = _compile(
        tmp_path,
        f"""      - review: Review.
        items:
          assignment: {assignment}
          steps:
            - analyze: Analyze.
            - fix: Fix.
              model: opus
            - local: Note it locally.
              role: manager
""",
    )

    assert [
        (item.name, item.requested_model, item.role)
        for item in plan.items
        if item.item_template
    ] == [
        ("analyze", "auto", "worker"),
        ("fix", "opus", "worker"),
        ("local", None, "manager"),
    ]


def test_shared_assignment_accepts_settings_set_on_items(tmp_path: Path) -> None:
    plan = _compile(
        tmp_path,
        """      - review: Review.
        items:
          assignment: per_item
          model: opus
          steps:
            - analyze: Analyze.
            - fix: Fix.
              model: opus
""",
    )

    assert {item.requested_model for item in plan.items if item.item_template} == {
        "opus"
    }


def test_per_step_allows_differing_stage_settings(tmp_path: Path) -> None:
    plan = _compile(
        tmp_path,
        """      - review: Review.
        items:
          steps:
            - analyze: Analyze.
              role: manager
            - fix: Fix.
              model: opus
""",
    )

    assert {item.name for item in plan.items if item.item_template} == {
        "analyze",
        "fix",
    }


def test_plan_view_shows_collection_guidance_and_item_assignment(
    tmp_path: Path,
) -> None:
    plan = _compile(
        tmp_path,
        """      - review: Review.
        items:
          description: Split by pull request comment.
          assignment: per_item
          steps:
            - analyze: Analyze.
""",
    )

    rendered = render_plan(plan, False)

    assert "**Splitting guidance:** Split by pull request comment." in rendered
    assert "**Item assignment:** `per_item`" in rendered


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("profile: reviewer", ("reviewer", "worker")),
        ("role: manager", (None, "manager")),
    ],
)
def test_step_settings_copied_from_a_handler_reach_the_stages(
    tmp_path: Path, setting: str, expected: tuple[str | None, str]
) -> None:
    plan = _compile(
        tmp_path,
        """      - review:
        handler: triage
        items:
          steps:
            - analyze: Analyze.
""",
        handlers=f"""handlers:
  - name: triage
    description: Triage the findings.
    {setting}
""",
    )

    analyze = next(item for item in plan.items if item.name == "analyze")
    assert (analyze.profile, analyze.role) == expected


def test_phase_guidance_extends_the_built_in_stage(tmp_path: Path) -> None:
    flow = (
        _load(
            tmp_path,
            "      - review: Review.\n"
            "        items:\n"
            "          description: Split by comment.\n"
            "          report: Reply in the same thread, then resolve it.\n"
            "          analyze: Quote the comment first.\n",
        )
        .workflows[0]
        .steps[0]
        .items
    )

    assert flow is not None
    assert flow.description == "Split by comment."
    (stage,) = flow.steps
    assert (stage.name, stage.item_operation) == ("handle-item", "handle_item")
    assert stage.description == (
        "Handle this item end to end: analyze it, resolve it, and report the "
        "outcome.\n\n"
        "When analyzing it: Quote the comment first.\n\n"
        "When reporting the outcome: Reply in the same thread, then resolve it."
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "          report: Reply.\n"
            "          steps:\n"
            "            - reply: Reply.\n"
            "              item_phase: report\n",
            r"phase guidance \(report\) describes the built-in handle-item",
        ),
        ("          report: Reply.\n          steps: []\n", "phase guidance"),
        (
            "          resolve: ~\n",
            "items.resolve must be a non-empty string",
        ),
    ],
)
def test_rejects_misplaced_phase_guidance(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _load(tmp_path, "      - review: Review.\n        items:\n" + body)


def test_items_save_metadata_belongs_to_the_built_in_stage(tmp_path: Path) -> None:
    flow = (
        _load(
            tmp_path,
            "      - review: Review.\n"
            "        items:\n"
            "          saves:\n"
            "            - metadata.pull_request.handled: The comment id.\n"
            "              append: true\n",
        )
        .workflows[0]
        .steps[0]
        .items
    )

    assert flow is not None
    (stage,) = flow.steps
    assert stage.name == "handle-item"
    (saved,) = stage.save_metadata
    assert (saved.name, saved.key, saved.append) == (
        "pull_request.handled",
        "pull_request.handled",
        True,
    )


def test_items_save_metadata_is_rejected_beside_configured_steps(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="saves belongs to the"):
        _load(
            tmp_path,
            "      - review: Review.\n"
            "        items:\n"
            "          saves:\n"
            "            - metadata.handled: The comment id.\n"
            "          steps:\n"
            "            - reply: Reply.\n"
            "              item_phase: report\n",
        )


def test_item_phase_marks_the_stage_and_items_take_phase_guidance() -> None:
    (collect,) = task_steps(
        task_workflow(
            "      - collect:\n        items:\n          steps:\n"
            "            - fix: Fix it.\n              item_phase: resolve\n"
        )
    )
    (builtin,) = task_steps(
        task_workflow(
            "      - collect:\n        items:\n          analyze: Read the thread.\n"
            "          report: Reply in the thread.\n"
        )
    )

    assert collect.items is not None
    assert collect.items.steps[0].item_operation == "resolve_item"
    assert builtin.items is not None
    description = builtin.items.steps[0].description
    assert "When analyzing it: Read the thread." in description
    assert "When reporting the outcome: Reply in the thread." in description
    with pytest.raises(ConfigurationError, match="item_phase must be one of"):
        parse_yaml_text(task_workflow("      - work: Work.\n        item_phase: fix\n"))


def test_interactive_page_is_the_operator_page_on_item_stages_only() -> None:
    (collect,) = task_steps(
        task_workflow(
            "      - collect:\n        items:\n          steps:\n"
            "            - answer: Answer it.\n              interactive: page\n"
        )
    )

    assert collect.items is not None
    stage = collect.items.steps[0]
    assert (stage.interactive, stage.ui) == (True, True)
    with pytest.raises(ConfigurationError, match="per-item stages only"):
        parse_yaml_text(
            task_workflow("      - talk: Talk.\n        interactive: page\n")
        )
    with pytest.raises(ConfigurationError, match="true, false, or page"):
        parse_yaml_text(
            task_workflow("      - talk: Talk.\n        interactive: chat\n")
        )
