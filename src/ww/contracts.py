# SPDX-License-Identifier: GPL-3.0-or-later
"""Closed-set type contracts shared by plans, execution, and presentation."""

from __future__ import annotations

from typing import Literal

# One condition of a command's ``assert`` list.
AssertionKind = Literal["empty", "equals"]
RequestedActionKind = Literal["mcp", "prompt", "skill", "slash_command"]
HookPhase = Literal[
    "before_start_workflow",
    "before_start",
    "before_complete",
    "after_complete",
    "before_complete_workflow",
]
PlanItemPhase = Literal[
    "before_start_workflow",
    "before_start",
    "step",
    "before_complete",
    "after_complete",
    "before_complete_workflow",
]
HookScope = Literal["global", "workflow", "step"]
# What a failed ``before_complete`` hook does: stop for the operator, as any
# failed handler does, or send the step back to its worker to fix.
HookFailure = Literal["fix", "operator"]
# The outcome of one check ww ran when a step completed.
CheckStatus = Literal["passed", "failed", "not_applicable"]
# Where a check came from: a rule's own command, a ``fix`` hook, a command
# the operator approved into the rule-automation store, or a verifier's
# verdict on a rule without a command.
CheckSource = Literal["rule", "hook", "derived", "judged"]
# Why a failed run failed, when the reason is not the item's own error.
FailureKind = Literal["fix_limit", "check_disputed", "value_unavailable"]
# A rule's standing in the rule-automation store, keyed by its text hash.
RuleAutomationStatus = Literal[
    "approach_proposed",
    "approach_approved",
    "interpreted",
    "proposed",
    "converted",
    "rejected",
    "not_convertible",
    "ambiguous",
]
# A derived check's standing in the rule-automation store.
CheckAutomationStatus = Literal["proposed", "converted", "rejected"]
# How a step's rule without a command of its own is enforced, decided when
# the step begins: by a converted derived check, or by a verifier's verdict.
RuleResolutionStatus = Literal["converted", "judged"]
Verdict = Literal["pass", "fail"]
ItemOperation = Literal[
    "collect", "process_item", "resolve_item", "report_item", "handle_item"
]
# ``handle_item`` is the built-in stage of a bare ``items`` step; it cannot be
# declared on a configured step.
# How the per-item stages of an ``items`` step are split into worker
# assignments in the ``auto`` runtime.
ItemAssignment = Literal["together", "per_item", "per_step"]
# How the body steps of a ``loop`` are split into worker assignments in the
# ``auto`` runtime: one assignment per body step, or one per loop round for
# consecutive steps that resolve to the same worker settings.
LoopAssignment = Literal["per_round", "per_step"]
DEFAULT_LOOP_ASSIGNMENT: LoopAssignment = "per_round"
ChildOperation = Literal["collect"]

# Action identifiers are registry keys; third-party internal registrations may
# extend the built-in set without changing generic plan consumers.
PlanItemKind = str
# Task IDs issued while an external identity is still being bound.
BOOTSTRAP_REQUEST_PREFIX = "REQUEST-"
PlanItemOwner = Literal["agent", "ww"]
ExecutionKind = Literal[
    "agent_instruction", "automatic", "loop_control", "workflow_transition"
]
LoopOperation = Literal["enter", "repeat"]

CommandStatus = Literal["pending", "in_progress", "interrupted", "completed", "failed"]
ItemStatus = Literal[
    "pending",
    "in_progress",
    "interrupted",
    "awaiting_input",
    "completed",
    "failed",
]
ExecutionStatus = Literal[
    "pending",
    "in_progress",
    "awaiting_input",
    "interrupted",
    "failed",
    "completed",
    # Closed by a later start of the same workflow; kept as history.
    "abandoned",
]
# A run that is neither finished nor abandoned is the task's open run: it
# blocks another start and is the one every task command addresses.
CLOSED_RUN_STATUSES: frozenset[str] = frozenset({"completed", "abandoned"})


def run_is_open(status: str) -> bool:
    return status not in CLOSED_RUN_STATUSES


StepStatus = Literal["pending", "in_progress", "interrupted", "completed", "failed"]
# ``skipped``: a ``break`` on a per-child parent stage ended the children
# before this one started.
ChildStatus = Literal[
    "pending", "starting", "in_progress", "completed", "failed", "skipped"
]
RunStatus = Literal["pending", "in_progress", "completed", "failed"]
InstructionStatus = Literal[
    "pending",
    "in_progress",
    "awaiting_input",
    "interrupted",
    "failed",
    "completed",
    "abandoned",
    "task_summary",
]
RecoveryAction = Literal["retry", "force"]
CallerRole = Literal["manager", "worker"]
# Who performs an agent step: the manager in its own session, or a worker it
# delegates to. Both are also the caller roles.
StepRole = Literal["manager", "worker"]
# Who acts next. The operator is the human ww waits for; never a caller role.
NextRole = Literal["manager", "worker", "operator"]
Control = Literal["continue_worker", "handoff_manager", "blocked", "awaiting_operator"]
# Why a task waits for the operator.
OperatorReason = Literal[
    "handler_failed",
    "work_failed",
    "child_failed",
    "handler_interrupted",
    "loop_limit",
    "fix_limit",
    "check_disputed",
    "value_unavailable",
    "plan_changed",
]
CALLER_ROLES: tuple[CallerRole, ...] = ("manager", "worker")
CONTROL_VALUES: tuple[Control, ...] = (
    "continue_worker",
    "handoff_manager",
    "blocked",
    "awaiting_operator",
)
