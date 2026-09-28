# SPDX-License-Identifier: GPL-3.0-or-later
"""Storage-backed construction of caller-facing instructions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ww.agents import choice_mechanism
from ww.assessments import assessment_outcomes, pending_assessment
from ww.assignments import (
    Assignment,
    ItemSpan,
    LoopSpan,
    active_assignment,
    assignment_at,
    completion_window,
    input_only,
    item_span,
    loop_span,
    selection_item,
)
from ww.contracts import (
    CallerRole,
    Control,
    InstructionStatus,
    ItemStatus,
    NextRole,
    OperatorReason,
)
from ww.control import child_workflow, is_coordinator, loop_control
from ww.documents import DocumentStore
from ww.errors import StateError
from ww.execution_models import ExecutionState, PlanItemExecution, PlanSnapshot
from ww.interactions import InteractionLog
from ww.operations import LoopBoundary
from ww.plan import PlanItem, WorkflowPlan
from ww.runtimes import runtime_instruction
from ww.storage_adapters import TaskStorageAdapter
from ww.transitions import enclosing_loop_entry_index, loop_limit_reached
from ww.workflow_config import INIT_STEP_NAME, ProvidedVariable
from ww.workspace import resolve_workspace

from .commands import (
    child_start_command,
    complete_command,
    force_command,
    instruction_command,
    interact_commands,
    next_command,
    recovery_commands,
)
from .models import (
    ConversationEntry,
    DocumentTask,
    Instruction,
    RecoveryCommand,
    StepHandover,
)
from .policy import (
    _control,
    _has_previous_artifacts,
    _index_for_id,
    _instruction_status,
    _item_status,
    _next_steps,
    _plan_item_kind,
    _result_saved,
    operator_reason,
)
from .text import _stage, action_text

TaskValues = Callable[[ExecutionState, WorkflowPlan], dict[str, str]]


@dataclass(frozen=True)
class _Selection:
    """The worker actually chosen: recorded on the item, else on the assignment."""

    agent: str | None
    model: str | None
    reasoning: str | None

    @classmethod
    def effective(
        cls, record: PlanItemExecution | None, state: ExecutionState
    ) -> _Selection:
        return cls(
            (record.selected_agent if record else None)
            or state.assignment_selected_agent,
            (record.selected_model if record else None)
            or state.assignment_selected_model,
            (record.selected_reasoning if record else None)
            or state.assignment_selected_reasoning,
        )


class InstructionBuilder:
    """Build caller-facing instructions from authoritative run records."""

    def __init__(
        self,
        tasks: TaskStorageAdapter,
        task_values: TaskValues,
        *,
        root: Path,
        documents: DocumentStore,
        interactions: InteractionLog,
    ) -> None:
        self.tasks = tasks
        self.documents = documents
        self.interactions = interactions
        # Persisted paths are project-relative; instructions print them
        # absolute for the filesystem this process runs in.
        self.root = root
        self.task_values = task_values

    def build(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        caller_role: CallerRole | None = None,
    ) -> Instruction:
        plan = snapshot.plan
        item = plan.items[state.cursor] if state.cursor < len(plan.items) else None
        record = (
            state.item_executions[state.cursor]
            if state.cursor < len(state.item_executions)
            else None
        )
        control, next_role = _control(state, plan)
        selection = _Selection.effective(record, state)
        automatic_running = (
            item is not None
            and record is not None
            and item.owner == "ww"
            and record.status == "in_progress"
        )
        assignment = _current_assignment(state, plan, item)
        # A hook shares its assignment with its step, so the worker shape and
        # the delegate heading come from the step that drives selection, not
        # from whichever item the cursor happens to be on.
        driver = selection_item(plan, assignment) if assignment is not None else None
        shape = driver or item
        span = plan.items[assignment.start : assignment.stop] if assignment else ()
        covered = tuple(
            entry
            for entry in span
            if entry.owner == "agent" or entry.requires_agent_input
        )
        built = self._build(state, snapshot)
        choosing = pending_assessment(state, plan)
        return replace(
            built,
            assessment_outcomes=(
                choosing.outcomes
                if choosing is not None
                else assessment_outcomes(plan, state.cursor)
                if item is not None and item.assessment_question is not None
                else ()
            ),
            choosing_outcome_of=(
                plan.items[choosing.index].name if choosing is not None else None
            ),
            continuation_command=(
                next_command(state.task_id, outcome="<outcome>")
                if choosing is not None
                else built.continuation_command
            ),
            run_id=state.run_id,
            workflow_runtime=state.workflow_runtime,
            agent=state.agent,
            model=(record.model if record and record.model else None)
            or (item.model if item else None)
            or state.model,
            reasoning=(record.reasoning if record and record.reasoning else None)
            or (item.reasoning if item else None)
            or state.reasoning,
            requested_agent=shape.requested_agent if shape else None,
            requested_model=shape.requested_model if shape else None,
            requested_reasoning=shape.requested_reasoning if shape else None,
            requested_profile=shape.profile if shape else None,
            subagents=shape.subagents if shape else True,
            profile_instruction=(
                built.profile_instruction
                or (
                    profile_instruction(driver, self.root)
                    if driver is not None and built.item_status == "in_progress"
                    else None
                )
            ),
            selected_agent=selection.agent,
            selected_model=selection.model,
            selected_reasoning=selection.reasoning,
            # Which work comes next depends on the outcome still to be chosen.
            assignment_preview=(
                None
                if choosing is not None
                else _assignment_preview(state, plan, item, next_role)
            ),
            assignment_step=driver.name if driver else None,
            assignment_items=tuple(entry.name for entry in covered),
            assignment_continues=(
                item is not None
                and bool(covered)
                and item.id != covered[0].id
                and item.id in {entry.id for entry in covered}
            ),
            caller_role=caller_role,
            next_role=next_role,
            control=control,
            operator_reason=operator_reason(state, plan),
            result_saved=(
                _result_saved(state, plan)
                if state.status in {"failed", "interrupted"} or automatic_running
                else None
            ),
            workflow_runtime_instruction=_guidance(state, shape, selection, next_role),
            is_child_workflow_control=item is not None
            and child_workflow(item) is not None,
            is_loop_control=item is not None and loop_control(item) is not None,
        )

    def _build(self, state: ExecutionState, snapshot: PlanSnapshot) -> Instruction:
        """Describe the current item; ``build`` layers the shared fields on top."""
        plan = snapshot.plan
        if state.status == "completed":
            handoff = self.tasks.read_handoff(state.task_id)
            return replace(
                _base(state, None),
                handoff=handoff.rstrip() if handoff else None,
                recommended_workflow=plan.recommended_next_workflow,
            )
        if state.status == "awaiting_input":
            return self._awaiting_input(state, plan)
        if state.status in {"failed", "interrupted"}:
            return self._stopped(state, plan)
        if state.cursor >= len(plan.items):
            return replace(
                _base(state, None, status="completed"),
                recommended_workflow=plan.recommended_next_workflow,
            )
        item = plan.items[state.cursor]
        record = state.item_executions[state.cursor]
        if child_workflow(item) is not None:
            return self._child_control(state, item, record)
        loop = loop_control(item)
        if loop is not None:
            return _loop_control(state, item, loop)
        if record.status == "in_progress":
            if item.owner == "ww" and item.execution == "automatic":
                return _automatic_running(state, item, record)
            return self._active(state, plan, item, record)
        return replace(
            _base(state, item, item_status="pending"),
            continuation_command=next_command(state.task_id),
        )

    def _awaiting_input(self, state: ExecutionState, plan: WorkflowPlan) -> Instruction:
        request = state.pending_input_request
        if request is None:  # pragma: no cover - validated state invariant
            raise ValueError("awaiting-input state requires an input request")
        index = _index_for_id(plan, request.item_id)
        item = plan.items[index]
        requested = {value.name for value in request.values}
        # A retried handler asks for its values again; what it failed with is
        # kept on its record so the page can show it.
        previous_values = tuple(
            (name, value)
            for name, value in state.item_executions[index].supplied_values
            if name in requested
        )
        # The values describe the work done since this handler last ran in
        # this run: an earlier loop round's execution is in the history.
        last_run = max(
            (
                str(record.completed_at)
                for record in state.execution_history
                if record.plan_item_id == item.id and record.completed_at
            ),
            default="",
        )
        input_context = tuple(
            StepHandover(
                found.step, str((self.root / found.artifact).resolve()), found.summary
            )
            for found in self._handovers(state, plan)
            if found.completed_at > last_run
        )
        assignment = active_assignment(
            plan, state.assignment_item_id, runtime=state.workflow_runtime
        )
        manager_input = (
            state.workflow_runtime == "auto"
            and assignment is not None
            and input_only(plan, assignment)
        )
        return replace(
            _base(state, item, item_status="awaiting_input"),
            required_values=request.values,
            previous_values=previous_values,
            automatic_context=(item.name,),
            continuation_command=complete_command(
                state.task_id,
                request.values,
                # Values are the whole result of an input-only assignment.
                artifact=not manager_input,
                role="manager" if manager_input else "worker",
            ),
            manager_input=manager_input,
            input_context=input_context,
        )

    def _previous_step_result(
        self, state: ExecutionState, plan: WorkflowPlan
    ) -> _StepResult | None:
        """The handover of the step completed most recently before this one.

        The full result stays in the artifact; only its reference travels.
        """
        handovers = self._handovers(state, plan)
        return handovers[-1] if handovers else None

    def _handovers(
        self, state: ExecutionState, plan: WorkflowPlan
    ) -> tuple[_StepResult, ...]:
        """Every completed ordinary step's handover in this run, oldest first.

        Only ordinary steps count: hooks, the built-in summary, and ``init``
        (already shown as the requirements) are skipped.  Loop history is
        included, so every round of a loop is present in order.
        """
        by_id = {entry.id: entry for entry in plan.items}
        candidates = sorted(
            (
                (record.completed_at, record.position, entry, record)
                for record in (*state.item_executions, *state.execution_history)
                if record.status == "completed"
                and record.artifact is not None
                and record.completed_at is not None
                and (entry := by_id.get(record.plan_item_id)) is not None
                and entry.hands_over
            ),
            key=lambda found: found[:2],
        )
        return tuple(
            _StepResult(
                entry.name,
                str(record.artifact),
                record.summary_for_next,
                str(record.completed_at),
            )
            for _, _, entry, record in candidates
        )

    def _conversation(
        self, state: ExecutionState, item: PlanItem
    ) -> tuple[ConversationEntry, ...]:
        """The recorded entries of this step's conversation in this run."""
        return tuple(
            ConversationEntry(entry.at, entry.speaker, entry.text)
            for entry in self.interactions.entries(state.task_id)
            if (entry.run_id, entry.step, entry.item_id)
            == (state.run_id, item.name, item.item_id)
        )

    def _item_values(self, state: ExecutionState, item: PlanItem) -> dict[str, str]:
        """``item.*`` and ``field.*`` for a per-item stage's own work item."""
        if item.item_id is None:
            return {}
        work = next(
            (
                entry
                for entry in self.tasks.read_items(state.task_id, state.run_id)
                if entry.id == item.item_id
            ),
            None,
        )
        if work is None:
            return {}
        return {
            "item.id": work.id,
            "item.text": work.item,
            **{f"field.{name}": value for name, value in work.fields},
        }

    def _document_tasks(
        self, plan: WorkflowPlan, item: PlanItem, state: ExecutionState
    ) -> tuple[DocumentTask, ...]:
        """The documents this item promised to update, with their current files."""
        declared = {document.name: document for document in plan.documents}
        workspace = resolve_workspace(self.root, state.working_directory)
        tasks = []
        for update in item.update_document:
            document = declared.get(update.name)
            if document is None:
                continue
            path = self.documents.path(document, state.task_id, workspace)
            tasks.append(
                DocumentTask(update.name, update.instruction, str(path), path.is_file())
            )
        return tuple(tasks)

    def _requirements(self, state: ExecutionState, plan: WorkflowPlan) -> str | None:
        """The requirements ``init`` saved, so every worker reads the user's ask."""
        record = next(
            (
                state.item_executions[index]
                for index, item in enumerate(plan.items)
                if item.step == INIT_STEP_NAME
                and item.phase == "step"
                and item.owner == "agent"
            ),
            None,
        )
        if record is None or record.artifact is None:
            return None
        try:
            artifact = self.tasks.read_execution_artifact(record.artifact)
        except StateError:
            return None
        return _result_body(artifact) or None

    def _stopped(self, state: ExecutionState, plan: WorkflowPlan) -> Instruction:
        """A failed or interrupted run: report the item and how to recover."""
        current = plan.items[state.cursor] if state.cursor < len(plan.items) else None
        record = (
            state.item_executions[state.cursor]
            if current is not None and state.cursor < len(state.item_executions)
            else None
        )
        interrupted = state.status == "interrupted"
        return replace(
            _base(
                state, current, item_status="interrupted" if interrupted else "failed"
            ),
            error=state.last_error,
            child_tasks=(
                self.tasks.read_children(state.task_id, state.run_id)
                if current is not None and child_workflow(current) is not None
                else ()
            ),
            operation_id=record.operation_id if record else None,
            recovery_commands=(recovery_commands(state.task_id) if current else ()),
        )

    def _child_control(
        self, state: ExecutionState, item: PlanItem, record: PlanItemExecution
    ) -> Instruction:
        children = self.tasks.read_children(state.task_id, state.run_id)
        if record.status == "pending":
            return replace(
                _base(state, item, item_status="pending"),
                child_tasks=children,
                continuation_command=next_command(state.task_id),
                is_child_workflow_control=True,
            )
        active = next(
            (
                child
                for child in children
                if child.status in {"in_progress", "starting"}
            ),
            None,
        )
        pending = next((child for child in children if child.status == "pending"), None)
        if active is not None and active.status == "starting":
            text = (
                f"Child `{active.id}` is obtaining its task ID. Continue it "
                f"with:\n\n```console\n"
                f"{instruction_command(active.id, role='manager')}\n```"
            )
        elif active is not None:
            text = f"Child `{active.id}` is in progress at `{active.task_id}`."
        elif pending is not None:
            text = (
                f"Start pending child `{pending.id}` with:\n\n```console\n"
                f"{child_start_command(state.task_id, pending.id)}\n```"
            )
        else:
            text = "No child task is currently available to start."
        return replace(
            _base(state, item, item_status=record.status),
            action_text=text,
            child_tasks=children,
            is_child_workflow_control=True,
        )

    def _active(
        self,
        state: ExecutionState,
        plan: WorkflowPlan,
        item: PlanItem,
        record: PlanItemExecution,
    ) -> Instruction:
        """The agent item in progress: its work text and completion commands."""
        assignment = active_assignment(
            plan, state.assignment_item_id, runtime=state.workflow_runtime
        )
        required, context = completion_window(
            plan, state.cursor, assignment.stop if assignment else None
        )
        selection = _Selection.effective(record, state)
        span = _span(plan, assignment)
        span_ids = tuple(stage.id for stage in span.stages) if span else ()
        loop_round = _loop_round(state, plan, item)
        role: CallerRole = "manager" if state.workflow_runtime == "auto" else "worker"
        previous = (
            self._previous_step_result(state, plan)
            if item.step != INIT_STEP_NAME
            else None
        )

        def completion(artifact: bool, loop_control: str | None = None) -> str:
            return complete_command(
                state.task_id,
                required,
                artifact,
                item.save_metadata,
                selected_agent=selection.agent,
                selected_model=selection.model,
                selected_reasoning=selection.reasoning,
                loop_control=loop_control,
                summary=item.hands_over,
            )

        loop_break_command = None
        if item.loop_break is not None:
            loop_entry = plan.items[enclosing_loop_entry_index(plan, state.cursor)]
            loop_break_command = completion(
                item.artifact or loop_entry.artifact, "break"
            )
        return replace(
            _base(state, item, item_status=record.status),
            action_text=action_text(
                item,
                {
                    **dict(state.workflow_values),
                    **self.task_values(state, plan),
                    **self._item_values(state, item),
                },
                state.task_id,
            ),
            required_values=required,
            required_metadata=item.save_metadata,
            automatic_context=context,
            has_previous_artifacts=_has_previous_artifacts(state, plan),
            next_steps=_next_steps(state, plan),
            artifact=record.artifact,
            continuation_command=completion(item.artifact),
            loop_break_prompt=item.loop_break,
            loop_break_command=loop_break_command,
            loop_continue_prompt=item.loop_continue,
            loop_continue_command=(
                completion(item.artifact, "continue")
                if item.loop_continue is not None
                else None
            ),
            loop_name=loop_round[0] if loop_round else None,
            loop_iteration=loop_round[1] if loop_round else None,
            loop_max_times=loop_round[2] if loop_round else None,
            task_requirements=(
                self._requirements(state, plan) if item.step != INIT_STEP_NAME else None
            ),
            previous_step=previous.step if previous else None,
            previous_step_artifact=(
                str((self.root / previous.artifact).resolve()) if previous else None
            ),
            previous_step_summary=previous.summary if previous else None,
            summary_required=item.hands_over,
            documents=self._document_tasks(plan, item, state),
            interactive=item.interactive,
            interaction_entries=record.interaction_entries,
            interaction_ended=record.interaction_ended,
            interact_commands=(
                interact_commands(state.task_id, role) if item.interactive else None
            ),
            choices=item.choices,
            choice_mechanism=(
                choice_mechanism(state.agent).instruction if item.choices else None
            ),
            chosen=record.chosen,
            operator_paused=state.operator_paused,
            conversation=(self._conversation(state, item) if item.interactive else ()),
            ui=item.ui,
            shared_items=item.shared_items,
            stored_items=(
                self.tasks.read_items(state.task_id, state.run_id)
                if item.shared_items
                else ()
            ),
            required_item_fields=item.update_item,
            collects_items=item.item_operation == "collect",
            item_identity=item.item_identity,
            item_unique=item.item_unique,
            run_handovers=(
                tuple(
                    StepHandover(
                        found.step,
                        str((self.root / found.artifact).resolve()),
                        found.summary,
                    )
                    for found in self._handovers(state, plan)
                )
                if item.summary
                else ()
            ),
            profile_instruction=profile_instruction(item, self.root),
            subagents=item.subagents,
            working_directory=(
                str(resolve_workspace(self.root, state.working_directory))
                if state.working_directory
                else None
            ),
            assignment_scope=(
                span.to_dict() if span and item.id == span_ids[0] else None
            ),
            continues_assignment=item.id in span_ids[1:],
        )


