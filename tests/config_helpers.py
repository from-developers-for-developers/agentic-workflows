# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared steps for tests that parse a one-workflow configuration text."""

from ww.config import parse_yaml_text
from ww.workflow_config import StepDefinition


def task_workflow(steps: str) -> str:
    """A configuration whose one workflow, ``task``, has the given step lines."""
    return "workflows:\n  - name: task\n    steps:\n" + steps


def task_steps(text: str) -> tuple[StepDefinition, ...]:
    """The parsed steps of the ``task`` workflow in a configuration text."""
    return parse_yaml_text(text).workflows_by_name["task"].steps
