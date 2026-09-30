# SPDX-License-Identifier: GPL-3.0-or-later
"""Instruction contract and serialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ww.assessments import AssessmentOutcome
from ww.children import ChildTask
from ww.contracts import (
    CallerRole,
    CheckAutomationStatus,
    Control,
    InstructionStatus,
    ItemStatus,
    NextRole,
    OperatorReason,
    PlanItemKind,
    RecoveryAction,
    RuleAutomationStatus,
    StepRole,
)
from ww.execution_models import WorkflowRunSummary
from ww.items import WorkItem
from ww.plan import PlannedMode
from ww.rule_store import RuleApprover
from ww.workflow_config import (
    ChoiceDefinition,
    ItemFieldUpdate,
    ProvidedVariable,
    SavedMetadata,
)


@dataclass(frozen=True)
class RecoveryCommand:
    """One operator choice for an interrupted automatic item."""

    action: RecoveryAction
    command: str

    def to_dict(self) -> dict[str, str]:
        return {"action": self.action, "command": self.command}


@dataclass(frozen=True)
class StepHandover:
    """One completed step's handover, an input to the workflow summary."""

    step: str
    artifact: str
    summary: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {"step": self.step, "artifact": self.artifact, "summary": self.summary}


@dataclass(frozen=True)
class DocumentTask:
    """A document the active step must create or update, with its file."""

    name: str
    instruction: str
    path: str
    exists: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "instruction": self.instruction,
            "path": self.path,
            "exists": self.exists,
        }


@dataclass(frozen=True)
class InteractCommands:
    """The commands of an interactive step, spelled for the role that holds it."""

    operator: str
    agent: str
    choice: str
    end: str
    # Serve the operator page and wait on it; a ``ui`` stage only.
    wait: str
    # Show the step again after the operator paused and returned.
    resume: str

    def to_dict(self) -> dict[str, str]:
        return {
            "operator": self.operator,
            "agent": self.agent,
            "choice": self.choice,
            "end": self.end,
            "wait": self.wait,
            "resume": self.resume,
        }


@dataclass(frozen=True)
class ConversationEntry:
    """One recorded entry of the current step's conversation."""

    at: str
    speaker: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"at": self.at, "speaker": self.speaker, "text": self.text}


@dataclass(frozen=True)
class RuleLine:
    """One rule the active step is given, as its page lists it.

    ``has_command`` rules are checked by ww when the step completes; the
    others the worker follows and reports on in its artifact.
    """

    id: str
    summary: str
    paths: tuple[str, ...] = ()
    has_command: bool = False
    hook: bool = False
    interpretation: str | None = None
    # The approved derived check that checks a rule without a command.
    check: str | None = None
    # A verifier judges the rule while an undecided proposal for it waits.
    pending_operator: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "summary": self.summary,
            "paths": list(self.paths),
            "has_command": self.has_command,
            "hook": self.hook,
            "interpretation": self.interpretation,
            "check": self.check,
            "pending_operator": self.pending_operator,
        }


@dataclass(frozen=True)
class FixFailure:
    """One check that failed when the worker last completed the step."""

    id: str
    hook: bool
    text: str | None
    command: str
    output: str
    # A verifier's verdict rather than a command: ``output`` is its evidence.
    judged: bool = False
    # For a derived check: the rules it covers, whose texts are ``text``.
    covers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "hook": self.hook,
            "text": self.text,
            "command": self.command,
            "output": self.output,
            "judged": self.judged,
            "covers": list(self.covers),
        }


@dataclass(frozen=True)
class DisputeView:
    """A worker's dispute of a check, as the operator decides it.

    ``failure`` is the disputed check as it last failed, ``reason`` the
    worker's argument, ``attempt`` the rejected completion it answers.
    """

    failure: FixFailure
    reason: str
    attempt: int

    def to_dict(self) -> dict[str, object]:
        return {
            "check": self.failure.to_dict(),
            "reason": self.reason,
            "attempt": self.attempt,
        }