def _base(
    state: ExecutionState,
    item: PlanItem | None,
    *,
    status: InstructionStatus | None = None,
    item_status: ItemStatus | None = None,
) -> Instruction:
    """The identity fields every instruction carries for the current item."""
    return Instruction(
        task_id=state.task_id,
        workflow=state.workflow,
        status=status or state.status,
        item_id=item.id if item else None,
        item_name=item.name if item else None,
        stage=_stage(item),
        step=item.step if item else None,
        parent=item.parent if item else None,
        item_status=item_status,
        action_kind=item.kind if item else None,
        action_text=None,
    )


def _loop_control(
    state: ExecutionState, item: PlanItem, loop: LoopBoundary
) -> Instruction:
    iteration = dict(state.loop_iterations).get(loop.loop_id, 0)
    limit_reached = loop_limit_reached(state, item)
    continuation: str | None = next_command(state.task_id)
    recovery: tuple[RecoveryCommand, ...] = ()
    if loop.boundary == "enter":
        text = (
            "Dispatch the first loop step. The loop remains active until "
            "a child step explicitly breaks it."
        )
    elif limit_reached:
        text = (
            f"Warning: loop `{loop.loop_id}` reached its maximum of "
            f"{loop.max_times} iterations. ww will not start another "
            "iteration. Escalate this result to the user for manual resolution."
        )
        continuation = None
        recovery = (force_command(state.task_id),)
    else:
        text = (
            f"Iteration {iteration} is complete. Dispatch the first step "
            "of the next iteration. The loop remains active until a child "
            "step explicitly breaks it."
        )
    return replace(
        _base(state, item, item_status="pending"),
        stage="Loop boundary",
        action_text=text,
        continuation_command=continuation,
        loop_iteration=iteration,
        loop_max_times=loop.max_times,
        loop_limit_reached=limit_reached,
        recovery_commands=recovery,
        is_loop_control=True,
    )


