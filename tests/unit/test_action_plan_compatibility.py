# SPDX-License-Identifier: GPL-3.0-or-later
"""Retain plan identities and ordering across action-boundary changes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.execution_models.plan_codec import _plan_from_dict
from ww.plan import (
    ChildWorkflowRun,
    LoopBoundary,
    WorkflowHandoff,
    WorkflowPlanCompiler,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures/action_plan_compatibility.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_existing_plan_compilation_and_decoding_are_compatible(
    tmp_path: Path,
    case: dict,
) -> None:
    configuration = tmp_path / "ww-agentic-workflows.yaml"
    configuration.write_text(case["yaml"], encoding="utf-8")
    plan = WorkflowPlanCompiler(
        load_configuration(configuration), tmp_path, "codex", "TEST-1"
    ).compile(case["workflow"])
    normalized = json.loads(json.dumps(plan.to_dict()).replace(str(tmp_path), "<ROOT>"))
    assert normalized == case["plan"]
    restored = _plan_from_dict(case["plan"])
    assert restored.to_dict() == case["plan"]
    core_types = {
        LoopBoundary.kind: LoopBoundary,
        WorkflowHandoff.kind: WorkflowHandoff,
        ChildWorkflowRun.kind: ChildWorkflowRun,
    }
    for item in restored.items:
        serialized = item.to_dict()["operation"]
        assert isinstance(serialized, dict)
        if serialized["type"] in core_types:
            assert isinstance(item.operation, core_types[serialized["type"]])
