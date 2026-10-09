# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from ww.config import parse_yaml_text
from ww.errors import ConfigurationError
from ww.plan import compile_workflow_plan


def compile_plan(tmp_path: Path, steps: str):
    config = parse_yaml_text("workflows:\n  - name: review\n    steps:\n" + steps)
    return compile_workflow_plan(config, tmp_path, "review", "codex", "TASK-1")


@pytest.mark.parametrize("shorthand", ["~", "Split by source.", "{steps: []}"])
def test_empty_body_is_a_bounded_container(tmp_path: Path, shorthand: str) -> None:
    plan = compile_plan(
        tmp_path, f"      - feedback: Split.\n        items: {shorthand}\n"
    )
    own = [step for step in plan.items if step.item_context == "feedback"]
    assert [step.item_operation for step in own] == ["collect", "complete_collection"]
    assert not any(step.child_template for step in own)


@pytest.mark.parametrize(
    "old",
    [
        "assignment: together",
        "assignment: per_item",
        "assignment: per_step",
        "analyze: Analyze.",
        "resolve: Resolve.",
        "report: Report.",
        "model: cheapest",
    ],
)
def test_removed_item_policy_is_rejected(tmp_path: Path, old: str) -> None:
    with pytest.raises(ConfigurationError):
        compile_plan(
            tmp_path, f"      - feedback: Split.\n        items:\n          {old}\n"
        )


def test_item_phase_rejected_even_on_a_named_resolve_step(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        compile_plan(
            tmp_path,
            "      - feedback: Split.\n        items:\n"
            "          steps:\n            - resolve: Work.\n"
            "              item_phase: resolve\n",
        )


@pytest.mark.parametrize("token", ["{{ww.item.unknown}}", "{{ww.item.field.bad.name}}"])
def test_unknown_reference_is_rejected(tmp_path: Path, token: str) -> None:
    with pytest.raises(ConfigurationError, match="item"):
        compile_plan(
            tmp_path,
            f"      - feedback: Split.\n        items:\n"
            f'          steps:\n            - work: "{token}"\n',
        )


def test_reference_outside_context_names_missing_context(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="items context"):
        compile_plan(tmp_path, '      - work: "{{ww.item.text}}"\n')


@pytest.mark.parametrize(
    "binding",
    [
        'argv: [echo, "{{ww.item.text}}"]',
        'shell: echo "$VALUE"\n              env: {VALUE: "{{ww.item.text}}"}',
        'shell: echo "$1"\n              args: ["{{ww.item.field.source}}"]',
    ],
)
def test_machine_inputs_reject_collection_tokens(tmp_path: Path, binding: str) -> None:
    with pytest.raises(ConfigurationError, match="machine inputs"):
        compile_plan(
            tmp_path,
            f"      - feedback: Split.\n        items:\n"
            f"          steps:\n            - work: Work.\n"
            f"              {binding}\n",
        )


def test_settings_and_hooks_use_normal_inheritance(tmp_path: Path) -> None:
    plan = compile_plan(
        tmp_path,
        """      - feedback: Split.
        model: parent-model
        items:
          steps:
            - develop: Work.
              model: child-model
              hooks:
                after_complete:
                  - name: inspect
                    description: Inspect.
            - resolve: An ordinary name.
""",
    )
    work = next(step for step in plan.items if step.name == "develop")
    assert work.requested_model == "child-model"
    ordinary = next(step for step in plan.items if step.name == "resolve")
    assert ordinary.item_operation is None
    assert ordinary.requested_model == "parent-model"
    hook = next(step for step in plan.items if step.name == "inspect")
    assert hook.item_context == "feedback"