@dataclass(frozen=True)
class CheckPreview:
    """What ``ww check`` found: the step's checks run now, nothing recorded.

    ``failures`` are shown like a fix page; ``passed`` and ``not_applicable``
    name the other checks; ``judged`` are the rules a verifier judges only
    when the step completes; ``waived`` the checks the operator waived.
    """

    task_id: str
    step: str
    checks: int
    failures: tuple[FixFailure, ...] = ()
    passed: tuple[str, ...] = ()
    not_applicable: tuple[str, ...] = ()
    judged: tuple[str, ...] = ()
    waived: tuple[tuple[str, str], ...] = ()
    all_files: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "step": self.step,
            "checks": self.checks,
            "passed": not self.failures,
            "failures": [failure.to_dict() for failure in self.failures],
            "passed_checks": list(self.passed),
            "not_applicable": list(self.not_applicable),
            "judged_at_completion": list(self.judged),
            "waived": dict(self.waived),
            "all_files": self.all_files,
        }


@dataclass(frozen=True)
class VerificationRuleLine:
    """One rule on a verification page, with what the verifier must report."""

    id: str
    text: str
    state: str
    interpretation: str | None = None
    approach: str | None = None
    check: str | None = None
    pending_operator: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "state": self.state,
            "interpretation": self.interpretation,
            "approach": self.approach,
            "check": self.check,
            "pending_operator": self.pending_operator,
        }


@dataclass(frozen=True)
class KnownCheck:
    """A check already in the rule-automation store, which a rule may join."""

    name: str
    status: str
    command: str
    config: tuple[str, ...] = ()
    covers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "command": self.command,
            "config": list(self.config),
            "covers": list(self.covers),
        }


@dataclass(frozen=True)
class VerificationPage:
    """What a verification item's worker needs: the rules and the evidence.

    ``files`` is the verified step's change set, or every file when
    ``all_files`` (no git); ``diff_command`` shows the change itself;
    ``draft_artifact`` is the path of the step worker's held artifact.
    """

    step: str
    rules: tuple[VerificationRuleLine, ...]
    files: tuple[str, ...] = ()
    all_files: bool = False
    diff_command: str | None = None
    draft_artifact: str | None = None
    checks: tuple[KnownCheck, ...] = ()

    @property
    def prepares(self) -> bool:
        """Whether any rule asks for a prepared check, so ``--check-result``."""
        return any(rule.state == "approach-approved" for rule in self.rules)

    def to_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "rules": [rule.to_dict() for rule in self.rules],
            "files": list(self.files),
            "all_files": self.all_files,
            "diff_command": self.diff_command,
            "draft_artifact": self.draft_artifact,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class Proposal:
    """One verifier proposal the operator decides at a ``check_proposed`` stop.

    ``kind`` is ``approach`` (stage A: how a rule would be checked),
    ``check`` (stage B: a prepared command, or a ``revision`` of an approved
    one), or ``ambiguous`` (readings to pick from). ``key`` is what the
    decision commands name: a check name or a rule hash.
    """

    kind: str
    key: str
    rules: tuple[tuple[str, str], ...]
    commands: tuple[RecoveryCommand, ...]
    interpretation: str | None = None
    approach: str | None = None
    check: str | None = None
    extends: bool = False
    command: str | None = None
    assertion: str | None = None
    config: tuple[str, ...] = ()
    proven: bool | None = None
    revision: bool = False
    candidates: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "key": self.key,
            "rules": [{"id": rule_id, "text": text} for rule_id, text in self.rules],
            "commands": [command.to_dict() for command in self.commands],
            "interpretation": self.interpretation,
            "approach": self.approach,
            "check": self.check,
            "extends": self.extends,
            "command": self.command,
            "assert": self.assertion,
            "config": list(self.config),
            "proven": self.proven,
            "revision": self.revision,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True)
