# SPDX-License-Identifier: GPL-3.0-or-later
"""The shape of a provider of per-step template values."""

from __future__ import annotations

from collections.abc import Callable

from ww.execution_models import ExecutionState
from ww.plan import PlanItem, WorkflowPlan

# Template values one step reads about what it works on, such as
# ``{{ww.child.*}}`` for a per-child stage or ``{{ww.item.*}}`` for a per-item
# stage; empty for a step that works on neither.
StepValues = Callable[[ExecutionState, WorkflowPlan, PlanItem], dict[str, str]]


def no_step_values(
    state: ExecutionState, plan: WorkflowPlan, item: PlanItem
) -> dict[str, str]:
    return {}
