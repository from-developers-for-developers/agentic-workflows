# SPDX-License-Identifier: GPL-3.0-or-later
"""Which workflows ask for a particular worker, and therefore want `auto`."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.extensions import ExtensionRegistry
from ww.workflow_config import delegation_requests


def _workflows(tmp_path: Path, source: str) -> dict[str, tuple[str, ...]]:
    (tmp_path / "ww.yaml").write_text(source, encoding="utf-8")
    configuration = load_configuration(
        tmp_path / "ww.yaml", ExtensionRegistry.discover(tmp_path)
    )
    return {
        workflow.name: delegation_requests(workflow)
        for workflow in configuration.workflows
    }


def test_a_workflow_with_no_requests_wants_nothing(tmp_path: Path) -> None:
    found = _workflows(
        tmp_path,
        """
workflows:
  - name: plain
    steps:
      - develop: Implement it.
      - document: Write it up.
""",
    )

    assert found["plain"] == ()


@pytest.mark.parametrize("field", ["agent", "model", "reasoning"])
def test_any_execution_setting_on_a_step_counts(tmp_path: Path, field: str) -> None:
    found = _workflows(
        tmp_path,
        f"""
workflows:
  - name: task
    steps:
      - develop: Implement it.
      - review: Review it.
        {field}: something
""",
    )

    assert found["task"] == ("review",)


def test_a_request_on_the_workflow_itself_is_reported_under_its_name(
    tmp_path: Path,
) -> None:
    found = _workflows(
        tmp_path,
        """
workflows:
  - name: task
    model: strongest
    steps:
      - develop: Implement it.
""",
    )

    assert found["task"] == ("task",)


def test_requests_nested_in_loops_items_and_assessments_are_found(
    tmp_path: Path,
) -> None:
    found = _workflows(
        tmp_path,
        """
workflows:
  - name: deep
    steps:
      - polish:
        loop:
          - fix: Fix what the review found.
            reasoning: high
      - collect: Review the change.
        items:
          description: One item per finding.
          steps:
            - handle: Handle one finding.
              model: cheapest
      - parent:
        steps:
          - nested: A nested child step.
            agent: codex
""",
    )

    assert found["deep"] == ("fix", "handle", "nested")


def test_a_profile_is_a_worker_request_too(tmp_path: Path) -> None:
    found = _workflows(
        tmp_path,
        """
profiles:
  careful: Take your time and verify every claim.

workflows:
  - name: task
    steps:
      - review: Review it.
        profile: careful
""",
    )

    assert found["task"] == ("review",)