def _automatic_running(
    state: ExecutionState, item: PlanItem, record: PlanItemExecution
) -> Instruction:
    """An automatic item still marked running: only the locked next can settle it."""
    return replace(
        _base(state, item, item_status="in_progress"),
        error=(
            "automatic handler may still be running; use next "
            "under the task lock to inspect and resolve it"
        ),
        continuation_command=next_command(state.task_id),
        operation_id=record.operation_id,
        recovery_commands=recovery_commands(state.task_id),
    )


@dataclass(frozen=True)
class _StepResult:
    step: str
    artifact: str
    summary: str | None
    completed_at: str = ""


def _result_body(artifact: str) -> str:
    """The agent's result inside ww's step artifact envelope."""
    _, marker, body = artifact.partition("\n## Result\n\n")
    return (body if marker else artifact).strip()


def _bootstrap_profile_instruction(
    request: dict[str, object], root: Path
) -> str | None:
    path = request.get("profile_path")
    if path:
        return f"Use the profile defined in `{(root / str(path)).resolve()}`."
    text = request.get("profile_instruction")
    return str(text) if text else None


def profile_instruction(item: PlanItem, root: Path) -> str | None:
    """The profile text for an item: a file under ``root`` or configured text."""
    if item.profile_path is not None:
        return f"Use the profile defined in `{(root / item.profile_path).resolve()}`."
    return item.profile_instruction


