# SPDX-License-Identifier: GPL-3.0-or-later
"""Persisted state written before the v1 renames keeps loading.

The plan snapshot (schema 17), the execution state (schema 10), the
rule-automation store (schema 2), and ww/git settings a run froze are
upgraded on read, never refused (the plan's persisted state rule).
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

from ww.config import load_configuration
from ww.execution_models import (
    EXECUTION_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    ExecutionState,
    PlanSnapshot,
    initial_state,
)
from ww.execution_models.records import VerificationRule
from ww.extensions import ExtensionRegistry
from ww.plan import WorkflowPlanCompiler
from ww.rule_store import STORE_SCHEMA_VERSION, RuleAutomation

_YAML = """documents:
  - plan: The plan.
    path: plans/{{ww.task.id}}.md
hooks:
  before_start:
    - steps: [check]
      argv: [git, rev-parse, --abbrev-ref, HEAD]
      assert: [{equals: main}]
workflows:
  - name: task
    steps:
      - name: check
        description: Read {{ww.documents.plan}} and {{ww.metadata.jira.key}}.
        rules:
          - text: Include foo.
            shell: grep -L foo $WW_STEP_CHANGED_FILES || true
            assert: [empty]
      - name: review
        loop:
          - fix: Fix what {{ww.project_metadata.release}} needs.
            break: Nothing is left.
      - name: comments
        items:
          steps:
            - answer: Answer {{ww.item.text}} ({{ww.item.field.thread}}).
