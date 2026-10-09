# SPDX-License-Identifier: GPL-3.0-or-later
"""Item pass identity, unset collection settings, and snapshot compatibility."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.execution_models import PLAN_SCHEMA_VERSION, PlanSnapshot
from ww.plan import WorkflowPlan, compile_workflow_plan


def _config(tmp_path: Path, steps: str):  # type: ignore[no-untyped-def]
    path = tmp_path / "ww.yaml"
    path.write_text(
        f"workflows:\n  - name: task\n    steps:\n{steps}", encoding="utf-8"
    )
    return load_configuration(path)


def _flow(tmp_path: Path, items: str):  # type: ignore[no-untyped-def]
    configuration = _config(
        tmp_path, f"      - review: Review.\n        items:{items}\n"
    )
    flow = configuration.workflows[0].steps[0].items
    assert flow is not None
    return flow


def _plan(tmp_path: Path, steps: str) -> WorkflowPlan:
    return compile_workflow_plan(_config(tmp_path, steps), tmp_path, "task", "codex")


def _snapshot(plan: WorkflowPlan) -> PlanSnapshot:
    return PlanSnapshot(
        schema_version=PLAN_SCHEMA_VERSION,
        compiler_version="test",
        configuration_digest="digest",
        compiled_at="2026-01-01T00:00:00Z",
        plan=plan,
    )


ONE_PASS = """      - collect: Collect.
        items:
          steps:
            - analyze: Analyze.
              item_phase: analyze
            - fix: Fix.
              item_phase: resolve
      - wrap: Wrap up.
"""


# --- model: unset settings and empty steps --------------------------------


def test_collection_settings_a_declaration_omits_stay_unset(tmp_path: Path) -> None:
    omitted = _flow(tmp_path, " ~")

    assert omitted.persistent is None
    assert omitted.identity is None
    assert omitted.unique is None


def test_explicit_default_settings_differ_from_unset(tmp_path: Path) -> None:
    explicit = _flow(tmp_path, "\n          persistent: false\n          unique: []")

    assert explicit.persistent is False
    assert explicit.unique == ()
    assert explicit != _flow(tmp_path, " ~")


def test_unique_is_kept_as_declared_and_identity_joins_the_effective_pool(
    tmp_path: Path,
) -> None:
    flow = _flow(
        tmp_path,
        "\n          persistent: true\n          identity: source_id\n"
        "          unique: [url]",
    )

    assert flow.persistent is True
    assert flow.identity == "source_id"
    assert flow.unique == ("url",)
    assert flow.effective_unique == ("source_id", "url")
    assert _flow(tmp_path, "\n          identity: source_id").unique is None


def test_unset_settings_compile_to_the_single_pass_defaults(tmp_path: Path) -> None:
    collector = next(
        item
        for item in _plan(tmp_path, "      - collect: C.\n        items: ~\n").items
        if item.item_operation == "collect"
    )

    assert collector.shared_items is False
    assert collector.item_identity is None
    assert collector.item_unique == ()


def test_empty_steps_and_bare_items_are_distinct_in_model_and_plan(
    tmp_path: Path,
) -> None:
    bare = _flow(tmp_path, " ~")
    empty = _flow(tmp_path, "\n          steps: []")
    nulled = _flow(tmp_path, "\n          steps: ~")

    assert not bare.collect_only
    assert [step.name for step in bare.steps] == ["handle-item"]
    assert empty.collect_only and nulled.collect_only
    assert empty.steps == ()

    bare_plan = _plan(tmp_path, "      - collect: C.\n        items: ~\n")
    empty_plan = _plan(
        tmp_path, "      - collect: C.\n        items:\n          steps: []\n"
    )
    assert [i.item_collect_only for i in bare_plan.items if i.name == "collect"] == [
        False
    ]
    assert any(i.name == "handle-item" and i.item_template for i in bare_plan.items)
    assert [i.item_collect_only for i in empty_plan.items if i.name == "collect"] == [
        True
    ]
    assert not any(i.name == "handle-item" for i in empty_plan.items)
    assert not any(i.item_template for i in empty_plan.items)


# --- compiler: pass identity ----------------------------------------------


def test_every_items_declaration_is_a_pass_with_its_own_stable_identity(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path, ONE_PASS)

    by_name = {item.name: item for item in plan.items}
    assert by_name["collect"].item_pass == "collect"
    assert by_name["analyze"].item_pass == "collect"
    assert by_name["fix"].item_pass == "collect"
    assert by_name["wrap"].item_pass is None
    assert all(
        item.item_template for item in plan.items if item.name in {"analyze", "fix"}
    )


def test_pass_identity_is_not_the_description_or_an_item_id(tmp_path: Path) -> None:
    first = _plan(tmp_path, "      - gather: One.\n        items: ~\n")
    second = _plan(
        tmp_path, "      - gather: Entirely different words.\n        items: ~\n"
    )

    def passes(plan: WorkflowPlan) -> set[str | None]:
        return {item.item_pass for item in plan.items if item.item_pass}

    assert passes(first) == passes(second) == {"gather"}


def test_pass_identity_covers_hooks_groups_and_nested_stages(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """handlers:
  - name: tidy
    argv: ["true"]
