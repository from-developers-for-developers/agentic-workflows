# SPDX-License-Identifier: GPL-3.0-or-later
"""Small service-result contracts shared with output adapters."""

from __future__ import annotations

from dataclasses import dataclass

from ww.executable import DEFAULT_EXECUTABLE
from ww.items import WorkItem


@dataclass(frozen=True)
class ResetResult:
    task_id: str
    removed: bool


# Note added by init when ww-agentic-workflows.yaml defines no workflow.
NO_WORKFLOWS_ACTION = "Define at least one workflow in ww-agentic-workflows.yaml."
# What every init ends with: the skill that sets ww up for the people using it.
INITIALIZATION_NEXT_STEP = (
    "Run the ww-setup skill to set ww up for you, your team and this project."
)


@dataclass(frozen=True)
class InitializationResult:
    root: str
    created: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    # The operator has not been shown the agent-permission notice yet.
    permission_notice: bool = True
    # The ww binary the project is configured to run.
    executable: str = DEFAULT_EXECUTABLE
    # The command prefixes an agent must allow to run ww without asking.
    commands: tuple[str, ...] = ()
    # For each set-up agent whose permission format ww knows: the agent, its
    # permissions file, and the JSON to merge into that file.
    permissions: tuple[tuple[str, str, str], ...] = ()
    # The set-up agents whose permission format ww does not know.
    other_agents: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupResult:
    removed_locks: int


@dataclass(frozen=True)
class ItemUpdateResult:
    item: WorkItem
    continuation_command: str | None


@dataclass(frozen=True)
class TaskStatus:
    """Compact, current-state view for a workflow task."""

    task_id: str
    workflow: str
    step: str | None
    step_state: str
    runtime: str
    agent: str | None
    model: str
    reasoning: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "task_id": self.task_id,
            "workflow": self.workflow,
            "step": self.step,
            "step_state": self.step_state,
            "runtime": self.runtime,
            "agent": self.agent,
            "model": self.model,
            "reasoning": self.reasoning,
        }