def _current_assignment(
    state: ExecutionState, plan: WorkflowPlan, item: PlanItem | None
) -> Assignment | None:
    """The auto-runtime assignment holding the cursor: active, or about to begin."""
    if state.workflow_runtime != "auto" or item is None or is_coordinator(item):
        return None
    if state.status in {"completed", "failed", "interrupted"}:
        return None
    active = active_assignment(
        plan, state.assignment_item_id, runtime=state.workflow_runtime
    )
    if active is not None and active.start <= state.cursor < active.stop:
        return active
    return assignment_at(plan, state.cursor, runtime=state.workflow_runtime)


def _span(
    plan: WorkflowPlan, assignment: Assignment | None
) -> ItemSpan | LoopSpan | None:
    """The several stages or loop steps one worker performs in this assignment."""
    if assignment is None:
        return None
    return item_span(plan, assignment) or loop_span(plan, assignment)


def _loop_round(
    state: ExecutionState, plan: WorkflowPlan, item: PlanItem
) -> tuple[str, int, int] | None:
    """The enclosing loop's name, current round, and limit for a body step."""
    if item.loop_id is None:
        return None
    entry = next(
        (
            candidate
            for candidate in plan.items
            if (loop := loop_control(candidate)) is not None
            and loop.loop_id == item.loop_id
            and loop.boundary == "enter"
        ),
        None,
    )
    boundary = loop_control(entry) if entry is not None else None
    if entry is None or boundary is None:  # pragma: no cover - compiler invariant
        raise ValueError(f"loop {item.loop_id!r} has no entry boundary")
    iteration = dict(state.loop_iterations).get(item.loop_id, 1)
    return entry.name, iteration, boundary.max_times