"""

# How schema 17 spelled what schema 18 renamed, as JSON text fragments.
_SPELLED_17 = (
    ("{{ww.documents.", "{{documents."),
    ("{{ww.metadata.", "{{metadata."),
    ("{{ww.project_metadata.", "{{project_metadata."),
    ("{{ww.item.field.", "{{field."),
    ("{{ww.item.", "{{item."),
    ('"item_assignment": "together"', '"item_assignment": "all_items"'),
    ('"loop_assignment": "per_round"', '"loop_assignment": "per_iteration"'),
    ('"phase": "before_start"', '"phase": "before_in_progress"'),
    ("plans/{{ww.task.id}}.md", "plans/{task_id}.md"),
    (
        '"assert": [{"equals": "main"}]',
        '"assert": {"operator": "eq", "expected": "main"}',
    ),
    ('"assert": ["empty"]', '"assert": {"operator": "empty"}'),
)


def _snapshot(tmp_path: Path) -> PlanSnapshot:
    path = tmp_path / "ww-agentic-workflows.yaml"
    path.write_text(_YAML, encoding="utf-8")
    plan = WorkflowPlanCompiler(
        load_configuration(path), tmp_path, "codex", "TASK-1"
    ).compile("task")
    return PlanSnapshot(
        schema_version=PLAN_SCHEMA_VERSION,
        compiler_version="plan-v9",
        configuration_digest="configuration",
        compiled_at="2026-09-30T00:00:00Z",
        plan=plan,
    )


def _as_schema_17(data: dict[str, object]) -> dict[str, object]:
    text = json.dumps(data)
    for new, old in _SPELLED_17:
        assert new in text, new
        text = text.replace(new, old)
    old_data = json.loads(text)
    old_data["schema_version"] = 17
    return old_data


def test_a_schema_17_plan_snapshot_reads_as_the_same_plan(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    current = snapshot.to_dict()

    restored = PlanSnapshot.from_dict(_as_schema_17(current))

    assert restored.schema_version == PLAN_SCHEMA_VERSION == 18
    assert restored.to_dict() == current
    assert restored.plan_digest == snapshot.plan_digest


def test_a_schema_17_agent_item_keeps_its_frozen_check_assertions(
    tmp_path: Path,
) -> None:
    current = _snapshot(tmp_path).to_dict()
    old = _as_schema_17(current)
    check = next(item for item in old["plan"]["items"] if item.get("checks"))
    assert check["checks"][0]["command"]["assert"] == {"operator": "empty"}

    restored = PlanSnapshot.from_dict(old).to_dict()

    migrated = next(item for item in restored["plan"]["items"] if item.get("checks"))
    assert migrated["checks"][0]["command"]["assert"] == ["empty"]


def test_plan_item_ids_keep_their_old_spelling_through_the_migration(
    tmp_path: Path,
) -> None:
    current = _snapshot(tmp_path).to_dict()
    old = _as_schema_17(current)
    hook = next(
        item for item in old["plan"]["items"] if item["phase"] == "before_in_progress"
    )
    hook["id"] = hook["id"].replace("before_start", "before_in_progress")

    restored = PlanSnapshot.from_dict(old).plan

    migrated = next(item for item in restored.items if item.phase == "before_start")
    assert ":before_in_progress:" in migrated.id


def test_a_schema_10_execution_state_reads_with_the_renamed_values(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    state = replace(
        initial_state(snapshot, (), "2026-09-30T00:00:00Z", run_id="01-task"),
        status="failed",
        failure_kind="rules_proposed",
        workflow_values=(
            ("ww.project.name", "api"),
            ("__branch_naming_strategy", "hotfix"),
            ("workflow", "task"),
        ),
    )
    check_item = next(
        index for index, item in enumerate(snapshot.plan.items) if item.checks
    )
    checks = snapshot.plan.items[check_item].checks
    records = list(state.item_executions)
    records[check_item] = replace(
        records[check_item],
        resolved_checks=checks,
        verification=(
            VerificationRule(
                id="include-foo",
                text="Include foo.",
                text_hash="a" * 64,
                state="approach_approved",
                approach="Grep for foo.",
                check="include-foo",
            ),
        ),
    )
    state = replace(state, item_executions=tuple(records))
    old = copy.deepcopy(state.to_dict())
    old["schema_version"] = 10
    old_record = old["item_executions"][check_item]
    old_record["verification"][0]["state"] = "approach-approved"
    assert old_record["resolved_checks"][0]["command"]["assert"] == ["empty"]
    old_record["resolved_checks"][0]["command"]["assert"] = {"operator": "empty"}
    old["failure_kind"] = "check_proposed"
    old["workflow_values"] = {
        "__project": "api",
        "__branch_naming_strategy": "hotfix",
        "workflow": "task",
    }

    restored = ExecutionState.from_dict(old)

    assert EXECUTION_SCHEMA_VERSION == 11
    assert restored == state
    assert restored.to_dict()["schema_version"] == 11


def test_a_schema_2_rule_store_reads_snake_case_statuses_and_assert_lists() -> None:
    store = RuleAutomation.from_dict(
        {
            "schema_version": 2,
            "rules": {
                "a" * 64: {"text": "Keep it small.", "status": "approach-proposed"},
                "b" * 64: {"text": "Name it well.", "status": "approach-approved"},
                "c" * 64: {
                    "text": "Be kind.",
                    "status": "not-convertible",
                    "reason": "A judgement.",
                },
            },
            "checks": {
                "lint": {
                    "argv": ["make", "lint"],
                    "assert": {"operator": "eq", "expected": "ok"},
                    "config": [],
                    "covers": ["a" * 64],
                    "proven": True,
                    "status": "converted",
                    "pending": {
                        "shell": "make lint",
                        "assert": {"operator": "empty"},
                        "config": [],
                        "covers": ["a" * 64],
                        "proven": False,
                    },
                }
            },
        }
    )

    assert [entry.status for entry in store.rules.values()] == [
        "approach_proposed",
        "approach_approved",
        "not_convertible",
    ]
    data = store.to_dict()
    assert data["schema_version"] == STORE_SCHEMA_VERSION == 3
    check = data["checks"]["lint"]
    assert check["assert"] == [{"equals": "ok"}]
    assert check["pending"]["assert"] == ["empty"]
    assert RuleAutomation.from_dict(data) == store


def test_frozen_git_settings_are_upgraded_and_live_ones_are_not() -> None:
    registry = ExtensionRegistry.discover(Path(__file__).parents[2])
    frozen = {
        "commit_message": "{{task_id}}: {{commit_message}}",
        "use_separate_branch": True,
        "branch_name_formats": {"default": "feature/{{task_id}}"},
    }

    upgraded = registry.frozen_settings("ww/git", frozen)

    assert upgraded == {
        "commit_format": "{{ww.task.id}}: {{commit_message}}",
        "separate_branch": True,
        "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
    }
    assert frozen["use_separate_branch"] is True


def test_frozen_git_tokens_written_with_spaces_are_upgraded() -> None:
    registry = ExtensionRegistry.discover(Path(__file__).parents[2])

    upgraded = registry.frozen_settings(
        "ww/git",
        {
            "commit_message": "{{ task_id }} ({{workflow }}, {{  run_id}}): "
            "{{ commit_message }}",
        },
    )

    assert upgraded == {
        "commit_format": "{{ww.task.id}} ({{ww.task.workflow}}, {{ww.task.run}}): "
        "{{ commit_message }}",
    }
