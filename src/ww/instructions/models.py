# SPDX-License-Identifier: GPL-3.0-or-later
"""Instruction contract and serialization."""

from __future__ import annotations

from dataclasses import dataclass

from ww.assessments import AssessmentOutcome
from ww.children import ChildTask
from ww.contracts import (
    CallerRole,
    Control,
    InstructionStatus,
    ItemStatus,
    PlanItemKind,
    RecoveryAction,
)
from ww.execution_models import WorkflowRunSummary
from ww.items import WorkItem
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
    caller_role: CallerRole | None = None
    next_role: CallerRole | None = None
    control: Control | None = None
    result_saved: bool | None = None
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
            "caller_role": self.caller_role,
            "next_role": self.next_role,
            "control": self.control,
            "result_saved": self.result_saved,
        }