def _assignment_preview(
    state: ExecutionState,
    plan: WorkflowPlan,
    item: PlanItem | None,
    next_role: NextRole,
) -> dict[str, object] | None:
    """What a delegating manager should know about the upcoming assignment."""
    if state.workflow_runtime != "auto" or next_role != "manager":
        return None
    if state.status == "awaiting_input":
        # The manager is already inside this assignment, supplying its values.
        return None
    assignment = assignment_at(plan, state.cursor, runtime=state.workflow_runtime)
    if assignment is not None and input_only(plan, assignment):
        return {
            "first_item_id": assignment.first_item_id,
            "start": assignment.start,
            "stop": assignment.stop,
            "message": (
                "The next assignment only supplies values to an automatic "
                "handler. The manager provides them itself; no worker is "
                "selected."
            ),
        }
    if assignment is not None:
        driver = selection_item(plan, assignment)
        if driver is None:
            return None
        if not driver.subagents:
            return {
                "first_item_id": assignment.first_item_id,
                "start": assignment.start,
                "stop": assignment.stop,
                "message": (
                    f"`{driver.name}` sets `subagents: false`, so no worker is "
                    "selected: after the manager command, perform it yourself "
                    "in this session."
                ),
            }
        span = _span(plan, assignment)
        return {
            "first_item_id": assignment.first_item_id,
            "start": assignment.start,
            "stop": assignment.stop,
            "selection_item_id": driver.id,
            "selection_item_name": driver.name,
            "requested_agent": driver.requested_agent,
            "requested_model": driver.requested_model,
            "requested_reasoning": driver.requested_reasoning,
            "requested_profile": driver.profile,
            **(
                {"item_scope": span.to_dict()}
                if isinstance(span, ItemSpan)
                else {"loop_scope": span.to_dict()}
                if span is not None
                else {}
            ),
        }
    if item is not None and is_coordinator(item):
        return {
            "coordinator_item_id": item.id,
            "coordinator_item_name": item.name,
            "message": (
                "Coordinator work must run before the next worker "
                "assignment can be determined."
            ),
        }
    return None


