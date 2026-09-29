# SPDX-License-Identifier: GPL-3.0-or-later
"""Persisted, plan-driven workflow execution models.

This facade preserves the public import surface while the persistence records,
run aggregates, plan codec, and state construction live in focused modules.
"""

from .construction import (
    build_step_projection,
    initial_state,
    new_item_execution,
    operation_scope_for,
)
from .records import (
    EXECUTION_SCHEMA_VERSION,
    CheckReport,
    CheckResult,
    CommandExecution,
    ExecutionState,
    InputRequest,
    PlanItemExecution,
    ProjectMetadataPublication,
    StepProgress,
)
from .runs import (
    PLAN_COMPILER_VERSION,
    PLAN_SCHEMA_VERSION,
    PlanSnapshot,
    TaskRunAggregate,
    WorkflowRunSummary,
    validate_task_runs,
)

__all__ = [
    "CheckReport",
    "CheckResult",
    "EXECUTION_SCHEMA_VERSION",
    "PLAN_COMPILER_VERSION",
    "PLAN_SCHEMA_VERSION",
    "CommandExecution",
    "ExecutionState",
    "InputRequest",
    "PlanItemExecution",
    "PlanSnapshot",
    "ProjectMetadataPublication",
    "StepProgress",
    "TaskRunAggregate",
    "WorkflowRunSummary",
    "build_step_projection",
    "initial_state",
    "new_item_execution",
    "operation_scope_for",
    "validate_task_runs",
]
