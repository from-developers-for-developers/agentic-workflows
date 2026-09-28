# SPDX-License-Identifier: GPL-3.0-or-later
"""Small service-result contracts shared with output adapters."""

from __future__ import annotations

from dataclasses import dataclass

from ww.items import WorkItem


@dataclass(frozen=True)
class ResetResult:
    task_id: str
    removed: bool


# The setup note initialization adds while workflows.yaml defines no workflow.
NO_WORKFLOWS_ACTION = "Define at least one workflow in workflows.yaml."


@dataclass(frozen=True)
class InitializationResult:
    root: str
    created: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    # The operator has not been shown the agent-permission notice yet.
    permission_notice: bool = True


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