def _guidance(
    state: ExecutionState,
    item: PlanItem | None,
    selection: _Selection,
    next_role: NextRole,
) -> tuple[str, ...]:
    """Runtime guidance plus notes about worker selection."""
    guidance = runtime_instruction(state.workflow_runtime, next_role)
    if state.workflow_runtime != "auto" or item is None:
        return guidance
    requested = (item.requested_agent, item.requested_model, item.requested_reasoning)
    selected = (selection.agent, selection.model, selection.reasoning)
    differences = tuple(
        (label, request, actual)
        for label, request, actual in zip(
            ("agent", "model", "reasoning"), requested, selected, strict=True
        )
        if request not in {None, "auto"} and actual is not None and request != actual
    )
    if differences:
        detail = ", ".join(
            f"{label} requested {request!r}, selected {actual!r}"
            for label, request, actual in differences
        )
        guidance = (
            *guidance,
            "Execution selection differs from the request: " + detail + ".",
        )
        if item.phase != "step":
            guidance = (
                *guidance,
                "This hook requests different execution settings from "
                "the current assignment. You may perform it yourself "
                "or delegate only this hook to a suitable worker. "
                "You remain responsible for reporting the result to ww.",
            )
    if item.interactive:
        guidance = (
            *guidance,
            "This step is interactive: a conversation with the operator that "
            "only this session can hold, because a delegated worker cannot "
            "talk to them. Perform it yourself and do not delegate it. Its "
            "profile, agent, model, and reasoning settings are ignored.",
        )
    elif not item.subagents:
        guidance = (
            *guidance,
            "This step sets `subagents: false`: perform it yourself rather "
            "than handing it to a worker. Its profile, agent, model, and "
            "reasoning settings are ignored.",
        )
    return guidance


