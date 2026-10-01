# SPDX-License-Identifier: GPL-3.0-or-later
"""A plan item to build plans and saved snapshots from in model tests."""

from ww.actions import PlannedAction, Prompt
from ww.plan import PlanItem


def plan_item(**changes: object) -> PlanItem:
    """An agent prompt item named ``step``, with ``changes`` applied."""
    values: dict[str, object] = {
        "id": "workflow:step:step:step:1",
        "position": 1,
        "name": "step",
        "description": "",
        "operation": PlannedAction("prompt", Prompt("Do the work.")),
        "owner": "agent",
        "execution": "agent_instruction",
        "requires_agent_input": False,
        "workflow": "workflow",
        "step": "step",
        "parent": None,
        "phase": "step",
        "source": "step",
        "registered_handler": None,
    }
    values.update(changes)
    return PlanItem(**values)  # type: ignore[arg-type]