class FixRequired:
    """ww refused the step's completion: the failed checks and the count.

    ``attempt`` is the number of rejected completions so far and
    ``max_fixes`` the most any of the step's checks allows.
    """

    attempt: int
    max_fixes: int
    checks: int
    failures: tuple[FixFailure, ...]
    draft_artifact: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "max_fixes": self.max_fixes,
            "checks": self.checks,
            "failures": [failure.to_dict() for failure in self.failures],
            "draft_artifact": self.draft_artifact,
        }


@dataclass(frozen=True)
class CoveredRule:
    """One rule a check covers: its ID in this run's plan, and a wording summary.

    ``id`` is the rule's short text hash when no step of the run declares it.
    """

    id: str
    wording: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "wording": self.wording}


@dataclass(frozen=True)
class ConvertedCheck:
    """A check approved in this run. ``undo`` is the command that revokes an
    automatic approval; an operator's approval has none."""

    name: str
    status: CheckAutomationStatus
    rules: tuple[CoveredRule, ...]
    command: str
    config: tuple[str, ...]
    proven: bool
    approved_by: RuleApprover | None
    undo: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "rules": [rule.to_dict() for rule in self.rules],
            "command": self.command,
            "config": list(self.config),
            "proven": self.proven,
            "approved_by": self.approved_by,
            "undo": self.undo,
        }


@dataclass(frozen=True)
class UndecidedProposal:
    """A proposal of this run still waiting for the operator (under ``auto``)."""

    kind: Literal["check", "rule"]
    key: str
    status: CheckAutomationStatus | RuleAutomationStatus
    rules: tuple[CoveredRule, ...]
    command: str | None = None
    proven: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "key": self.key,
            "status": self.status,
            "rules": [rule.to_dict() for rule in self.rules],
            "command": self.command,
            "proven": self.proven,
        }


@dataclass(frozen=True)
class RuleConversions:
    converted: tuple[ConvertedCheck, ...] = ()
    undecided: tuple[UndecidedProposal, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.converted or self.undecided)

    def to_dict(self) -> dict[str, object]:
        return {
            "converted": [check.to_dict() for check in self.converted],
            "undecided": [proposal.to_dict() for proposal in self.undecided],
        }


@dataclass(frozen=True)
class HandoffStep:
    """One item a worker performed in an assignment, as the handoff reports it.

    ``outcome`` is ``completed``, ``loop break``, ``loop continue``,
    ``held for verification``, ``failed``, or ``not completed``; ``checks``
    pairs each check of the last attempt with its status, and ``fix_rounds``
    counts the completions ww rejected.
    """

    name: str
    outcome: str
    artifact: str | None = None
    checks: tuple[tuple[str, str], ...] = ()
    checks_waived: tuple[str, ...] = ()
    fix_rounds: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "outcome": self.outcome,
            "artifact": self.artifact,
            "checks": dict(self.checks),
            "checks_waived": list(self.checks_waived),
            "fix_rounds": self.fix_rounds,
            "error": self.error,
        }


@dataclass(frozen=True)
class HandoffBlock:
    """What ww tells the manager when a worker's assignment ends.

    Built from the saved state, so the manager never depends on free text
    from the worker; the worker's own words are only its short ``summary``.
    ``files`` is ``None`` when no change set was taken: no step of the
    assignment has rules or checks, or the directory has no git.
    """

    task_id: str
    token: str
    steps: tuple[HandoffStep, ...] = ()
    files: tuple[str, ...] | None = None
    summary: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "assignment": self.token,
            "steps": [step.to_dict() for step in self.steps],
            "files": None if self.files is None else list(self.files),
            "summary": self.summary,
            "error": self.error,
        }