workflows:
  - name: task
    steps:
      - collect: Collect.
        hooks:
          before_complete:
            - name: tidy
        items:
          steps:
            - again: ~
              steps:
                - work: Work.
                  hooks:
                    before_complete:
                      - name: tidy
      - wrap: Wrap up.
""",
        encoding="utf-8",
    )
    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    members = [item for item in plan.items if item.item_template]
    assert {item.kind for item in members} >= {"prompt", "cli"}
    assert {item.item_pass for item in members} == {"collect"}
    # The collection and the step's own hook sit outside the per-item section.
    outside = [item for item in plan.items if not item.item_template]
    assert {item.item_pass for item in outside if item.name == "collect"} == {"collect"}
    assert {item.item_pass for item in outside if item.name != "collect"} == {None}


def test_assessment_outcomes_inside_a_per_item_stage_belong_to_the_pass(
    tmp_path: Path,
) -> None:
    plan = _plan(
        tmp_path,
        """      - collect: Collect.
        items:
          steps:
            - assess:
                question: Is it worth reviewing?
                positive:
                  steps:
                    - review: Review it.
""",
    )

    inside = [item for item in plan.items if item.item_template]
    assert {"assess", "review"} <= {item.name for item in inside}
    assert {item.item_pass for item in inside} == {"collect"}


def test_children_expansion_metadata_is_not_pass_identity(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        """workflows:
  - name: child
    steps:
      - work: Work.
  - name: task
    steps:
      - split: Split.
        children:
          workflow: child
""",
        encoding="utf-8",
    )
    plan = compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")

    assert all(item.item_pass is None for item in plan.items)


# --- codec -----------------------------------------------------------------


def test_pass_identity_round_trips_in_the_current_schema(tmp_path: Path) -> None:
    plan = _plan(tmp_path, ONE_PASS)
    raw = json.loads(json.dumps(_snapshot(plan).to_dict()))

    assert raw["schema_version"] == PLAN_SCHEMA_VERSION == 1
    loaded = PlanSnapshot.from_dict(raw)
    assert loaded.plan == plan
    assert loaded.template_plan == plan
    assert loaded.plan_digest == _snapshot(plan).plan_digest


def test_collect_only_flag_round_trips(tmp_path: Path) -> None:
    plan = _plan(tmp_path, "      - collect: C.\n        items:\n          steps: []\n")
    loaded = PlanSnapshot.from_dict(
        json.loads(json.dumps(_snapshot(plan).to_dict()))
    ).plan

    assert loaded == plan
    assert [
        item.item_collect_only for item in loaded.items if item.name == "collect"
    ] == [True]


def test_a_current_schema_plan_without_pass_identity_is_refused(
    tmp_path: Path,
) -> None:
    raw = json.loads(json.dumps(_snapshot(_plan(tmp_path, ONE_PASS)).to_dict()))
    for item in raw["plan"]["items"]:
        item.pop("item_pass", None)

    with pytest.raises(ValueError, match="no item_pass.*cannot be resumed"):
        PlanSnapshot.from_dict(raw)