def build_bootstrap_instruction(request: dict[str, object], root: Path) -> Instruction:
    """Build the presentation view for a short-lived identity request."""
    request_id = str(request["request_id"])
    status = str(request["status"])
    in_progress = status == "in_progress"
    display_status = "in_progress" if status == "binding" else status
    if status == "completed":
        return Instruction(
            task_id=request_id,
            workflow=str(request["workflow"]),
            status="completed",
            item_id=None,
            item_name=None,
            stage=None,
            step=None,
            parent=None,
            item_status=None,
            action_kind=None,
            action_text=(
                "External task identity has been bound to "
                f"`{request['resolved_task_id']}`."
            ),
            next_role="manager",
            control="handoff_manager",
        )
    # A failed bootstrap is agent work that failed: the operator decides.
    reason: OperatorReason | None = "work_failed" if status == "failed" else None
    next_role: NextRole = (
        "worker" if in_progress else "operator" if reason else "manager"
    )
    control: Control = (
        "continue_worker"
        if in_progress
        else "awaiting_operator"
        if reason
        else "handoff_manager"
    )
    return Instruction(
        task_id=request_id,
        workflow=str(request["workflow"]),
        status=_instruction_status(display_status),
        item_id=str(request["item_id"]),
        item_name=str(request["item_name"]),
        stage="Task identity bootstrap",
        step=str(request["step"]),
        parent=None,
        item_status=_item_status(
            "in_progress" if status in {"binding", "in_progress"} else status
        ),
        action_kind=_plan_item_kind(str(request["action_kind"])),
        action_text=str(request["action_text"]) if in_progress else None,
        required_values=(ProvidedVariable("task_id", "The external task ID."),)
        if in_progress
        else (),
        continuation_command=(
            complete_command(request_id, (ProvidedVariable("task_id"),))
            if in_progress
            else next_command(request_id)
        ),
        error=str(request["error"]) if "error" in request else None,
        profile_instruction=(
            _bootstrap_profile_instruction(request, root) if in_progress else None
        ),
        workflow_runtime=str(request.get("workflow_runtime", "single")),
        workflow_runtime_instruction=runtime_instruction(
            str(request.get("workflow_runtime", "single")), next_role
        ),
        model=str(request.get("model", "auto")),
        reasoning=str(request.get("reasoning", "auto")),
        requested_agent=str(request.get("requested_agent", request.get("agent", "")))
        or None,
        requested_model=str(request.get("requested_model", "auto")),
        requested_reasoning=str(request.get("requested_reasoning", "auto")),
        requested_profile=(
            str(request["requested_profile"])
            if request.get("requested_profile")
            else None
        ),
        selected_agent=(
            str(request["selected_agent"]) if request.get("selected_agent") else None
        ),
        selected_model=(
            str(request["selected_model"]) if request.get("selected_model") else None
        ),
        selected_reasoning=(
            str(request["selected_reasoning"])
            if request.get("selected_reasoning")
            else None
        ),
        next_role=next_role,
        control=control,
        operator_reason=reason,
    )
