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


# Note added by init when ww.yaml defines no workflow.
NO_WORKFLOWS_ACTION = "Define at least one workflow in ww.yaml."
# What every init ends with: the skill that sets ww up for the people using it.
INITIALIZATION_NEXT_STEP = "Run the ww-setup skill to set ww up for this project."


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
    # What the operator asked to change during the run's latest step that
    # recorded any, and that step, as its worker reported them; shown only
    # when there is something.
    adjustments: str | None = None
    adjustments_step: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        data: dict[str, str | None] = {
            "task_id": self.task_id,
            "workflow": self.workflow,
            "step": self.step,
            "step_state": self.step_state,
            "runtime": self.runtime,
            "agent": self.agent,
            "model": self.model,
            "reasoning": self.reasoning,
        }
        if self.adjustments is not None:
            data["adjustments"] = self.adjustments
            data["adjustments_step"] = self.adjustments_step
        return data
