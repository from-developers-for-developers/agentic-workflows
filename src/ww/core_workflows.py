# SPDX-License-Identifier: GPL-3.0-or-later
"""Core workflows ww provides to every project in ``ww-agentic-workflows.yaml``.

``catchall`` records a change no configured workflow covers. It exists so that
every change goes through ww, including the small ones an agent would
otherwise just make, without adding any process to them: the agent works
exactly as it would on a plain prompt and ww keeps the record.

A project turns a core workflow off in ``ww-agentic-workflows.json``
(``"workflows": {"catchall": {"enabled": false}}``) or replaces it by defining
a workflow of the same name in ``ww-agentic-workflows.yaml``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ww.workflow_config import (
    StepDefinition,
    WorkflowConfiguration,
    WorkflowDefinition,
)

if TYPE_CHECKING:
    from ww.project_config import ProjectConfig

CATCHALL = "catchall"

CATCHALL_WORKFLOW = WorkflowDefinition(
    name=CATCHALL,
    description=(
        "Records a change to files that no other workflow covers, adding no "
        "process of its own."
    ),
    steps=(
        StepDefinition(
            name="work",
            description=(
                "This workflow only records the request; it adds no process of "
                "its own. Carry out the request exactly as you would if the "
                "user had asked you directly, without ww: the same judgement, "
                "tools, subagents, skills, and project conventions. Complete "
                "the step when the work is done, with what you changed as the "
                "artifact."
            ),
            # The session that received the prompt does the work, as it would
            # without ww; any subagents it uses along the way are its choice.
            role="manager",
        ),
    ),
    # A plain prompt leaves the agent free to delegate; so does this.
    runtime="auto",
    # A new request on the same task replaces an unfinished one.
    restartable=True,
)

CORE_WORKFLOWS = (CATCHALL_WORKFLOW,)
CORE_WORKFLOW_NAMES = frozenset(workflow.name for workflow in CORE_WORKFLOWS)


def with_core_workflows(
    configuration: WorkflowConfiguration, project_config: ProjectConfig
) -> WorkflowConfiguration:
    """Add each enabled core workflow the project does not define itself."""
    defined = {workflow.name for workflow in configuration.workflows}
    added = tuple(
        workflow
        for workflow in CORE_WORKFLOWS
        if workflow.name not in defined
        and project_config.workflow_enabled(workflow.name)
    )
    if not added:
        return configuration
    return replace(configuration, workflows=(*configuration.workflows, *added))