@dataclass(frozen=True)
class Instruction:
    """A presentation-ready view derived only from persisted execution state."""

    task_id: str
    workflow: str
    status: InstructionStatus
    item_id: str | None
    item_name: str | None
    stage: str | None
    step: str | None
    parent: str | None
    item_status: ItemStatus | None
    action_kind: PlanItemKind | None
    action_text: str | None
    loop_break_prompt: str | None = None
    loop_break_command: str | None = None
    loop_continue_prompt: str | None = None
    loop_continue_command: str | None = None
    loop_iteration: int | None = None
    loop_max_times: int | None = None
    loop_limit_reached: bool = False
    # The enclosing loop of a body step, so the round can be described.
    loop_name: str | None = None
    # The task requirements saved by ``init``, repeated to every worker.
    task_requirements: str | None = None
    # The most recently completed step's handover and artifact reference, so a
    # worker knows what was just done.  Hooks and the summary never qualify.
    previous_step: str | None = None
    previous_step_artifact: str | None = None
    previous_step_summary: str | None = None
    # The completion of this step must include a summary for the next step.
    summary_required: bool = False
    # For the built-in workflow summary: every step's handover in this run.
    run_handovers: tuple[StepHandover, ...] = ()
    # For pending input: the steps completed since the waiting handler last
    # ran in this run, so the values describe current work.
    input_context: tuple[StepHandover, ...] = ()
    # Documents the active step promised to create or update in place.
    documents: tuple[DocumentTask, ...] = ()
    # An interactive step: its conversation state and the recording commands.
    interactive: bool = False
    interaction_entries: int = 0
    interaction_ended: bool = False
    interact_commands: InteractCommands | None = None
    # Operator choices of an interactive step, the agent's way to offer them,
    # and what was chosen so far.
    choices: tuple[ChoiceDefinition, ...] = ()
    choice_mechanism: str | None = None
    chosen: str | None = None
    # The operator said they are done for now; the agent stops until they
    # return.
    operator_paused: bool = False
    # The conversation of this step so far, as recorded.
    conversation: tuple[ConversationEntry, ...] = ()
    # A per-item stage answered on the operator page rather than in a
    # conversation; the page is an extra the core knows only by this flag.
    ui: bool = False
    # A collection step of a shared item flow: the items it reconciles.
    shared_items: bool = False
    stored_items: tuple[WorkItem, ...] = ()
    # Custom item fields this step must set, and the flow's identity and
    # unique fields when this step is the collection.
    required_item_fields: tuple[ItemFieldUpdate, ...] = ()
    collects_items: bool = False
    item_identity: str | None = None
    item_unique: tuple[str, ...] = ()
    # Pending input on an input-only assignment: the manager supplies it.
    manager_input: bool = False
    required_values: tuple[ProvidedVariable, ...] = ()
    # What a retried handler was given last time, per requested value.
    previous_values: tuple[tuple[str, str], ...] = ()
    required_metadata: tuple[SavedMetadata, ...] = ()
    automatic_context: tuple[str, ...] = ()
    has_previous_artifacts: bool = False
    next_steps: tuple[str, ...] = ()
    artifact: str | None = None
    continuation_command: str | None = None
    error: str | None = None
    handoff: str | None = None
    # The workflow a completed run offers the operator next.
    recommended_workflow: str | None = None
    # A completed run's "Rules converted in this run", built from the store.
    rule_conversions: RuleConversions = RuleConversions()
    # An assessment's answers and what each does: on the assessment's own
    # page, and on the page that asks ``next`` for the chosen one.
    assessment_outcomes: tuple[AssessmentOutcome, ...] = ()
    # This page asks for the outcome of the named, completed assessment.
    choosing_outcome_of: str | None = None
    profile_instruction: str | None = None
    run_id: str | None = None
    task_runs: tuple[WorkflowRunSummary, ...] = ()
    working_directory: str | None = None
    child_tasks: tuple[ChildTask, ...] = ()
    operation_id: str | None = None
    recovery_commands: tuple[RecoveryCommand, ...] = ()
    workflow_runtime: str = "single"
    # The agent the task was started for, as ``start --agent`` named it.
    agent: str = ""
    workflow_runtime_instruction: tuple[str, ...] = ()
    model: str = "auto"
    reasoning: str = "auto"
    requested_agent: str | None = None
    requested_model: str | None = None
    requested_reasoning: str | None = None
    requested_profile: str | None = None
    role: StepRole = "worker"
    # ``False``: whoever performs the item spawns no subagents.
    subagents: bool = True
    selected_agent: str | None = None
    selected_model: str | None = None
    selected_reasoning: str | None = None
    assignment_preview: dict[str, object] | None = None
    # The first stage of a multi-stage item assignment describes its scope;
    # later stages of the same assignment are rendered compactly.
    assignment_scope: dict[str, object] | None = None
    continues_assignment: bool = False
    # The assignment the current item belongs to, in the ``auto`` runtime: the
    # step that drives worker selection, every agent or input item it covers,
    # and whether the current item is a later one the worker already holds.
    assignment_step: str | None = None
    assignment_items: tuple[str, ...] = ()
    assignment_continues: bool = False
    manager_intro: bool = False
    completion_registered: bool = False
    # The completion was accepted but held: verifiers judge its rules first.
    completion_held: bool = False
    caller_role: CallerRole | None = None
    next_role: NextRole | None = None
    control: Control | None = None
    # Set exactly when ``control`` is ``awaiting_operator``.
    operator_reason: OperatorReason | None = None
    result_saved: bool | None = None
    # The rules the active step is given, and, after a rejected completion,
    # what failed; ``checks_waived`` are the checks the operator waived for
    # the step, each with the reason; ``dispute`` is the worker's objection
    # to one at a ``check_disputed`` stop.
    rules: tuple[RuleLine, ...] = ()
    # The modes the active step works in: the run's selected modes, then the
    # automatic ones whose filters admit the step.
    modes: tuple[PlannedMode, ...] = ()
    fix_required: FixRequired | None = None
    checks_waived: tuple[tuple[str, str], ...] = ()
    dispute: DisputeView | None = None
    # A worker asked for an item the manager performs itself (``role:
    # manager`` or ``interactive`` in the ``auto`` runtime): the page names it
    # and offers no completion command.
    manager_only: bool = False
    # The open assignment's token in the ``auto`` runtime, carried by every
    # worker command the page prints.
    assignment_token: str | None = None
    # The worker's assignment ended with this command: ww's report of it,
    # which the worker returns to the manager verbatim.
    handoff_block: HandoffBlock | None = None
    # A verification item's rules and evidence, and, at a ``check_proposed``
    # stop, the proposals the operator decides.
    verification: VerificationPage | None = None
    proposals: tuple[Proposal, ...] = ()
    # Internal capability markers let presentation and service refresh paths
    # avoid rediscovering a saved plan or matching built-in action names.
    is_child_workflow_control: bool = False
    is_loop_control: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "workflow": self.workflow,
            "status": self.status,
            "item_id": self.item_id,
            "item_name": self.item_name,
            "stage": self.stage,
            "step": self.step,
            "parent": self.parent,
            "item_status": self.item_status,
            "action_kind": self.action_kind,
            "action_text": self.action_text,
            "loop_break_prompt": self.loop_break_prompt,
            "loop_break_command": self.loop_break_command,
            "loop_continue_prompt": self.loop_continue_prompt,
            "loop_continue_command": self.loop_continue_command,
            "loop_iteration": self.loop_iteration,
            "loop_max_times": self.loop_max_times,
            "loop_limit_reached": self.loop_limit_reached,
            "loop_name": self.loop_name,
            "task_requirements": self.task_requirements,
            "previous_step": self.previous_step,
            "previous_step_artifact": self.previous_step_artifact,
            "previous_step_summary": self.previous_step_summary,
            "summary_required": self.summary_required,
            "run_handovers": [item.to_dict() for item in self.run_handovers],
            "input_context": [item.to_dict() for item in self.input_context],
            "documents": [item.to_dict() for item in self.documents],
            "interactive": self.interactive,
            "interaction_entries": self.interaction_entries,
            "interaction_ended": self.interaction_ended,
            "interact_commands": (
                self.interact_commands.to_dict() if self.interact_commands else None
            ),
            "choices": [choice.to_dict() for choice in self.choices],
            "choice_mechanism": self.choice_mechanism,
            "chosen": self.chosen,
            "operator_paused": self.operator_paused,
            "conversation": [entry.to_dict() for entry in self.conversation],
            "ui": self.ui,
            "shared_items": self.shared_items,
            "stored_items": [item.to_dict() for item in self.stored_items],
            "required_item_fields": [
                field.to_dict() for field in self.required_item_fields
            ],
            "collects_items": self.collects_items,
            "item_identity": self.item_identity,
            "item_unique": list(self.item_unique),
            "manager_input": self.manager_input,
            "required_values": [value.to_dict() for value in self.required_values],
            "previous_values": dict(self.previous_values),
            "required_metadata": [value.to_dict() for value in self.required_metadata],
            "automatic_context": list(self.automatic_context),
            "has_previous_artifacts": self.has_previous_artifacts,
            "next_steps": list(self.next_steps),
            "artifact": self.artifact,
            "continuation_command": self.continuation_command,
            "error": self.error,
            "handoff": self.handoff,
            "recommended_workflow": self.recommended_workflow,
            "rule_conversions": self.rule_conversions.to_dict(),
            "assessment_outcomes": [
                {
                    "label": outcome.label,
                    "stops": outcome.stops,
                    "first_step": outcome.first_step,
                }
                for outcome in self.assessment_outcomes
            ],
            "choosing_outcome_of": self.choosing_outcome_of,
            "profile_instruction": self.profile_instruction,
            "run_id": self.run_id,
            "task_runs": [item.to_dict() for item in self.task_runs],
            "working_directory": self.working_directory,
            "child_tasks": [child.to_dict() for child in self.child_tasks],
            "operation_id": self.operation_id,
            "recovery_commands": [item.to_dict() for item in self.recovery_commands],
            "workflow_runtime": self.workflow_runtime,
            "agent": self.agent,
            "workflow_runtime_instruction": list(self.workflow_runtime_instruction),
            "model": self.model,
            "reasoning": self.reasoning,
            "requested_agent": self.requested_agent,
            "requested_model": self.requested_model,
            "requested_reasoning": self.requested_reasoning,
            "requested_profile": self.requested_profile,
            "role": self.role,
            "subagents": self.subagents,
            "selected_agent": self.selected_agent,
            "selected_model": self.selected_model,
            "selected_reasoning": self.selected_reasoning,
            "assignment_preview": self.assignment_preview,
            "assignment_scope": self.assignment_scope,
            "continues_assignment": self.continues_assignment,
            "assignment_step": self.assignment_step,
            "assignment_items": list(self.assignment_items),
            "assignment_continues": self.assignment_continues,
            "manager_intro": self.manager_intro,
            "completion_registered": self.completion_registered,
            "completion_held": self.completion_held,
            "caller_role": self.caller_role,
            "next_role": self.next_role,
            "control": self.control,
            "operator_reason": self.operator_reason,
            "result_saved": self.result_saved,
            "rules": [rule.to_dict() for rule in self.rules],
            "modes": [mode.to_dict() for mode in self.modes],
            "fix_required": (
                self.fix_required.to_dict() if self.fix_required else None
            ),
            "checks_waived": dict(self.checks_waived),
            "dispute": self.dispute.to_dict() if self.dispute else None,
            "manager_only": self.manager_only,
            "assignment_token": self.assignment_token,
            "handoff_block": (
                self.handoff_block.to_dict() if self.handoff_block else None
            ),
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "proposals": [proposal.to_dict() for proposal in self.proposals],
        }
