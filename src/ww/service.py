# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan-driven workflow lifecycle service."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ww.action_execution import (
    ActionExecutor,
)
from ww.actions import (
    PlannedAction,
    actions,
)
from ww.amendments import MAX_AMENDMENT_LENGTH, Amendment, TaskRequirements
from ww.assessments import pending_assessment
from ww.assignments import (
    active_assignment,
    assignment_at,
    completion_window,
    completion_window_items,
)
from ww.bootstrap import BootstrapCoordinator
from ww.builtin_workflows import missing_lane, require_lane
from ww.changes import take_mark
from ww.child_coordination import ChildCoordinator
from ww.children import ChildTask, skip_pending
from ww.completion_artifacts import rule_outcomes, write_completion_artifacts
from ww.completion_inputs import (
    group_metadata_values,
    validate_requested_values,
    validate_values,
)
from ww.config import YamlConfigurationLoader
from ww.contracts import (
    CALLER_ROLES,
    CLOSED_RUN_STATUSES,
    CallerRole,
    run_is_open,
)
from ww.control import child_workflow, loop_control, workflow_transition
from ww.defaults import (
    AGENT_INSTRUCTIONS,
    DEFAULT_PROJECT_CONFIG_JSON,
    DEFAULT_WORKFLOWS_YAML,
    PROJECT_LAUNCHER,
    SKILLS,
    skill_location,
)
from ww.direct_work import (
    DirectWork,
    RunWindow,
    reconciled_summary,
    unseen_commits,
)
from ww.documents import DocumentStore
from ww.errors import ConfigurationError, StateError
from ww.execution_models import (
    PLAN_COMPILER_VERSION,
    PLAN_SCHEMA_VERSION,
    CheckReport,
    Dispute,
    ExecutionState,
    HeldCompletion,
    PlanItemExecution,
    PlanSnapshot,
    RuleResolution,
    TaskRunAggregate,
    initial_state,
    operation_scope_for,
)
from ww.extensions import ExtensionRegistry, is_extension_reference, parse_reference
from ww.feedback import FeedbackStore
from ww.handler_repairs import close_assignment, needs_repair, request_repair
from ww.hooks.records import HookRecords, Interruption
from ww.instructions import Instruction, InstructionBuilder
from ww.instructions.commands import SUMMARY_FLAG, instruction_command
from ww.instructions.handoff import handoff_block
from ww.instructions.models import CheckPreview, HandoffBlock
from ww.interactions import InteractionLog, parse_transcript
from ww.interpolation import dependencies, interpolate
from ww.item_passes import (
    item_collection,
    leaving_pass,
    pass_gate_failures,
    reports_item_on_completion,
)
from ww.items import EDITABLE_WORK_ITEM_FIELDS, WorkItem, validate_item_fields
from ww.metadata_publication import MetadataPublisher, validate_metadata_values
from ww.operations import ChildLaunch
from ww.plan import (
    PlanCompilationOptions,
    PlanItem,
    PlannedCheck,
    WorkflowPlan,
    compile_workflow_plan,
)
from ww.project_config import load_project_config
from ww.recovery import RecoveryCoordinator
from ww.replanning import PlanChange, plan_change
from ww.replanning import keep_plan as keep_plan_
from ww.replanning import replan as replan_
from ww.results import (
    CleanupResult,
    InitializationResult,
    ItemUpdateResult,
    ResetResult,
    TaskStatus,
)
from ww.rule_checks import CheckScope, RuleChecker, change_set, item_reports
from ww.rule_conversion import SCRIPTIZE_WORKFLOW, scriptize_notice
from ww.rule_disputes import DisputeEntry, DisputeLog
from ww.rule_store import RuleStore
from ww.rule_verification import (
    close_round,
    hold_completion,
    index_of,
    judged_report,
    judged_rules,
    open_verification_round,
    parse_rule_results,
    record_round,
    resolve_rules,
    resume_held,
    round_open,
    skip_idle_verification,
    to_verify,
    verdicts_of,
    verification_needs,
)
from ww.rule_views import RuleView, check_preview, rule_view
from ww.run_coordination import RunCoordinator
from ww.runtimes import requested_setting, runtime_instruction
from ww.storage import Storage
from ww.storage_adapters import (
    CommandOutputAddress,
    ProjectMetadataStorage,
    TaskStorageAdapter,
)
from ww.task_ids import (
    candidate_task_ids,
    generated_bootstrap_id,
    is_bootstrap_request,
    task_id_claimed,
    validate_child_id,
    validate_task_id,
)
from ww.transitions import (
    advance_completed_item,
    await_item_input,
    begin_agent_item,
    begin_child_workflow,
    block_item_phase,
    complete_agent_item,
    complete_run,
    dispute_check,
    enclosing_loop_entry_index,
    enter_loop,
    exit_exhausted_loop,
    fail_agent_item,
    fail_child_workflow,
    finish_loop_continue,
    finish_loop_exit,
    finish_selection,
    fix_limits,
    loop_limit_reached,
    materialize_child_plan,
    materialize_item_plan,
    pause_for_agent,
    project_steps,
    reject_completion,
    repeat_loop,
    request_loop_continue,
    request_loop_exit,
    retry_failed_item,
    select_assessment_outcome,
    settle_stale_automatic_item,
    skip_failed_item,
    stop_for_values,
    supply_requested_input,
    waive_checks,
)
from ww.variables import (
    BRANCH_NAMING_STRATEGY,
    CHILD_FIELD_PREFIX,
    CHILD_VALUE_PREFIX,
    CHOICES,
    DOCUMENTS_PREFIX,
    METADATA_PREFIX,
    PROJECT,
    PROJECT_METADATA_PREFIX,
    child_value_name,
    child_values,
    item_binding_values,
    runtime_variable_values,
    unavailable_ww_values,
)
from ww.workflow_config import (
    INIT_STEP_NAME,
    ConfigurationLoader,
    DocumentDefinition,
    WorkflowConfiguration,
    WorkflowDefinition,
)
from ww.workflow_validation import validate_configuration
from ww.workspace import relative_workspace, resolve_workspace


@dataclass(frozen=True)
class CompletionSelection:
    """Normalized execution selection recorded for a completed agent item."""

    selected_agent: str | None
    selected_model: str | None
    selected_reasoning: str | None
    clear_selected_model: bool
    clear_selected_reasoning: bool


def _normalize_completion_selection(
    selected_agent: str | None,
    selected_model: str | None,
    selected_reasoning: str | None,
    previous_selected_agent: str | None,
    previous_selected_model: str | None,
) -> CompletionSelection:
    """Apply completion-time selection reset and inheritance policy."""
    agent_changed = (
        selected_agent is not None and selected_agent != previous_selected_agent
    )
    model_changed = selected_model not in {None, "auto", previous_selected_model}
    return CompletionSelection(
        selected_agent=selected_agent,
        selected_model=None if selected_model == "auto" else selected_model,
        selected_reasoning=(
            None if selected_reasoning == "auto" else selected_reasoning
        ),
        clear_selected_model=selected_model == "auto"
        or (agent_changed and selected_model is None),
        clear_selected_reasoning=selected_reasoning == "auto"
        or (agent_changed and selected_reasoning is None)
        or (model_changed and selected_reasoning is None),
    )


# The longest ``--summary``: one or two short sentences, since
# the detail belongs in the artifact and the summary reaches the manager.
SUMMARY_LIMIT = 500
ADJUSTMENTS_LIMIT = 500
ADJUSTMENTS_FLAG = "--adjustments"


def _latest_adjustments(
    state: ExecutionState, plan: WorkflowPlan
) -> tuple[str, str] | None:
    """The step and text of the adjustments most recently recorded in the run.

    A completion held for verification carries them until it is recorded.
    """
    steps = {item.id: item.step for item in plan.items}
    found = [
        (record.completed_at or "~", steps.get(record.plan_item_id), text)
        for record in (*state.execution_history, *state.item_executions)
        if (
            text := record.adjustments
            or (record.held_completion.adjustments if record.held_completion else None)
        )
    ]
    if not found:
        return None
    _, step, text = max(found, key=lambda entry: entry[0])
    return (step or "", text)


@dataclass(frozen=True)
class OpenAssignment:
    """A worker's open assignment as a worker command found it.

    ``items`` are the ids of its agent items, taken before the command runs,
    so the handoff can still name them after the assignment has ended.
    """

    token: str
    items: tuple[str, ...]
    active: str | None = None


FORCE_NOT_APPLICABLE = (
    "cannot force a task that is not failed or interrupted and is not stopped "
    "at a loop limit"
)
# Forcing would skip the step after the pass, not the pass's missing records.
FORCE_PAST_PASS_GATE = (
    "an items pass gate cannot be forced: record what each item lacks with "
    "update-item, then run next --retry"
)


class WorkflowService:
    """Advance exactly the immutable plan snapshot persisted for each task."""

    def __init__(
        self,
        storage: Storage,
        task_persistence: TaskStorageAdapter | None = None,
        extensions: ExtensionRegistry | None = None,
        configuration_loader: ConfigurationLoader | None = None,
        project_metadata: ProjectMetadataStorage | None = None,
        feedback_store: FeedbackStore | None = None,
    ) -> None:
        self.storage = storage
        self.tasks = task_persistence or storage.task_persistence
        self.project_metadata_store = project_metadata or storage.project_metadata
        self.extensions = extensions or ExtensionRegistry.discover(storage.root)
        self.configuration_loader = configuration_loader or YamlConfigurationLoader(
            storage.config_path, self.extensions
        )
        self.documents = DocumentStore(self.storage)
        self.interactions = InteractionLog(self.storage)
        self.feedback = feedback_store or FeedbackStore(self.storage)
        self.hook_records = HookRecords(self.storage, self.tasks)
        self.rule_store = RuleStore(self.storage.root)
        self.rule_disputes = DisputeLog(self.storage.root)
        self.instructions = InstructionBuilder(
            self.tasks,
            self._runtime_values,
            child_values=self._child_values,
            item_values=self._item_values,
            worker_requirements=lambda: (
                load_project_config(
                    self.storage.project_config_path
                ).pages.worker_requirements
            ),
            root=self.storage.root,
            documents=self.documents,
            interactions=self.interactions,
        )
        self.runs = RunCoordinator(self.tasks)
        self.metadata_publisher = MetadataPublisher(
            self.tasks, self.project_metadata_store, self
        )
        self.bootstrap = BootstrapCoordinator(
            self.storage,
            self.tasks,
            self.extensions,
            now=_now,
            generated_request_id=generated_bootstrap_id,
            validate_task_id=validate_task_id,
            task_exists=self._task_exists,
        )
        self.actions = ActionExecutor(
            root=self.storage.root,
            extensions=self.extensions,
            commit=self.commit,
            project_state=self._with_steps,
            now=_now,
            write_command_output=self.tasks.write_command_output,
            read_command_output=self.tasks.read_command_output,
            task_values=self._runtime_values,
            metadata_publisher=self.metadata_publisher,
            child_values=self._child_values,
            item_values=self._item_values,
            read_items=lambda state: self.tasks.read_items(state.task_id, state.run_id),
            commit_items=self._commit_items,
        )
        self.recovery = RecoveryCoordinator(self.tasks, self.actions, self, _now)
        self.rule_checker = RuleChecker(self.tasks.write_command_output, _now)
        self.children = ChildCoordinator(
            self.tasks,
            self,
            self._start,
            _now,
            self._validate_child_workflow,
            self._start_child_identity,
        )

    def start(
        self,
        workflow_name: str,
        task_id: str | None,
        mode_names: tuple[str, ...] = (),
        agent: str = "codex",
        model: str = "auto",
        reasoning: str = "auto",
        workflow_runtime: str | None = None,
        init_artifact: str | None = "Recorded requirements.",
        *,
        caller_role: CallerRole | None = None,
        branch_naming_strategy: str | None = None,
        project: str | None = None,
        fresh_items: bool = False,
    ) -> Instruction:
        self._require_manager("start", caller_role)
        # ``"on_request"`` allows it: an agent starts a task only when asked.
        if self.extensions.config.disabled:
            raise StateError(
                "ww is disabled for this project (ww.json has "
                '"enabled": false); do not use ww for this work'
            )
        if not agent:
            raise StateError("start requires --agent")
        if init_artifact is None or not init_artifact.strip():
            raise StateError("start requires a non-empty --requirements")
        self._validate_execution_metadata(model, reasoning)
        if workflow_runtime is None:
            # The flag outranks the workflow's own runtime, which outranks the
            # project default.
            declared = self._load_configuration().workflows_by_name.get(workflow_name)
            workflow_runtime = (
                declared.runtime if declared is not None and declared.runtime else None
            ) or self.extensions.config.runtime
        runtime_instruction(workflow_runtime)
        if branch_naming_strategy is not None and not branch_naming_strategy.strip():
            raise StateError("start --branch-strategy must be non-empty")
        if project is not None:
            self._project_directory(project)
        if task_id is None:
            bootstrap = self.bootstrap.step(
                self._load_configuration(),
                workflow_name,
                mode_names,
                agent,
                self._unknown_modes,
                project=project,
            )
            if bootstrap is not None:
                instruction = replace(
                    self._tag_caller(
                        self.bootstrap.start(
                            workflow_name,
                            mode_names,
                            agent,
                            bootstrap,
                            model,
                            reasoning,
                            workflow_runtime,
                            branch_naming_strategy,
                            init_artifact,
                            project,
                        ),
                        caller_role,
                    ),
                    manager_intro=True,
                )
                return self._with_rules_notice(instruction, workflow_name)
            instruction = replace(
                self._tag_caller(
                    self._start_generated(
                        workflow_name,
                        mode_names,
                        agent,
                        model,
                        reasoning,
                        workflow_runtime,
                        branch_naming_strategy,
                        init_artifact,
                        project,
                    ),
                    caller_role,
                ),
                manager_intro=True,
            )
            return self._with_rules_notice(instruction, workflow_name)
        validate_task_id(task_id)
        with self.tasks.lock_task(task_id):
            instruction = replace(
                self._tag_caller(
                    self._start(
                        task_id,
                        workflow_name,
                        mode_names,
                        agent,
                        model=model,
                        reasoning=reasoning,
                        workflow_runtime=workflow_runtime,
                        init_artifact=init_artifact,
                        branch_naming_strategy=branch_naming_strategy,
                        project=project,
                        fresh_items=fresh_items,
                    ),
                    caller_role,
                ),
                manager_intro=True,
            )
        return self._with_rules_notice(instruction, workflow_name)

    def _with_rules_notice(
        self, instruction: Instruction, workflow_name: str
    ) -> Instruction:
        """The first page of a start, with the notice of rules without a check.

        ``ww-scriptize-rules`` itself is the answer to the notice, so its
        pages go without it. The task has started by now: a store that cannot
        be read drops the notice rather than failing the start.
        """
        if workflow_name == SCRIPTIZE_WORKFLOW:
            return instruction
        try:
            automation = self.rule_store.load()
        except StateError:
            return instruction
        return replace(
            instruction,
            rules_notice=scriptize_notice(self._load_configuration(), automation),
        )

    def _project_path(self, project: str | None) -> str | None:
        """The configured project's directory, without checking it exists."""
        if project is None:
            return None
        definition = self.extensions.config.projects_by_name.get(project)
        if definition is None:
            return None
        return str(definition.directory(self.storage.root))

    def _project_directory(self, project: str | None) -> str | None:
        """Resolve a configured project to the directory its tasks work in.

        The result is persisted, so it is relative to the project root; see
        :mod:`ww.workspace`.
        """
        if project is None:
            return None
        path = self._project_path(project)
        if path is None:
            raise StateError(self.extensions.config.unknown_project(project))
        directory = Path(path)
        if not directory.is_dir():
            raise StateError(
                f"project {project!r} directory does not exist: {directory}"
            )
        return relative_workspace(self.storage.root, directory)

    def _start_generated(
        self,
        workflow_name: str,
        mode_names: tuple[str, ...],
        agent: str,
        model: str,
        reasoning: str,
        workflow_runtime: str,
        branch_naming_strategy: str | None,
        init_artifact: str,
        project: str | None = None,
    ) -> Instruction:
        """Start under the first generated ID that is free.

        Each candidate is locked before it is checked, and released again if
        it turns out to be taken.
        """
        task_format = self.extensions.task_format(project)
        for candidate in candidate_task_ids(task_format):
            validate_task_id(candidate)
            with self.tasks.lock_task(candidate):
                if not self._task_exists(candidate, workflow_name, project):
                    return self._start(
                        candidate,
                        workflow_name,
                        mode_names,
                        agent,
                        model=model,
                        reasoning=reasoning,
                        workflow_runtime=workflow_runtime,
                        init_artifact=init_artifact,
                        branch_naming_strategy=branch_naming_strategy,
                        project=project,
                    )
        raise StateError(
            f"cannot generate an unused task ID from task format {task_format!r}"
        )

    def _start(
        self,
        task_id: str,
        workflow_name: str,
        mode_names: tuple[str, ...],
        agent: str,
        bootstrap_step: str | None = None,
        bootstrap_values: tuple[tuple[str, str], ...] = (),
        bootstrap_request_id: str | None = None,
        parent_task_id: str | None = None,
        start_operation_id: str | None = None,
        model: str = "auto",
        reasoning: str = "auto",
        workflow_runtime: str = "single",
        branch_naming_strategy: str | None = None,
        init_artifact: str = "",
        project: str | None = None,
        fresh_items: bool = False,
    ) -> Instruction:
        configuration = self._load_configuration()
        working_directory = self._project_directory(project)
        if workflow_name not in configuration.workflows_by_name:
            raise ConfigurationError(f"workflow not found: {workflow_name}")
        workflow = configuration.workflows_by_name[workflow_name]
        require_lane(workflow)
        unknown_modes = self._unknown_modes(mode_names, configuration)
        if unknown_modes:
            raise StateError("unknown mode(s): " + ", ".join(sorted(unknown_modes)))
        plan = compile_workflow_plan(
            configuration,
            self.storage.root,
            workflow_name,
            agent,
            task_id,
            self.extensions,
            PlanCompilationOptions(
                task_id=task_id,
                completed_bootstrap_step=bootstrap_step,
                project=project,
                modes=mode_names or None,
            ),
            self.extensions.config,
        )
        snapshot = PlanSnapshot(
            schema_version=PLAN_SCHEMA_VERSION,
            compiler_version=PLAN_COMPILER_VERSION,
            configuration_digest=self._configuration_digest(configuration),
            compiled_at=_now(),
            plan=plan,
            bootstrap_step=bootstrap_step,
        )
        if not snapshot.plan.items:
            raise ConfigurationError(
                f"workflow {workflow_name!r} has no executable plan items"
            )
        self._abandon_for_restart(task_id, workflow)
        run_id = self.tasks.next_execution_run_id(task_id, workflow_name)
        workflow_values = dict(bootstrap_values or (("task_id", task_id),))
        if branch_naming_strategy is not None:
            workflow_values[BRANCH_NAMING_STRATEGY] = branch_naming_strategy
        if project is not None:
            workflow_values[PROJECT] = project
        state = replace(
            initial_state(
                snapshot,
                tuple(mode_names or workflow.modes),
                _now(),
                run_id=run_id,
                execution_instance_id=uuid.uuid4().hex,
                parent_task_id=parent_task_id,
                start_operation_id=start_operation_id,
                workflow_runtime=workflow_runtime,
                model=model,
                reasoning=reasoning,
            ),
            run_id=run_id,
            workflow_values=tuple(workflow_values.items()),
            working_directory=working_directory,
            pending_init_artifact=init_artifact,
        )
        if fresh_items:
            self.tasks.write_shared_items(task_id, ())
        self.commit(
            state,
            snapshot,
            items=self._seed_shared_items(task_id, plan),
            bootstrap_request_id=bootstrap_request_id,
        )
        state, snapshot = self._complete_initialization(state, snapshot)
        return self.render(state, snapshot)

    def _require_item_fields(
        self, task_id: str, state: ExecutionState, item: PlanItem
    ) -> None:
        """A step's declared item fields must be set before it completes."""
        items = self.tasks.read_items(task_id, state.run_id)
        if item.item_id is not None:
            items = tuple(entry for entry in items if entry.id == item.item_id)
        missing = [
            f"{field.name} on {entry.id}"
            for entry in items
            for field in item.update_item
            if not entry.field(field.name)
        ]
        if missing:
            raise StateError(
                f"{item.name!r} sets item fields that are still empty: "
                + ", ".join(missing)
                + "; set them with update-item --field NAME=VALUE, then complete"
            )

    def _abandon_for_restart(self, task_id: str, workflow: WorkflowDefinition) -> None:
        """Close an unfinished run of a restartable workflow before a new start.

        The run stays in the task's history with everything it recorded; it
        is only no longer the open run.  An unfinished run of another
        workflow is never abandoned this way.
        """
        runs, _handoff, _revision = self.tasks.read_task_record(task_id)
        active = next(
            (run for run in reversed(runs) if run_is_open(run.state.status)), None
        )
        if active is None:
            return
        if not workflow.restartable or active.workflow != workflow.name:
            raise StateError(
                f"task {task_id!r} already has an unfinished run {active.run_id!r} "
                f"of workflow {active.workflow!r}; continue it with "
                f"{instruction_command(task_id, role='manager')}. Only the "
                "operator decides to reset the task instead, or to declare the "
                "workflow restartable so a new start abandons its earlier run"
            )
        self.commit(
            replace(
                active.state,
                status="abandoned",
                last_error=f"abandoned by a new start of {workflow.name!r}",
                updated_at=_now(),
            ),
            active.snapshot,
        )

    def _seed_shared_items(
        self, task_id: str, plan: WorkflowPlan
    ) -> tuple[WorkItem, ...] | None:
        """A new run of a shared item flow starts from the task's items.

        Identity, text, and references carry over; the outcome fields start
        clear, because every run is a new round over the same items.
        """
        collection = item_collection(plan)
        if collection is None or not collection.shared_items:
            return None
        return tuple(
            WorkItem(
                item.id,
                item.item,
                reference_to_id=item.reference_to_id,
                fields=item.fields,
            )
            for item in self.tasks.read_shared_items(task_id)
        )

    def _share_items(
        self, task_id: str, plan: WorkflowPlan, items: tuple[WorkItem, ...]
    ) -> None:
        """Refresh the task's shared items from the run's copy, when shared."""
        collection = item_collection(plan)
        if collection is not None and collection.shared_items:
            self.tasks.write_shared_items(task_id, items)

    def commit(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        *,
        items: tuple[WorkItem, ...] | None = None,
        children: tuple[ChildTask, ...] | None = None,
        handoff: str | None = None,
        bootstrap_request_id: str | None = None,
    ) -> None:
        """Commit the complete run transition through the storage-adapter boundary."""
        self.runs.commit_run(
            state,
            snapshot,
            items=items,
            children=children,
            handoff=handoff,
            bootstrap_request_id=bootstrap_request_id,
        )
        if (
            state.status == "completed"
            and load_project_config(self.storage.project_config_path).feedback_learning
        ):
            self.feedback.complete_task(state.task_id)

    def _reported_items(
        self, task_id: str, state: ExecutionState, item: PlanItem, reports: bool
    ) -> tuple[WorkItem, ...] | None:
        """The items with ``item``'s own item marked reported, if it reports.

        The mark is committed with the completion that finishes the report
        stage, so an item is never reported before that completion is durable.
        """
        if not reports or item.item_id is None:
            return None
        return tuple(
            replace(entry, reported=True) if entry.id == item.item_id else entry
            for entry in self.tasks.read_items(task_id, state.run_id)
        )

    def _commit_items(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        items: tuple[WorkItem, ...],
    ) -> None:
        """Commit a transition together with the item records it changed."""
        self.commit(state, snapshot, items=items)
        self._share_items(state.task_id, snapshot.plan, items)

    def feedback_sources(
        self,
        task_id: str,
        run_id: str | None = None,
    ) -> dict[str, object]:
        """Expose completed learnable artifacts through the storage boundary."""
        state, snapshot = self.load(task_id, run_id)
        if state.status != "completed":
            raise StateError("feedback deduction requires a completed workflow run")
        sources = []
        items = {item.id: item for item in snapshot.plan.items}
        seen: set[str] = set()
        for record in (*state.execution_history, *state.item_executions):
            item = items.get(record.plan_item_id)
            if (
                item is None
                or not item.learnable
                or record.status != "completed"
                or not record.artifact
                or record.artifact in seen
            ):
                continue
            seen.add(record.artifact)
            source_id = (
                "artifact-" + hashlib.sha256(record.artifact.encode()).hexdigest()[:16]
            )
            sources.append(
                {
                    "id": source_id,
                    "step": item.step,
                    "artifact": record.artifact,
                    "encountered_at": record.completed_at or state.updated_at,
                    "content": self.tasks.read_execution_artifact(record.artifact),
                    # What the operator asked to change during the step: the
                    # worker's own report, quotable as evidence like the artifact.
                    **(
                        {"adjustments": record.adjustments}
                        if record.adjustments
                        else {}
                    ),
                }
            )
        return {"task": task_id, "run": state.run_id, "sources": sources}

    def record_feedback(
        self,
        task_id: str,
        analysis: object,
        *,
        run_id: str | None = None,
        caller_role: CallerRole | None = None,
        assignment: str | None = None,
    ) -> dict[str, object]:
        """Record deductions from a finished run, without modifying its plan/state."""
        self._validate_caller_role(caller_role)
        validate_task_id(task_id)
        if not load_project_config(self.storage.project_config_path).feedback_learning:
            raise StateError("feedback learning is disabled in ww.json")
        with self.tasks.lock_task(task_id):
            self._authorize_worker(task_id, caller_role, assignment)
            sources = self.feedback_sources(task_id, run_id)
            state, _ = self.load(task_id, run_id)
            return self.feedback.record(
                task_id, state.run_id, analysis, sources["sources"]
            )

    def _commit_runs(
        self,
        task_id: str,
        aggregates: tuple[TaskRunAggregate, ...],
        *,
        handoff: str | None = None,
    ) -> None:
        self.runs.commit_runs(task_id, aggregates, handoff=handoff)

    def next(
        self,
        task_id: str,
        model: str = "auto",
        reasoning: str = "auto",
        force: bool = False,
        retry: bool = False,
        force_reason: str | None = None,
        outcome: str | None = None,
        selected_agent: str | None = None,
        *,
        caller_role: CallerRole | None = None,
        reassign: bool = False,
        replan: bool = False,
        keep_plan: bool = False,
    ) -> Instruction:
        """Advance the task.

        ``reassign`` gives the open assignment a new token, so its previous
        worker can no longer act, and returns the page to dispatch it again.
        When the workflow's definition changed since the run's plan was saved,
        ``next`` stops at a ``plan_changed`` page first; ``replan`` takes the
        new definition from the first changed item on, ``keep_plan`` carries
        on with the saved plan, and either then advances as usual.
        """
        self._require_manager("next", caller_role)
        if (replan or keep_plan) and (
            replan == keep_plan or force or retry or outcome or reassign
        ):
            raise StateError(
                "choose one of next --replan or --keep-plan, without other decisions"
            )
        if reassign:
            if force or retry or outcome:
                raise StateError("next --reassign takes no other decision")
            return self._tag_caller(self._reassign(task_id), caller_role)
        if force and (force_reason is None or not force_reason.strip()):
            raise StateError("next --force requires --reason")
        if force_reason is not None and not force:
            raise StateError("--reason requires next --force")
        if retry and force:
            raise StateError("choose only one of next --retry or next --force")
        self._validate_execution_metadata(model, reasoning)
        self._validate_selected_agent(selected_agent)
        refreshed = self.children.refresh_parent(task_id)
        if refreshed is not None:
            return self._tag_caller(refreshed, caller_role)
        if not is_bootstrap_request(task_id):
            stop = self._plan_gate(task_id, replan=replan, keep_plan=keep_plan)
            if stop is not None:
                return self._tag_caller(stop, caller_role)
        instruction = self._next_command(
            task_id,
            model,
            reasoning,
            force,
            retry,
            force_reason,
            outcome,
            selected_agent=selected_agent,
            caller_role=caller_role,
        )
        self.children.reconcile_after_child(task_id)
        instruction = self._start_declared_child(task_id, instruction)
        return self._tag_caller(instruction, caller_role)

    def _start_declared_child(
        self, task_id: str, instruction: Instruction
    ) -> Instruction:
        """Start the child a ``start_child`` stage declares, once it is reached.

        The launch goes through ``start-child`` itself, so validation and
        recording are the same as for a manual start.  It runs after the
        parent's lock is released because starting a child locks the parent.
        A launch that cannot proceed fails the stage like any automatic
        handler: the operator reads the cause and retries or replans.
        """
        if is_bootstrap_request(task_id) or "/" in task_id:
            return instruction
        state, snapshot = self.load(task_id)
        if state.cursor >= len(snapshot.plan.items):
            return instruction
        item = snapshot.plan.items[state.cursor]
        coordinator = child_workflow(item)
        if (
            coordinator is None
            or coordinator.launch is None
            or item.child_number is None
            or state.status != "in_progress"
            or state.item_executions[state.cursor].status != "in_progress"
        ):
            return instruction
        children = self.tasks.read_children(state.task_id, state.run_id)
        child = children[item.child_number - 1]
        if child.status not in {"pending", "starting"}:
            return instruction
        # A start already begun keeps its frozen settings.
        settings = (
            {}
            if child.status == "starting"
            else self._launch_settings(
                coordinator.launch, self._child_values(state, snapshot.plan, item)
            )
        )
        try:
            started = self.children.start_child(state.task_id, child.id, **settings)
        except (StateError, ConfigurationError) as error:
            with self.tasks.lock_task(state.task_id):
                state, snapshot = self.load(state.task_id)
                failed = fail_child_workflow(
                    state,
                    snapshot.plan,
                    item,
                    child.id,
                    _now,
                    f"ww could not start child {child.id!r} for step "
                    f"{item.name!r}: {error}",
                )
                self.commit(failed, snapshot)
                return self.render(failed, snapshot)
        return replace(
            started,
            notices=(
                *started.notices,
                f"ww started child `{child.id}` for step `{item.name}` of "
                f"`{state.task_id}` with the launch settings recorded on the child.",
            ),
        )

    @staticmethod
    def _launch_settings(
        launch: ChildLaunch, values: Mapping[str, str]
    ) -> dict[str, str]:
        """The ``start-child`` options a launch renders to from the child's record.

        A setting that is absent, names a field the child lacks, or renders
        empty is left out, so it inherits.
        """
        settings: dict[str, str] = {}
        for name, template in (
            ("workflow_name", launch.workflow),
            ("workflow_runtime", launch.runtime),
            ("model", launch.model),
            ("reasoning", launch.reasoning),
            ("agent", launch.agent),
        ):
            if template is None:
                continue
            bound = {name: "" for name in dependencies(template)} | dict(values)
            rendered = interpolate(template, bound).strip()
            if rendered:
                settings[name] = rendered
        return settings

    def _next_command(
        self,
        task_id: str,
        model: str = "auto",
        reasoning: str = "auto",
        force: bool = False,
        retry: bool = False,
        force_reason: str | None = None,
        outcome: str | None = None,
        selected_agent: str | None = None,
        *,
        caller_role: CallerRole | None = None,
    ) -> Instruction:
        self._validate_execution_metadata(model, reasoning)
        request = (
            self.storage.read_bootstrap(task_id)
            if is_bootstrap_request(task_id)
            else None
        )
        if request is not None:
            return self.bootstrap.next(
                request,
                force,
                selected_agent=selected_agent,
                selected_model=None if model == "auto" else model,
                selected_reasoning=None if reasoning == "auto" else reasoning,
                start_task=self._start,
                status_task=self.instruction_status,
                bind_child=self.children.bind_child,
            )
        if retry:
            validate_task_id(task_id)
            return self.recovery.recover(task_id, retry=True)
        validate_task_id(task_id)
        with self.tasks.lock_task(task_id):
            return self._next(
                task_id,
                force,
                force_reason,
                outcome,
                model,
                reasoning,
                selected_agent=selected_agent,
                caller_role=caller_role,
            )

    def recover(
        self,
        task_id: str,
        *,
        retry: bool = False,
        mark_succeeded: bool = False,
        output: str | None = None,
        working_directory: str | None = None,
        variables: tuple[tuple[str, str], ...] = (),
        caller_role: CallerRole | None = None,
    ) -> Instruction:
        self._require_manager("recover", caller_role)
        instruction = self.recovery.recover(
            task_id,
            retry=retry,
            mark_succeeded=mark_succeeded,
            output=output,
            working_directory=working_directory,
            variables=variables,
        )
        self.children.reconcile_after_child(task_id)
        return self._tag_caller(instruction, caller_role)

    def _next(
        self,
        task_id: str,
        force: bool = False,
        force_reason: str | None = None,
        outcome: str | None = None,
        model: str = "auto",
        reasoning: str = "auto",
        selected_agent: str | None = None,
        *,
        caller_role: CallerRole | None = None,
    ) -> Instruction:
        state, snapshot = self.load(task_id)
        state = select_assessment_outcome(state, snapshot.plan, outcome, _now)
        if outcome is not None:
            self.commit(state, snapshot)
        state, snapshot = self.metadata_publisher.reconcile(state, snapshot)
        starting_instance = state.execution_instance_id
        if state.status == "completed":
            raise StateError(f"task {task_id!r} is already completed")
        if state.status == "abandoned":
            raise StateError(
                f"run {state.run_id!r} of task {task_id!r} was abandoned by a later "
                "start; work on the current run"
            )
        if state.status == "interrupted":
            if force:
                if state.cursor >= len(snapshot.plan.items):
                    raise StateError(
                        "interrupted task has no current item to force past"
                    )
                state = skip_failed_item(state, snapshot.plan, _now, force_reason)
                self.commit(state, snapshot)
            else:
                # Never replay an operation with an unknown external outcome as
                # an incidental consequence of asking for the next instruction,
                # unless its handler declared the replay harmless.  Otherwise
                # use ``next --retry`` or explicitly force past it.
                replayed = self.recovery.replay_if_idempotent(state, snapshot)
                if replayed is None:
                    return self.render(state, snapshot)
                return self.resume(replayed, snapshot)
        if state.status == "failed":
            if force and state.failure_kind == "pass_incomplete":
                raise StateError(FORCE_PAST_PASS_GATE)
            if (
                force
                and state.failure_kind in {"fix_limit", "check_disputed"}
                and not needs_repair(state)
            ):
                # Forcing past a step at its fix limit completes it without
                # its checks, and past a dispute without the disputed one;
                # never without its work.
                assert force_reason is not None
                state = waive_checks(
                    state,
                    snapshot.plan,
                    self._waivable(state, snapshot.plan),
                    force_reason,
                    _now,
                )
                self.commit(state, snapshot)
            elif force:
                if state.cursor >= len(snapshot.plan.items):
                    raise StateError("failed task has no current item to force past")
                state = skip_failed_item(state, snapshot.plan, _now, force_reason)
                self.commit(state, snapshot)
            else:
                state = retry_failed_item(state, snapshot.plan, _now)
                self.commit(state, snapshot)
        elif force:
            if not self._at_loop_limit(state, snapshot):
                raise StateError(FORCE_NOT_APPLICABLE)
            state = exit_exhausted_loop(state, snapshot.plan, _now, force_reason)
            self.commit(state, snapshot)
        if state.status == "awaiting_input":
            return self.render(state, snapshot)
        if needs_repair(state):
            return self._dispatch_repair(
                state, snapshot, model, reasoning, selected_agent
            )
        if state.active_item_id:
            if child_workflow(snapshot.plan.items[state.cursor]) is not None:
                return self.render(state, snapshot)
            item = snapshot.plan.items[state.cursor]
            record = state.item_executions[state.cursor]
            if (
                item.owner == "ww"
                and item.execution == "automatic"
                and record.status == "in_progress"
            ):
                state = self.mark_interrupted(state, snapshot)
                replayed = self.recovery.replay_if_idempotent(state, snapshot)
                if replayed is not None:
                    return self.resume(replayed, snapshot)
                return self.render(state, snapshot)
            if state.workflow_runtime == "single":
                # The same session holds every step: asking again for the one
                # already open is harmless, so show it instead of refusing.
                return self.render(state, snapshot)
            raise StateError("an agent item is already in progress; use complete")
        # One manager call carries through every coordinator boundary it
        # meets, so preparation hooks before a loop, or a loop nested in
        # another, never cost the manager a second `next` before the first
        # worker step can start.  Each pass enters or repeats at most one
        # boundary, so the plan length bounds the passes.
        for _ in range(len(snapshot.plan.items) + 1):
            if state.cursor < len(snapshot.plan.items):
                boundary = snapshot.plan.items[state.cursor]
                loop = loop_control(boundary)
                if loop is not None:
                    if loop_limit_reached(state, boundary):
                        # The escalation instruction names the operator's
                        # exit, ``next --force``; nothing else may start
                        # another round.
                        return self.render(state, snapshot)
                    state = (
                        enter_loop(state, snapshot.plan, boundary, _now)
                        if loop.boundary == "enter"
                        else repeat_loop(state, snapshot.plan, boundary, _now)
                    )
                    self.commit(state, snapshot)
            if caller_role == "manager" and state.assignment_item_id is None:
                state = self._begin_assignment(
                    state,
                    snapshot,
                    model=model,
                    reasoning=reasoning,
                    selected_agent=selected_agent,
                )
            state, snapshot = self.drain(
                state,
                snapshot,
                state.assignment_item_id if caller_role == "manager" else None,
            )
            if caller_role == "manager" and (
                state.execution_instance_id != starting_instance
                or state.assignment_item_id is not None
            ):
                state, snapshot = self._activate_or_handoff(state, snapshot)
                if self._stopped_at_loop_boundary(state, snapshot):
                    continue
                return self.render(state, snapshot)
            if state.status in {"completed", "awaiting_input", "failed"}:
                return self.render(state, snapshot)
            if (
                state.active_item_id
                and child_workflow(snapshot.plan.items[state.cursor]) is not None
            ):
                return self.render(state, snapshot)
            if loop_control(snapshot.plan.items[state.cursor]) is not None:
                if self._stopped_at_loop_boundary(state, snapshot):
                    continue
                return self.render(state, snapshot)
            break
        else:  # pragma: no cover - every pass consumes a boundary
            raise StateError("next did not reach an agent item")
        if needs_repair(state):
            return self._dispatch_repair(
                state, snapshot, model, reasoning, selected_agent
            )
        item = snapshot.plan.items[state.cursor]
        if item.owner != "agent":
            raise StateError("executor stopped on a non-agent item")
        state = begin_agent_item(
            state,
            snapshot.plan,
            item,
            model=model
            if model != "auto"
            else requested_setting(item.model) or state.model,
            reasoning=(
                reasoning
                if reasoning != "auto"
                else requested_setting(item.reasoning) or state.reasoning
            ),
            selected_model=model if model != "auto" else None,
            selected_reasoning=reasoning if reasoning != "auto" else None,
            change_mark=self._change_mark(state, snapshot.plan, item),
            resolution=self._resolution(state, snapshot.plan, item),
            now=_now,
        )
        self.commit(state, snapshot)
        return self.render(state, snapshot)

    @staticmethod
    def _waivable(state: ExecutionState, plan: WorkflowPlan) -> tuple[str, ...]:
        """What ``next --force`` waives: the disputed check, or all of them."""
        record = state.item_executions[state.cursor]
        if state.failure_kind == "check_disputed":
            assert record.dispute is not None
            return (record.dispute.check,)
        return tuple(fix_limits(plan.items[state.cursor], record))

    def force_target(self, task_id: str) -> str:
        """Describe what ``next --force`` would do, or raise when nothing can be forced.

        The CLI asks this before its interactive confirmation, so an operator is
        never asked to approve a force that ww would then refuse.
        """
        validate_task_id(task_id)
        state, snapshot = self.load(task_id)
        items = snapshot.plan.items
        if (
            state.status == "failed"
            and state.failure_kind == "fix_limit"
            and not needs_repair(state)
        ):
            return (
                f"waive the failed checks of `{items[state.cursor].name}`: its "
                "worker completes it again without them, and the artifact "
                "records the waiver"
            )
        if state.status == "failed" and state.failure_kind == "check_disputed":
            (disputed,) = self._waivable(state, snapshot.plan)
            return (
                f"waive the disputed check `{disputed}` of "
                f"`{items[state.cursor].name}`: its worker completes it again "
                "without that check, and the artifact records the waiver"
            )
        if state.status == "failed" and state.failure_kind == "pass_incomplete":
            raise StateError(FORCE_PAST_PASS_GATE)
        if state.status in {"failed", "interrupted"}:
            if state.cursor >= len(items):
                raise StateError(
                    f"{state.status} task has no current item to force past"
                )
            return (
                f"skip the {state.status} item `{items[state.cursor].name}` "
                "without running it"
            )
        if self._at_loop_limit(state, snapshot):
            loop = loop_control(items[state.cursor])
            assert loop is not None
            return (
                f"leave the `{loop.loop_id}` loop at its limit of {loop.max_times} "
                "iterations and continue with the steps after it"
            )
        raise StateError(FORCE_NOT_APPLICABLE)

    @staticmethod
    def _stopped_at_loop_boundary(
        state: ExecutionState, snapshot: PlanSnapshot
    ) -> bool:
        """Whether ``next`` idles on a loop boundary it may still pass now."""
        return (
            state.status == "pending"
            and state.active_item_id is None
            and state.assignment_item_id is None
            and state.cursor < len(snapshot.plan.items)
            and loop_control(snapshot.plan.items[state.cursor]) is not None
            and not loop_limit_reached(state, snapshot.plan.items[state.cursor])
        )

    @staticmethod
    def _at_loop_limit(state: ExecutionState, snapshot: PlanSnapshot) -> bool:
        return state.cursor < len(snapshot.plan.items) and loop_limit_reached(
            state, snapshot.plan.items[state.cursor]
        )

    def mark_interrupted(
        self, state: ExecutionState, snapshot: PlanSnapshot
    ) -> ExecutionState:
        """Record that a persisted automatic operation outlived its process.

        An ``in_progress`` record is only written immediately before invoking
        an external command or extension.  Seeing one during a later command
        therefore means the outcome is unknown, unless a command segment had
        already recorded its non-zero exit, which is a known failure.  Neither
        may be interpreted as agent-owned work or silently skipped.
        """
        item = snapshot.plan.items[state.cursor]
        state = settle_stale_automatic_item(state, snapshot.plan, item, _now)
        self.commit(state, snapshot)
        return state

    def fail(
        self,
        task_id: str,
        error: str,
        *,
        caller_role: CallerRole | None = None,
        assignment: str | None = None,
    ) -> Instruction:
        self._validate_caller_role(caller_role)
        request = (
            self.storage.read_bootstrap(task_id)
            if is_bootstrap_request(task_id)
            else None
        )
        if request is not None:
            if not error.strip():
                raise StateError("--error must be non-empty")
            request["status"] = "failed"
            request["error"] = error.strip()
            self.storage.write_bootstrap(task_id, request)
            return self._tag_caller(self.bootstrap.instruction(request), caller_role)
        validate_task_id(task_id)
        if not error.strip():
            raise StateError("--error must be non-empty")
        with self.tasks.lock_task(task_id):
            self._authorize_worker(task_id, caller_role, assignment)
            opened = self._open_assignment(task_id, caller_role)
            instruction = self._with_handoff(
                task_id, self._fail(task_id, error.strip()), opened
            )
        self.children.reconcile_after_child(task_id)
        return self._tag_caller(instruction, caller_role)

    def interact(
        self,
        task_id: str,
        *,
        operator: str | None = None,
        agent: str | None = None,
        transcript: str | None = None,
        choice: str | None = None,
        end: bool = False,
        pause: bool = False,
        caller_role: CallerRole | None = None,
        assignment: str | None = None,
    ) -> Instruction:
        """Record a conversation of an interactive step, end it, or pause it.

        ``transcript`` is the whole conversation in the plain form
        :func:`parse_transcript` reads; its entries are appended in order
        under one time, before ``choice`` and ``end`` apply.
        """
        self._validate_caller_role(caller_role)
        validate_task_id(task_id)
        operator = (operator or "").strip() or None
        agent = (agent or "").strip() or None
        choice = (choice or "").strip() or None
        if transcript is not None and (operator is not None or agent is not None):
            raise StateError(
                "--transcript records both sides; leave out --operator-said "
                "and --agent-said"
            )
        spoken = parse_transcript(transcript) if transcript is not None else ()
        if (
            not spoken
            and operator is None
            and agent is None
            and choice is None
            and not (end or pause)
        ):
            raise StateError(
                "interact needs --transcript, --operator-said, --agent-said, or "
                "--choice text, --end, or --pause"
            )
        if end and pause:
            raise StateError(
                "--end finishes the conversation and --pause leaves it "
                "open; use one of them"
            )
        with self.tasks.lock_task(task_id):
            self._authorize_worker(task_id, caller_role, assignment)
            state, snapshot = self.load(task_id)
            state = self._record_interaction(
                state,
                snapshot,
                operator=operator,
                agent=agent,
                transcript=spoken,
                choice=choice,
                end=end,
                pause=pause,
            )
            self.commit(state, snapshot)
            return self._tag_caller(self.render(state, snapshot), caller_role)

    def _record_interaction(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        *,
        operator: str | None = None,
        agent: str | None = None,
        transcript: Sequence[tuple[str, str]] = (),
        choice: str | None = None,
        end: bool = False,
        pause: bool = False,
        end_text: str = "The operator ended the interaction.",
    ) -> ExecutionState:
        """Append the entries of one exchange and update the step's record.

        The one recording path for a conversation, whether the operator spoke
        in the session or answered on the operator page.  Callers hold the
        task lock and commit the returned state.
        """
        item, record = _interactive_item(state, snapshot)
        entries = record.interaction_entries
        chosen = record.chosen
        spoken: list[tuple[str, str]] = []
        if choice is not None:
            chosen = resolve_choice(item, choice)
            spoken.append(("operator", f"Choice: {chosen}"))
        spoken.extend(transcript)
        spoken.extend(
            (speaker, text)
            for speaker, text in (("operator", operator), ("agent", agent))
            if text is not None
        )
        entries += len(spoken)
        if end:
            if entries == 0:
                raise StateError(
                    "nothing was recorded; record the operator's words before "
                    "ending the interaction"
                )
            if item.choices and chosen is None:
                raise StateError(
                    f"{item.name!r} offers choices; record the operator's pick "
                    "with --choice before ending the interaction"
                )
            spoken.append(("end", end_text))
        if pause:
            spoken.append(("pause", "The operator is done for now."))
        self.interactions.append_entries(
            state.task_id,
            spoken,
            run_id=state.run_id,
            step=item.name,
            item_id=item.item_id,
            at=_now(),
        )
        records = list(state.item_executions)
        records[state.cursor] = replace(
            record,
            interaction_entries=entries,
            interaction_ended=end,
            chosen=chosen,
        )
        # A pause is lifted by the operator's own words, not by the agent's or
        # by ending; those may happen while they are away.
        spoke = (
            operator is not None
            or choice is not None
            or any(speaker == "operator" for speaker, _ in transcript)
        )
        return replace(
            state,
            item_executions=tuple(records),
            operator_paused=pause or (state.operator_paused and not spoke),
            updated_at=_now(),
        )

    def interactions_text(self, task_id: str) -> str:
        """The task's whole record of conversations with the operator."""
        validate_task_id(task_id)
        return self.interactions.read(task_id)

    def loop(
        self,
        task_id: str,
        variables: tuple[tuple[str, str], ...] = (),
        artifact: str | None = None,
        metadata_values: tuple[tuple[str, str], ...] = (),
        selected_agent: str | None = None,
        selected_model: str | None = None,
        selected_reasoning: str | None = None,
        continue_loop: bool = False,
        *,
        summary_for_next: str | None = None,
        adjustments: str | None = None,
        caller_role: CallerRole | None = None,
        assignment: str | None = None,
    ) -> Instruction:
        """Complete a loop-control worker step."""
        self._validate_caller_role(caller_role)
        self._validate_selected_agent(selected_agent)
        for name, value in (
            ("selected model", selected_model),
            ("selected reasoning", selected_reasoning),
        ):
            if value is not None and not value.strip():
                raise StateError(f"{name} must be non-empty")
        validate_task_id(task_id)
        with self.tasks.lock_task(task_id):
            self._authorize_worker(task_id, caller_role, assignment)
            self._check_performer(task_id, caller_role, loop=True)
            opened = self._open_assignment(task_id, caller_role)
            instruction = self._complete(
                task_id,
                variables,
                artifact,
                metadata_values,
                selected_agent=selected_agent,
                selected_model=selected_model,
                selected_reasoning=selected_reasoning,
                summary_for_next=summary_for_next,
                adjustments=adjustments,
                caller_role=caller_role,
                stopping_loop=not continue_loop,
                continuing_loop=continue_loop,
            )
            instruction = self._with_handoff(
                task_id,
                self._open_next_in_single(task_id, instruction),
                opened,
                loop="continue" if continue_loop else "break",
            )
        return replace(
            self._tag_caller(instruction, caller_role),
            completion_registered=True,
        )

    def _fail(self, task_id: str, error: str) -> Instruction:
        state, snapshot = self.load(task_id)
        if not state.active_item_id or state.cursor >= len(snapshot.plan.items):
            raise StateError("no agent item is in progress; use next")
        item = snapshot.plan.items[state.cursor]
        if needs_repair(state):
            state = replace(
                state, status="failed", failure_kind="work_failed", last_error=error
            )
            self.commit(state, snapshot)
            return self.render(state, snapshot)
        if item.id != state.active_item_id or item.owner != "agent":
            raise StateError("active plan item does not match the execution cursor")
        state = fail_agent_item(state, snapshot.plan, item, error, _now)
        self.commit(state, snapshot)
        return self.render(state, snapshot)

    def complete(
        self,
        task_id: str,
        variables: tuple[tuple[str, str], ...] = (),
        artifact: str | None = None,
        metadata_values: tuple[tuple[str, str], ...] = (),
        selected_agent: str | None = None,
        selected_model: str | None = None,
        selected_reasoning: str | None = None,
        *,
        summary_for_next: str | None = None,
        adjustments: str | None = None,
        caller_role: CallerRole | None = None,
        rule_results: tuple[str, ...] = (),
        assignment: str | None = None,
    ) -> Instruction:
        """Complete the active agent item.

        A verification item reports one ``rule_results`` JSON object, a
        verdict, per rule it covers.
        """
        self._validate_caller_role(caller_role)
        self._validate_selected_agent(selected_agent)
        for name, value in (
            ("selected model", selected_model),
            ("selected reasoning", selected_reasoning),
        ):
            if value is not None and not value.strip():
                raise StateError(f"{name} must be non-empty")
        request = (
            self.storage.read_bootstrap(task_id)
            if is_bootstrap_request(task_id)
            else None
        )
        if request is not None:
            if metadata_values:
                raise StateError("bootstrap completion cannot save task metadata")
            if rule_results:
                raise StateError("a bootstrap completion verifies no rules")
            if adjustments:
                raise StateError("a bootstrap completion records no adjustments")
            return replace(
                self._tag_caller(
                    self.bootstrap.complete(
                        task_id,
                        request,
                        variables,
                        artifact,
                        start_task=self._start,
                        status_task=self.instruction_status,
                        bind_child=self.children.bind_child,
                    ),
                    caller_role,
                ),
                completion_registered=True,
            )
        validate_task_id(task_id)
        with self.tasks.lock_task(task_id):
            self._authorize_worker(task_id, caller_role, assignment)
            self._check_performer(task_id, caller_role)
            opened = self._open_assignment(task_id, caller_role)
            instruction = self._complete(
                task_id,
                variables,
                artifact,
                metadata_values,
                selected_agent=selected_agent,
                selected_model=selected_model,
                selected_reasoning=selected_reasoning,
                summary_for_next=summary_for_next,
                adjustments=adjustments,
                caller_role=caller_role,
                rule_results=rule_results,
            )
            held = instruction.completion_held
            instruction = self._with_handoff(
                task_id,
                replace(
                    self._open_next_in_single(task_id, instruction),
                    completion_held=held,
                ),
                opened,
            )
        self.children.reconcile_after_child(task_id)
        return replace(
            self._tag_caller(instruction, caller_role),
            completion_registered=True,
        )

    def _open_next_in_single(
        self, task_id: str, instruction: Instruction
    ) -> Instruction:
        """After a completion in the single runtime, open the next agent step.

        One session does every step, so the ``next`` it would run anyway is
        run for it and the completion shows the next step's own page.  The
        ``auto`` runtime keeps the manager's ``next``, which chooses a worker.
        Callers hold the task lock.
        """
        state, snapshot = self.load(task_id)
        plan = snapshot.plan
        if (
            state.workflow_runtime != "single"
            or state.status not in ("pending", "in_progress")
            or state.active_item_id is not None
            or state.cursor >= len(plan.items)
            or plan.items[state.cursor].owner != "agent"
        ):
            return instruction
        try:
            return self._next(task_id)
        except StateError:
            # The completion stands; opening the next step needs something
            # only the agent can give, such as an assessment outcome, and
            # the pending page says what.
            state, snapshot = self.load(task_id)
            return self.render(state, snapshot)

    def _complete(
        self,
        task_id: str,
        variables: tuple[tuple[str, str], ...],
        artifact: str | None,
        metadata_values: tuple[tuple[str, str], ...],
        selected_agent: str | None = None,
        selected_model: str | None = None,
        selected_reasoning: str | None = None,
        *,
        summary_for_next: str | None = None,
        adjustments: str | None = None,
        caller_role: CallerRole | None = None,
        stopping_loop: bool = False,
        continuing_loop: bool = False,
        initialization: bool = False,
        drain_stop: int | None = None,
        rule_results: tuple[str, ...] = (),
        replayed: bool = False,
    ) -> Instruction:
        """Complete the active agent item; see :meth:`complete`.

        ``replayed`` records a held completion for a caller who is not the
        step's worker, such as a verifier: the step's automatic
        follow-ups still run, but its next agent item is not opened for that
        caller, whose assignment ends here.
        """
        state, snapshot = self.load(task_id)
        state, snapshot = self.metadata_publisher.reconcile(state, snapshot)
        if state.status == "failed":
            return self.render(state, snapshot)
        if needs_repair(state):
            if (
                variables
                or metadata_values
                or rule_results
                or adjustments
                or stopping_loop
                or continuing_loop
            ):
                raise StateError(
                    "repair completion accepts only an artifact and summary"
                )
            return self._complete_repair(state, snapshot, artifact, summary_for_next)
        supplied = validate_values(variables)
        supplied_metadata = group_metadata_values(metadata_values)
        if "task_id" in supplied:
            raise StateError(
                "task_id is reserved for bootstrap start without an explicit task ID"
            )
        if state.status == "awaiting_input":
            if adjustments:
                raise StateError("supplying values records no adjustments")
            return self._complete_pending_input(
                state,
                snapshot,
                supplied,
                supplied_metadata,
                selected_agent=selected_agent,
                selected_model=selected_model,
                selected_reasoning=selected_reasoning,
                caller_role=caller_role,
            )
        if not state.active_item_id or state.cursor >= len(snapshot.plan.items):
            raise StateError("no agent item is in progress; use next")
        item = snapshot.plan.items[state.cursor]
        if item.id != state.active_item_id or item.owner != "agent":
            raise StateError("active plan item does not match the execution cursor")
        if stopping_loop and item.loop_break is None:
            raise StateError("active step is not permitted to break a loop")
        if continuing_loop and item.loop_continue is None:
            raise StateError("active step is not permitted to continue a loop")
        # A break that ends per-child stages has no loop wrapper.
        loop_entry = (
            snapshot.plan.items[enclosing_loop_entry_index(snapshot.plan, state.cursor)]
            if (stopping_loop and not item.breaks_children) or continuing_loop
            else None
        )
        assignment = active_assignment(
            snapshot.plan, state.assignment_item_id, runtime=state.workflow_runtime
        )
        if initialization:
            window_stop: int | None = state.cursor + 1
        else:
            window_stop = assignment.stop if assignment else None
        required, _ = completion_window(snapshot.plan, state.cursor, window_stop)
        validate_requested_values(supplied, required)
        self._validate_supplied_inputs(
            state,
            completion_window_items(snapshot.plan, state.cursor, window_stop),
            supplied,
        )
        task_metadata, project_metadata = validate_metadata_values(
            supplied_metadata, item.save_metadata
        )
        artifact_required = (
            item.phase == "step"
            and item.artifact
            and item.item_operation is None
            and item.child_operation is None
        ) or bool(loop_entry is not None and loop_entry.artifact)
        if artifact_required and (artifact is None or not artifact.strip()):
            raise StateError(
                f"artifact is required to complete {item.name!r}; "
                "pass a non-empty --artifact or configure artifact: false"
            )
        if item.update_item:
            self._require_item_fields(task_id, state, item)
        active_record = state.item_executions[state.cursor]
        if item.interactive and not active_record.interaction_ended:
            raise StateError(
                f"{item.name!r} is interactive: once the operator says the "
                "conversation is done, record it with `interact --transcript - "
                "--end`, then complete"
            )
        summary_for_next = (summary_for_next or "").strip() or None
        if summary_for_next is not None and len(summary_for_next) > SUMMARY_LIMIT:
            raise StateError(
                f"{SUMMARY_FLAG} has {len(summary_for_next)} characters; keep it "
                f"to {SUMMARY_LIMIT}: one or two short sentences, with the "
                "detail in the artifact"
            )
        if item.hands_over and summary_for_next is None:
            raise StateError(
                f"{item.name!r} needs {SUMMARY_FLAG}: one or two short sentences "
                "the next step will read about what was done and what it should know"
            )
        adjustments = (adjustments or "").strip() or None
        if adjustments is not None and len(adjustments) > ADJUSTMENTS_LIMIT:
            raise StateError(
                f"{ADJUSTMENTS_FLAG} has {len(adjustments)} characters; keep it "
                f"to {ADJUSTMENTS_LIMIT}: one or two short sentences, with the "
                "detail in the artifact"
            )
        if adjustments is not None and item.verifies is not None:
            raise StateError(
                f"{item.name!r} is a verification: it reports rule results, "
                f"not {ADJUSTMENTS_FLAG}"
            )
        if item.child_operation == "collect" and not self.tasks.read_children(
            task_id, state.run_id
        ):
            raise StateError(
                "children step completed without recorded children; use add-child"
            )
        if item.verifies is not None:
            return self._complete_verification(
                state,
                snapshot,
                item,
                artifact,
                rule_results,
                caller_role=caller_role,
            )
        if rule_results:
            raise StateError(
                f"--rule-result reports a verification; {item.name!r} is not one"
            )
        held = active_record.held_completion
        waived = {key for key, _ in active_record.checks_waived}
        check_report = None
        if any(
            check.id not in waived
            for check in (*item.checks, *active_record.resolved_checks)
        ):
            check_report = self.rule_checker.run(
                state,
                item,
                self._check_scope(state, snapshot.plan, item),
                reuse=held.report if held is not None else None,
            )
            if check_report.failed:
                # Rejected: nothing of the completion is recorded, and the
                # step goes back to its worker, or to the operator at the limit.
                # A held completion failing a newly approved check waits to be
                # handed back: whoever triggered the check is not its worker.
                state = reject_completion(
                    state,
                    snapshot.plan,
                    item,
                    check_report,
                    artifact,
                    _now,
                    keep_active=held is None,
                )
                self.commit(state, snapshot)
                return self.render(state, snapshot)
        if to_verify(item, active_record):
            held_page = self._hold_for_verification(
                state,
                snapshot,
                item,
                check_report,
                artifact,
                HeldCompletion(
                    variables=variables,
                    metadata_values=metadata_values,
                    selected_agent=selected_agent,
                    selected_model=selected_model,
                    selected_reasoning=selected_reasoning,
                    summary_for_next=summary_for_next,
                    adjustments=adjustments,
                    loop_control=(
                        "break"
                        if stopping_loop
                        else "continue"
                        if continuing_loop
                        else None
                    ),
                ),
            )
            if held_page is not None:
                return held_page
        updated_metadata, project_publication = self.metadata_publisher.prepare(
            task_id, state, item, task_metadata, project_metadata
        )
        promised_documents = self._promised_documents(snapshot.plan, item, state)
        artifact_reference, wrapper_artifact_reference = write_completion_artifacts(
            self.tasks,
            task_id,
            state,
            snapshot,
            item,
            loop_entry,
            artifact,
            rules=rule_outcomes(state, item, check_report),
        )
        selection = _normalize_completion_selection(
            selected_agent,
            selected_model,
            selected_reasoning,
            state.item_executions[state.cursor].selected_agent,
            state.item_executions[state.cursor].selected_model,
        )
        reported_items = self._reported_items(
            task_id,
            state,
            item,
            reports_item_on_completion(snapshot.plan, state.cursor),
        )
        state = complete_agent_item(
            state,
            snapshot.plan,
            supplied,
            artifact_reference,
            _now,
            selected_agent=selection.selected_agent,
            selected_model=selection.selected_model,
            selected_reasoning=selection.selected_reasoning,
            clear_selected_model=selection.clear_selected_model,
            clear_selected_reasoning=selection.clear_selected_reasoning,
            summary_for_next=summary_for_next,
            check_report=check_report,
            adjustments=adjustments,
        )
        if initialization:
            state = replace(state, pending_init_artifact=None)
        state = replace(
            state,
            pending_task_metadata=(updated_metadata.values if updated_metadata else ()),
            pending_project_metadata=project_publication,
        )
        if stopping_loop:
            state = request_loop_exit(
                state,
                snapshot.plan,
                item,
                wrapper_artifact_reference,
                _now,
            )
        elif continuing_loop:
            state = request_loop_continue(state, snapshot.plan, item, _now)
        elif item.item_operation == "collect":
            state, snapshot = materialize_item_plan(
                state,
                snapshot,
                item,
                self.tasks.read_items(task_id, state.run_id),
                _now,
            )
        elif item.child_operation == "collect":
            state, snapshot = materialize_child_plan(
                state, snapshot, self.tasks.read_children(task_id, state.run_id), _now
            )
        if reported_items is None:
            self.commit(state, snapshot)
        else:
            self._commit_items(state, snapshot, reported_items)
        for document in promised_documents:
            self.documents.record_update(
                document,
                task_id,
                run_id=state.run_id,
                step=item.name,
                updated_at=_now(),
                workspace=resolve_workspace(self.storage.root, state.working_directory),
            )
        state, snapshot = self.metadata_publisher.reconcile(state, snapshot)
        if caller_role is not None:
            if state.assignment_item_id is None:
                state = self._begin_assignment(
                    state,
                    snapshot,
                    model=(
                        state.item_executions[state.cursor - 1].model or state.model
                    ),
                    reasoning=(
                        state.item_executions[state.cursor - 1].reasoning
                        or state.reasoning
                    ),
                    cursor=max(0, state.cursor - 1),
                )
            state, snapshot = self._activate_or_handoff(
                state, snapshot, activate=not replayed
            )
        else:
            state, snapshot = self.drain(
                state,
                snapshot,
                stop_at=(
                    drain_stop
                    if initialization
                    else (
                        self._initialization_stop(snapshot.plan)
                        if state.pending_init_artifact is not None
                        else None
                    )
                ),
            )
        return self.render(state, snapshot)

    def _validate_supplied_inputs(
        self,
        state: ExecutionState,
        consumers: tuple[PlanItem, ...],
        supplied: dict[str, str],
    ) -> None:
        """Refuse a value its handler would reject, while nothing is saved yet.

        The consuming handler sees its whole declared input set, already-known
        values included, and its own message becomes the refusal, so the agent
        corrects the value in place of a handler failure the operator would
        have to resolve.
        """
        known = {**dict(state.workflow_values), **supplied}
        for item in consumers:
            if item.owner != "ww" or item.execution != "automatic":
                continue
            if not any(value.name in supplied for value in item.provide):
                continue
            error = self.actions.validate_inputs(
                item,
                {
                    value.name: known[value.name]
                    for value in item.provide
                    if value.name in known
                },
            )
            if error is not None:
                raise StateError(f"{item.name} rejected the supplied value: {error}")

    def _complete_pending_input(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        supplied: dict[str, str],
        supplied_metadata: dict[str, tuple[str, ...]],
        *,
        selected_agent: str | None,
        selected_model: str | None,
        selected_reasoning: str | None,
        caller_role: CallerRole | None,
    ) -> Instruction:
        """Persist requested input, then resume the active assignment or plan."""
        validate_metadata_values(supplied_metadata, ())
        request = state.pending_input_request
        if request is None:
            raise StateError("task is awaiting input without an input request")
        validate_requested_values(supplied, request.values)
        index = _index_for_id(snapshot.plan, request.item_id)
        self._validate_supplied_inputs(state, (snapshot.plan.items[index],), supplied)
        state = supply_requested_input(
            state,
            index,
            supplied,
            _now,
            selected_agent=selected_agent or state.assignment_selected_agent,
            selected_model=(
                None
                if selected_model == "auto"
                else selected_model or state.assignment_selected_model
            ),
            selected_reasoning=(
                None
                if selected_reasoning == "auto"
                else selected_reasoning or state.assignment_selected_reasoning
            ),
        )
        self.commit(state, snapshot)
        if caller_role is not None and state.assignment_item_id is None:
            state = self._begin_assignment(
                state, snapshot, model=state.model, reasoning=state.reasoning
            )
        if caller_role is not None:
            state, snapshot = self._activate_or_handoff(state, snapshot)
        else:
            state, snapshot = self.drain(state, snapshot)
        return self.render(state, snapshot)

    def instruction(
        self,
        task_id: str,
        run_id: str | None = None,
        *,
        caller_role: CallerRole | None = None,
        assignment: str | None = None,
    ) -> Instruction:
        self._validate_caller_role(caller_role)
        request = (
            self.storage.read_bootstrap(task_id)
            if is_bootstrap_request(task_id)
            else None
        )
        if request is not None:
            if run_id is not None:
                raise StateError("bootstrap requests do not have workflow runs")
            return replace(
                self._tag_caller(self.bootstrap.instruction(request), caller_role),
                manager_intro=True,
            )
        validate_task_id(task_id)
        self._authorize_worker(task_id, caller_role, assignment, read=True)
        instruction, resolved = self._status_from_one_read(task_id, run_id)
        if caller_role != "worker" and run_id is None and resolved is not None:
            _, change = self._detect_plan_change(*resolved)
            if change is not None:
                return replace(
                    self._tag_caller(
                        _plan_changed_page(instruction, change), caller_role
                    ),
                    manager_intro=True,
                )
        if instruction.is_child_workflow_control:
            refreshed = self.children.refresh_parent(task_id)
            if refreshed is not None:
                instruction = refreshed
        if caller_role == "worker" and _manager_performs(instruction):
            # A worker that outlived its assignment must not perform the
            # manager's item: its page names the item and offers no command.
            instruction = replace(
                instruction,
                manager_only=True,
                continuation_command=None,
                loop_break_command=None,
                loop_continue_command=None,
                interact_commands=None,
            )
        return replace(self._tag_caller(instruction, caller_role), manager_intro=True)

    def check(self, task_id: str) -> CheckPreview:
        """Run the active step's checks now, as its completion would.

        Nothing is recorded: no attempt counts, the record is untouched, and
        no command output is kept. Rules a verifier judges are only named,
        since a verifier judges them when the step completes.
        """
        validate_task_id(task_id)
        state, snapshot = self.load(task_id)
        item, record = self._active_step(state, snapshot, "nothing to check")
        report = RuleChecker(None, _now).run(
            state, item, self._check_scope(state, snapshot.plan, item)
        )
        return check_preview(state, item, report)

    def dispute(
        self,
        task_id: str,
        check_id: str,
        reason: str,
        *,
        caller_role: CallerRole | None = None,
        assignment: str | None = None,
    ) -> Instruction:
        """Stop for the operator: the step's worker disputes a check.

        Only a check that rejected a completion of the step in progress can
        be disputed. The dispute goes into the project's dispute log, then
        onto the step's record; the operator lets the check stand or waives
        it for this step. Nothing is written to the rule-automation store.
        """
        self._validate_caller_role(caller_role)
        validate_task_id(task_id)
        reason = reason.strip()
        if not reason:
            raise StateError("dispute --reason must be non-empty")
        with self.tasks.lock_task(task_id):
            self._authorize_worker(task_id, caller_role, assignment)
            opened = self._open_assignment(task_id, caller_role)
            state, snapshot = self.load(task_id)
            item, _ = self._active_step(state, snapshot, "nothing to dispute")
            failed = [
                (report, result)
                for report in item_reports(state)
                for result in report.failed
                if result.id == check_id
            ]
            if not failed:
                raise StateError(
                    f"nothing to dispute: no rejected completion of {item.name!r} "
                    f"failed {check_id!r}; run check first to see what fails, "
                    "and dispute a check the fix page names"
                )
            report, result = failed[-1]
            dispute = Dispute(
                check=check_id,
                reason=reason,
                attempt=report.attempt,
                disputed_at=_now(),
                command=result.command,
                output=result.output,
            )
            rule = next((rule for rule in item.rules if rule.id == check_id), None)
            self.rule_disputes.append(
                DisputeEntry(
                    check=check_id,
                    text_hash=rule.text_hash if rule else None,
                    task_id=task_id,
                    run_id=state.run_id,
                    step=item.name,
                    reason=reason,
                    attempt=report.attempt,
                    disputed_at=dispute.disputed_at,
                )
            )
            state = dispute_check(state, snapshot.plan, dispute, _now)
            self.commit(state, snapshot)
            instruction = self._with_handoff(
                task_id, self.render(state, snapshot), opened
            )
        self.children.reconcile_after_child(task_id)
        return self._tag_caller(instruction, caller_role)

    def rule(self, task_id: str, rule_id: str) -> RuleView:
        """One rule or check of the task in full, as its plan froze it."""
        validate_task_id(task_id)
        state, snapshot = self.load(task_id)
        return rule_view(state, snapshot.plan, self.rule_store.load(), rule_id)

    def _active_step(
        self, state: ExecutionState, snapshot: PlanSnapshot, refusal: str
    ) -> tuple[PlanItem, PlanItemExecution]:
        """The agent step in progress, or a refusal naming what is missing."""
        plan = snapshot.plan
        if (
            state.status != "in_progress"
            or state.active_item_id is None
            or state.cursor >= len(plan.items)
        ):
            raise StateError(
                f"{refusal}: no step of task {state.task_id!r} is in progress"
            )
        item = plan.items[state.cursor]
        record = state.item_executions[state.cursor]
        if item.owner != "agent" or record.status != "in_progress":
            raise StateError(
                f"{refusal}: no step of task {state.task_id!r} is in progress"
            )
        if item.verifies is not None:
            raise StateError(
                f"{refusal}: {item.name!r} verifies another step's rules and has "
                "no checks of its own"
            )
        return item, record

    def task_status(
        self,
        task_id: str,
        run_id: str | None = None,
        *,
        caller_role: CallerRole | None = None,
    ) -> TaskStatus:
        """Return a compact summary of the currently active workflow run."""
        self._validate_caller_role(caller_role)
        if is_bootstrap_request(task_id):
            instruction = self.instruction(task_id, run_id, caller_role=caller_role)
            return TaskStatus(
                task_id=instruction.task_id,
                workflow=instruction.workflow,
                step=instruction.step,
                step_state=instruction.item_status or instruction.status,
                runtime=instruction.workflow_runtime,
                agent=instruction.selected_agent or instruction.requested_agent,
                model=instruction.selected_model or instruction.model,
                reasoning=instruction.selected_reasoning or instruction.reasoning,
            )
        direct_work = self.tasks.read_direct_work(task_id)
        if direct_work and not self.tasks.read_task_record(task_id)[0]:
            # Work registered against a task that never ran a workflow.
            return TaskStatus(
                task_id=task_id,
                workflow="none",
                step=None,
                step_state="direct work only",
                runtime=self.extensions.config.runtime,
                agent=None,
                model="-",
                reasoning="-",
                direct_work=direct_work,
            )
        state, snapshot = self.load(task_id, run_id)
        instruction = self.render(state, snapshot)
        if instruction.is_child_workflow_control:
            refreshed = self.children.refresh_parent(task_id)
            if refreshed is not None:
                instruction = refreshed
        step_state = next(
            (
                progress.status
                for progress in state.steps
                if progress.path == instruction.step
            ),
            instruction.item_status or instruction.status,
        )
        latest = _latest_adjustments(state, snapshot.plan)
        return TaskStatus(
            task_id=instruction.task_id,
            workflow=instruction.workflow,
            step=instruction.step,
            step_state=step_state,
            runtime=instruction.workflow_runtime,
            agent=instruction.selected_agent or state.agent,
            model=instruction.selected_model or instruction.model,
            reasoning=instruction.selected_reasoning or instruction.reasoning,
            adjustments=(latest[1] if latest else None),
            adjustments_step=(latest[0] or None if latest else None),
            direct_work=direct_work,
        )

    def status(
        self,
        task_id: str,
        run_id: str | None = None,
        *,
        caller_role: CallerRole | None = None,
        assignment: str | None = None,
    ) -> Instruction:
        """Backward-compatible service alias for :meth:`instruction`."""
        return self.instruction(
            task_id, run_id, caller_role=caller_role, assignment=assignment
        )

    def instruction_status(self, task_id: str, run_id: str | None) -> Instruction:
        """Render a task from one record read, so it reflects one revision."""
        return self._status_from_one_read(task_id, run_id)[0]

    def _status_from_one_read(
        self, task_id: str, run_id: str | None
    ) -> tuple[Instruction, tuple[ExecutionState, PlanSnapshot] | None]:
        """The task's page and the run it renders, from one record read.

        The run is ``None`` for the summary of a task with several runs.
        """
        runs, _, _ = self.tasks.read_task_record(task_id)
        if len(runs) > 1 and run_id is None:
            summary = Instruction(
                task_id=task_id,
                workflow="",
                status="task_summary",
                item_id=None,
                item_name=None,
                stage=None,
                step=None,
                parent=None,
                item_status=None,
                action_kind=None,
                action_text=None,
                task_runs=self.tasks.summarize_runs(runs),
                next_role="manager",
                control="handoff_manager",
            )
            return summary, None
        state, snapshot = self.runs.resolve(task_id, runs, run_id)
        return self.render(state, snapshot), (state, snapshot)

    def requirements(self, task_id: str, run_id: str | None = None) -> TaskRequirements:
        """The requirements ``init`` recorded, with their amendments, read only."""
        validate_task_id(task_id)
        state, snapshot = self.load(task_id, run_id)
        return TaskRequirements(
            task_id,
            self.instructions.requirements(state, snapshot.plan),
            self.tasks.read_amendments(task_id),
        )

    def amend(
        self,
        task_id: str,
        text: str,
        *,
        caller_role: CallerRole | None = None,
        assignment: str | None = None,
    ) -> Amendment:
        """Append a timestamped amendment to the task's requirements.

        The recorded requirements are never rewritten.  ``caller_role`` is who
        recorded it; without one the operator at the terminal did.
        """
        self._validate_caller_role(caller_role)
        validate_task_id(task_id)
        amendment_text = text.strip()
        if not amendment_text:
            raise StateError("amend requires non-empty --requirements")
        if len(amendment_text) > MAX_AMENDMENT_LENGTH:
            raise StateError(
                f"an amendment is a short clarification (at most "
                f"{MAX_AMENDMENT_LENGTH} characters); the original requirements "
                "stay as recorded"
            )
        with self.tasks.lock_task(task_id):
            self._authorize_worker(task_id, caller_role, assignment)
            state, _ = self.load(task_id)
            if state.status == "completed":
                raise StateError(
                    f"task {task_id!r} is already completed; its requirements "
                    "cannot be amended"
                )
            amendment = Amendment(_now(), caller_role or "operator", amendment_text)
            self.tasks.append_amendment(task_id, amendment)
        return amendment

    def record_direct_work(self, task_id: str, summary: str) -> DirectWork:
        """Register work done outside any workflow, with the commits ww finds.

        The task need not have a run; its directory is created when it is
        missing. Commits already known to ww, or already registered, are not
        counted again.
        """
        validate_task_id(task_id)
        text = summary.strip()
        if not text:
            raise StateError("record requires a non-empty --summary")
        with self.tasks.lock_task(task_id):
            entry = DirectWork(
                _now(),
                text,
                "agent",
                unseen_commits(
                    self.storage.root,
                    self.extensions.config,
                    task_id,
                    self.tasks.read_direct_work(task_id),
                ),
            )
            self.tasks.append_direct_work(task_id, entry)
        return entry

    def reconcile_direct_work(self, task_id: str) -> int:
        """Register commits ww had not seen as a ``reconciled`` entry.

        Returns how many commits it recorded. A task ww does not hold is left
        alone, and no commit is ever recorded twice.
        """
        validate_task_id(task_id)
        if not self.tasks.task_exists(task_id):
            return 0
        with self.tasks.lock_task(task_id):
            registered = self.tasks.read_direct_work(task_id)
            runs, _, _ = self.tasks.read_task_record(task_id)
            commits = unseen_commits(
                self.storage.root,
                self.extensions.config,
                task_id,
                registered,
                _run_windows(runs),
            )
            if not commits:
                return 0
            self.tasks.append_direct_work(
                task_id,
                DirectWork(_now(), reconciled_summary(commits), "reconciled", commits),
            )
        return len(commits)

    def documents_listing(self, task_id: str | None) -> list[dict[str, object]]:
        """Describe every declared document, with its file and last update."""
        workspace = None
        if task_id is not None:
            validate_task_id(task_id)
            runs, _, _ = self.tasks.read_task_record(task_id)
            run = self.runs.select_run(runs)
            if run is not None:
                workspace = resolve_workspace(
                    self.storage.root, run.state.working_directory
                )
        return self.documents.listing(
            self._load_configuration().documents, task_id, workspace
        )

    def _promised_documents(
        self, plan: WorkflowPlan, item: PlanItem, state: ExecutionState
    ) -> tuple[DocumentDefinition, ...]:
        """The documents ``item`` promised to update, each verified to exist."""
        declared = {document.name: document for document in plan.documents}
        workspace = resolve_workspace(self.storage.root, state.working_directory)
        promised = []
        for update in item.update_document:
            document = declared.get(update.name)
            if document is None:
                raise StateError(
                    f"document {update.name!r} is not declared in this run's plan"
                )
            path = self.documents.path(document, state.task_id, workspace)
            if not path.is_file():
                raise StateError(
                    f"{item.name!r} promised to update document {update.name!r}, "
                    f"but {path} does not exist; create it, then complete"
                )
            promised.append(document)
        return tuple(promised)

    def metadata(self, task_id: str) -> dict[str, object]:
        """Return a task's durable metadata as a nested JSON-ready mapping."""
        validate_task_id(task_id)
        if not self.tasks.task_exists(task_id):
            raise StateError(f"task not found: {task_id}")
        metadata = self.tasks.read_task_metadata(task_id)
        return metadata.to_dict() if metadata is not None else {}

    def project_metadata(self) -> dict[str, object]:
        """Return durable project metadata as a nested JSON-ready mapping."""
        metadata = self.project_metadata_store.read_project_metadata()
        return metadata.to_dict() if metadata is not None else {}

    def _runtime_values(
        self, state: ExecutionState, plan: WorkflowPlan
    ) -> dict[str, str]:
        project = dict(state.workflow_values).get(PROJECT)
        # A declared append key that nothing has filled yet reads as empty
        # rather than leaving its placeholder in a prompt.
        empty_lists = {
            (
                f"{PROJECT_METADATA_PREFIX}{saved.key}"
                if saved.scope == "project"
                else f"{METADATA_PREFIX}{saved.key}"
            ): ""
            for item in plan.items
            for saved in item.save_metadata
            if saved.append
        }
        workspace = resolve_workspace(self.storage.root, state.working_directory)
        document_paths = {
            f"{DOCUMENTS_PREFIX}{document.name}": str(
                self.documents.path(document, state.task_id, workspace)
            )
            for document in plan.documents
        }
        values = {
            **empty_lists,
            **document_paths,
            **self.metadata_publisher.values(state.task_id),
            **runtime_variable_values(
                self.storage.root,
                state.task_id,
                state.working_directory,
                project,
                self._project_names(),
                self._project_path(project),
            ),
        }
        current = plan.items[state.cursor] if state.cursor < len(plan.items) else None
        choice_step = current
        if current is not None and current.phase != "step":
            choice_step = next(
                (
                    item
                    for item in plan.items
                    if item.step == current.step and item.phase == "step"
                ),
                current,
            )
        values[CHOICES] = json.dumps(
            [choice.label for choice in choice_step.choices]
            if choice_step is not None
            else [],
            ensure_ascii=False,
        )
        bound: dict[str, dict[str, object] | None] = {}
        for item in plan.items:
            if not isinstance(item.operation, PlannedAction):
                continue
            if not actions.contains(item.kind):
                continue
            implementation = actions.get(item.kind)
            binding = implementation.traits(
                item.payload_as(implementation.planned_type)
            ).extension_binding
            if binding is None:
                continue
            identifier = parse_reference(binding.reference).identifier
            if identifier not in bound:
                bound[identifier] = binding.settings
        values = self.extensions.apply_variable_overrides(
            values,
            tuple(bound.items()),
            task_id=state.task_id,
            run_id=state.run_id,
            workflow=state.workflow,
            workflow_values=dict(state.workflow_values),
            workspace=workspace,
            lane=plan.lane,
        )
        # Resolved now, for the task as it stands; a value not available yet
        # is left out, and a step reading it stops for the operator
        # (``value_unavailable``) before it starts.
        return {
            **values,
            **self.extensions.namespace_values(
                self.extensions.namespace_variables(),
                task_id=state.task_id,
                run_id=state.run_id,
                workflow=state.workflow,
                workflow_values=dict(state.workflow_values),
                workspace=workspace,
                project=project or None,
                lane=plan.lane,
            ),
        }

    def _unavailable_values(
        self, state: ExecutionState, plan: WorkflowPlan, item: PlanItem
    ) -> tuple[str, ...]:
        """The ``ww.`` values ``item``'s templates read that are missing now."""
        return unavailable_ww_values(
            item.dependencies,
            {
                **dict(state.workflow_values),
                **self._runtime_values(state, plan),
                **self._child_values(state, plan, item),
            },
        )

    def _item_values(
        self, state: ExecutionState, plan: WorkflowPlan, item: PlanItem
    ) -> dict[str, str]:
        """``{{ww.item.*}}`` for a per-item stage: its item as stored right now.

        Read from the run's items on every call, so a value changed in an
        earlier pass or by ``update-item`` after a gate stop is what renders;
        nothing is frozen into the plan.  A step with no bound item, or whose
        item is gone, has none: reading one is a context error.
        """
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
        return item_binding_values(item.item_id, work, item.dependencies)

    def _child_values(
        self, state: ExecutionState, plan: WorkflowPlan, item: PlanItem
    ) -> dict[str, str]:
        """``{{ww.child.*}}`` for a per-child stage: its child as it stands now.

        The child's extension values (``{{ww.child.git.branch}}``) are
        resolved for the child's own task; one its extension cannot give yet,
        such as the branch of a child that has not started, is left out.
        """
        if item.child_number is None:
            return {}
        children = self.tasks.read_children(state.task_id, state.run_id)
        if item.child_number > len(children):
            return {}
        child = children[item.child_number - 1]
        runs, _, _ = self.tasks.read_task_record(child.task_id)
        run = next(
            (
                entry
                for entry in reversed(runs)
                if entry.state.parent_task_id == state.task_id
                and entry.state.start_operation_id == child.start_operation_id
            ),
            None,
        )
        workflow = child.workflow or next(
            (
                coordinator.workflow
                for entry in plan.items
                if entry.child_number == item.child_number
                and (coordinator := child_workflow(entry)) is not None
            ),
            state.workflow,
        )
        namespaced = self.extensions.namespace_values(
            self.extensions.namespace_variables(),
            task_id=child.task_id,
            run_id=run.run_id if run else None,
            workflow=workflow,
            workflow_values=dict(run.state.workflow_values) if run else {},
            workspace=(
                resolve_workspace(self.storage.root, run.state.working_directory)
                if run
                else None
            ),
            project=child.project,
            lane=(run.snapshot.plan.lane if run else self._configured_lane(workflow)),
        )
        return {
            **child_values(child.id, child.description, child.project, child.fields),
            **{child_value_name(name): value for name, value in namespaced.items()},
        }

    def _stage_child_id(self, state: ExecutionState, item: PlanItem) -> str | None:
        """The ID of the child a per-child stage belongs to, when it exists."""
        if item.child_number is None:
            return None
        children = self.tasks.read_children(state.task_id, state.run_id)
        if item.child_number > len(children):
            return None
        return children[item.child_number - 1].id

    def _unavailable_error(
        self, names: tuple[str, ...], state: ExecutionState, item: PlanItem
    ) -> str:
        """Name each missing value and what provides it.

        A child's field comes from its record, so the message says how to
        set it; any other value comes from an extension, the child's own for
        ``{{ww.child.<namespace>.*}}``.
        """
        owners = {
            name: identifier
            for name, (identifier, _) in self.extensions.namespaces().items()
        }

        def describe(name: str) -> str:
            if name.startswith(CHILD_FIELD_PREFIX):
                field = name.removeprefix(CHILD_FIELD_PREFIX)
                child = self._stage_child_id(state, item) or "<child-id>"
                return (
                    f"{{{{{name}}}}} (the child has no field {field!r}; set it "
                    f"with `ww update-child {state.task_id} {child} --field "
                    f"{field}=<value>`, or `--field` on add-child)"
                )
            _, _, rest = name.partition(".")
            if name.startswith(CHILD_VALUE_PREFIX):
                _, _, rest = rest.partition(".")
            owner = owners.get(rest.partition(".")[0])
            source = f" (provided by {owner})" if owner else ""
            return f"{{{{{name}}}}}{source}"

        return "template value(s) not available for this task yet: " + ", ".join(
            describe(name) for name in names
        )

    def reset(self, task_id: str) -> ResetResult:
        validate_task_id(task_id)
        with self.tasks.lock_task(task_id):
            children = self.tasks.child_task_ids(task_id)
            if children:
                raise StateError(
                    f"cannot reset {task_id!r} while child task(s) exist: "
                    + ", ".join(children)
                )
            # Side records go first, so the task directory is empty for the
            # storage adapter to remove.
            self.interactions.remove(task_id)
            self.hook_records.remove(task_id)
            self.documents.remove_task(task_id)
            # Extension records outlive task state otherwise, and a later
            # task under the same ID would inherit them.
            self.extensions.forget_task(task_id)
            return ResetResult(task_id, self.tasks.remove_task(task_id))

    def interruption(self, task_id: str) -> Interruption | None:
        """The task's interruption while its interrupted attempt is still open."""
        validate_task_id(task_id)
        return self.hook_records.interruption(task_id)

    def interruptions(self) -> tuple[tuple[str, Interruption], ...]:
        """Every task still marked as interrupted, newest first."""
        return self.hook_records.interruptions()

    def cleanup(self) -> CleanupResult:
        """Prune obsolete lock sidecars outside any task-specific state."""
        return CleanupResult(self.storage.cleanup_locks())

    def items(self, task_id: str, run_id: str | None = None) -> tuple[WorkItem, ...]:
        validate_task_id(task_id)
        runs, _handoff, _revision = self.tasks.read_task_record(task_id)
        selected = self.runs.select_run(runs, run_id)
        if selected is None:
            if run_id is not None:
                return ()
            raise StateError(f"task {task_id!r} has not been started; use start")
        return selected.items

    def item(self, task_id: str, item_id: str, run_id: str | None = None) -> WorkItem:
        """Return one work item from one workflow run."""
        for item in self.items(task_id, run_id):
            if item.id == item_id:
                return item
        raise StateError(f"item {item_id!r} was not found")

    def find_item(
        self, task_id: str, name: str, value: str, run_id: str | None = None
    ) -> WorkItem:
        """Return the work item whose custom field ``name`` holds ``value``."""
        for item in self.items(task_id, run_id):
            if item.field(name) == value:
                return item
        raise StateError(f"no item has {name} = {value!r}")

    def _check_item_fields(
        self,
        task_id: str,
        plan: WorkflowPlan,
        items: tuple[WorkItem, ...],
        item: WorkItem,
        *,
        adding: bool,
    ) -> None:
        """Enforce the flow's identity and unique fields for one item.

        A new item must carry the identity field.  Across the unique fields,
        a value may appear once over all items of the run and, when the
        flow is shared, of the task's store.
        """
        collect = item_collection(plan)
        if collect is None:
            return
        if adding and collect.item_identity and not item.field(collect.item_identity):
            raise StateError(
                f"item {item.id!r} needs the field {collect.item_identity!r}: "
                f"pass --field {collect.item_identity}=<value>"
            )
        if not collect.item_unique:
            return
        others = [entry for entry in items if entry.id != item.id]
        if collect.shared_items:
            others += [
                entry
                for entry in self.tasks.read_shared_items(task_id)
                if entry.id != item.id
            ]
        taken = {
            entry.field(name): (entry.id, name)
            for entry in others
            for name in collect.item_unique
            if entry.field(name)
        }
        for name in collect.item_unique:
            value = item.field(name)
            if value and value in taken:
                holder, held_as = taken[value]
                raise StateError(
                    f"{name} {value!r} is already the {held_as} of item {holder!r}"
                )

    def artifacts(
        self, task_id: str, run_id: str | None = None
    ) -> tuple[dict[str, str], ...]:
        """Return stable artifact references for one workflow run.

        Each reference is project-relative and stable across machines; ``path``
        beside it is the absolute location, which a worker running in a linked
        worktree needs because ``.ww`` lives under the primary checkout.
        """
        validate_task_id(task_id)
        state, snapshot = self.load(task_id, run_id)
        artifacts: list[dict[str, str]] = []
        for item, record in zip(
            snapshot.plan.items, state.item_executions, strict=True
        ):
            if record.status != "completed" or record.artifact is None:
                continue
            artifact = {
                "step": item.step,
                "artifact": record.artifact,
                "path": str(self.storage.root / record.artifact),
            }
            if item.phase != "step":
                artifact["hook"] = item.name
                artifact["hook_phase"] = item.phase
            artifacts.append(artifact)
        item_by_id = {item.id: item for item in snapshot.plan.items}
        listed_checks: set[str] = set()
        for record in (*state.item_executions, *state.execution_history):
            recorded = item_by_id.get(record.plan_item_id)
            if (
                recorded is None
            ):  # pragma: no cover - aggregate validation prevents this
                continue
            for repair_reference in record.repair_artifacts:
                if repair_reference not in listed_checks:
                    listed_checks.add(repair_reference)
                    artifacts.append(
                        {
                            "step": recorded.step,
                            "repair_artifact": repair_reference,
                            "path": str(self.storage.root / repair_reference),
                        }
                    )
            for command in record.commands:
                for stream, command_reference in (
                    ("stdout", command.stdout_ref),
                    ("stderr", command.stderr_ref),
                ):
                    if command_reference is None:
                        continue
                    artifacts.append(
                        {
                            "step": recorded.step,
                            "command_output": command_reference,
                            "path": str(self.storage.root / command_reference),
                            "stream": stream,
                            "operation_id": command.operation_id or "unknown",
                            "attempt": str(command.attempts),
                        }
                    )
            for report in record.check_reports:
                for result in report.results:
                    for stream, check_reference in (
                        ("stdout", result.stdout_ref),
                        ("stderr", result.stderr_ref),
                    ):
                        # A retried record's copy in the history repeats them.
                        if check_reference is None or check_reference in listed_checks:
                            continue
                        listed_checks.add(check_reference)
                        artifacts.append(
                            {
                                "step": recorded.step,
                                "command_output": check_reference,
                                "path": str(self.storage.root / check_reference),
                                "stream": stream,
                                "check": result.id,
                                "attempt": str(report.attempt),
                            }
                        )
        return tuple(artifacts)

    def add_item(self, task_id: str, item: WorkItem) -> WorkItem:
        validate_task_id(task_id)
        with self.tasks.lock_task(task_id):
            run_id = self.tasks.active_execution_run(task_id)
            if run_id is None:
                raise StateError(f"task {task_id!r} has no active workflow run")
            state, snapshot = self.load(task_id, run_id)
            items = self.tasks.read_items(task_id, run_id)
            if any(existing.id == item.id for existing in items):
                raise StateError(f"item {item.id!r} already exists")
            if item.reference_to_id and not any(
                existing.id == item.reference_to_id for existing in items
            ):
                raise StateError(
                    f"item reference {item.reference_to_id!r} does not exist"
                )
            self._check_item_fields(task_id, snapshot.plan, items, item, adding=True)
            self.commit(state, snapshot, items=(*items, item))
            self._share_items(task_id, snapshot.plan, (*items, item))
            return item

    def _project_names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.extensions.config.projects)

    def add_child(
        self,
        task_id: str,
        child_id: str | None,
        description: str,
        project: str | None = None,
        fields: tuple[tuple[str, str], ...] = (),
    ) -> ChildTask:
        validate_task_id(task_id)
        if not description.strip():
            raise StateError("child description must be non-empty")
        fields = _child_fields(fields)
        if project is not None:
            self._project_directory(project)
        with self.tasks.lock_task(task_id):
            state, snapshot = self.load(task_id)
            if not (
                state.active_item_id
                and snapshot.plan.items[state.cursor].child_operation == "collect"
            ):
                raise StateError(
                    "children can only be added while a children step is active"
                )
            children = self.tasks.read_children(task_id, state.run_id)
            if child_id is None and self._children_bind_identity(snapshot.plan):
                # The child gets its ID from its own first step; until then a
                # request ID names it, and ``start-child`` opens that request.
                child_id = generated_bootstrap_id()
                while any(entry.id == child_id for entry in children):
                    child_id = generated_bootstrap_id()
            elif child_id is None:
                child_id = self._generated_child_id(task_id, children, project)
            validate_child_id(child_id)
            if any(child.id == child_id for child in children):
                raise StateError(f"child {child_id!r} already exists")
            child = ChildTask(
                id=child_id,
                description=description,
                workflow="",
                task_id=f"{task_id}/{child_id}",
                start_operation_id=(
                    f"{task_id}:{operation_scope_for(state)}:{child_id}:start"
                ),
                parent_task_id=task_id,
                project=project,
                fields=fields,
            )
            self.commit(
                state,
                snapshot,
                children=(*children, child),
            )
            return child

    def update_child(
        self,
        task_id: str,
        child_id: str,
        *,
        text: str | None = None,
        project: str | None = None,
        fields: tuple[tuple[str, str], ...] = (),
    ) -> ChildTask:
        """Change a child's text, project, or custom fields.

        Text and project are allowed while the child is ``pending``: during
        the children step, in a per-child stage before the child runs, and
        while the parent waits for its children. A started child already
        holds its requirements. Custom fields only feed the parent's
        per-child stages, so they may change at any time.
        """
        validate_task_id(task_id)
        validate_child_id(child_id)
        if text is None and project is None and not fields:
            raise StateError("update-child needs --text, --project, or --field")
        if text is not None and not text.strip():
            raise StateError("child text must be non-empty")
        fields = _child_fields(fields)
        if project is not None:
            self._project_directory(project)
        with self.tasks.lock_task(task_id):
            state, snapshot = self.load(task_id)
            children = list(self.tasks.read_children(task_id, state.run_id))
            index = next(
                (i for i, child in enumerate(children) if child.id == child_id), None
            )
            if index is None:
                raise StateError(f"child {child_id!r} was not found")
            child = children[index]
            if (text is not None or project is not None) and child.status != "pending":
                raise StateError(
                    f"child {child_id!r} is {child.status}; only a pending child "
                    "can change its text or project"
                )
            child = replace(
                child,
                description=text if text is not None else child.description,
                project=project if project is not None else child.project,
            ).with_fields(dict(fields))
            children[index] = child
            self.commit(state, snapshot, children=tuple(children))
            return child

    def start_child(
        self,
        parent_task_id: str,
        child_id: str,
        *,
        workflow_name: str | None = None,
        workflow_runtime: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        agent: str | None = None,
    ) -> Instruction:
        return self.children.start_child(
            parent_task_id,
            child_id,
            workflow_name=workflow_name,
            workflow_runtime=workflow_runtime,
            model=model,
            reasoning=reasoning,
            agent=agent,
        )

    def _validate_child_workflow(
        self, workflow_name: str, child: ChildTask, parent: ExecutionState
    ) -> None:
        """Validate a launch target before freezing the parent's child record."""
        configuration = self._load_configuration()
        workflow = configuration.workflows_by_name.get(workflow_name)
        if workflow is None:
            raise ConfigurationError(f"workflow not found: {workflow_name}")
        require_lane(workflow)
        self._project_directory(child.project)
        plan = compile_workflow_plan(
            configuration,
            self.storage.root,
            workflow_name,
            child.agent or parent.agent,
            child.task_id,
            self.extensions,
            PlanCompilationOptions(task_id=child.task_id, project=child.project),
            self.extensions.config,
        )
        if any(item.child_operation is not None for item in plan.items):
            raise StateError("child workflows cannot use children")
        if (
            is_bootstrap_request(child.id)
            and self.bootstrap.step(
                configuration,
                workflow_name,
                (),
                child.agent or parent.agent,
                self._unknown_modes,
                project=child.project,
            )
            is None
        ):
            raise StateError(
                f"child workflow {workflow_name!r} declares no variable task_id in "
                "its first step; add the child with an explicit --id"
            )

    @staticmethod
    def _children_bind_identity(plan: WorkflowPlan) -> bool:
        return any(item.child_identity for item in plan.items)

    def _start_child_identity(
        self, child: ChildTask, workflow_name: str, parent: ExecutionState
    ) -> Instruction:
        """Open, or show, the identity request a child's own first step answers."""
        existing = self.storage.read_bootstrap(child.id)
        if existing is not None:
            return self.bootstrap.instruction(existing)
        item = self.bootstrap.step(
            self._load_configuration(),
            workflow_name,
            (),
            child.agent or parent.agent,
            self._unknown_modes,
            project=child.project,
        )
        if item is None:
            raise StateError(
                f"child workflow {workflow_name!r} declares no variable task_id in "
                "its first step; add the child with an explicit --id"
            )
        return self.bootstrap.start(
            workflow_name,
            (),
            child.agent or parent.agent,
            item,
            child.model or parent.model,
            child.reasoning or parent.reasoning,
            child.workflow_runtime or parent.workflow_runtime,
            None,
            f"Requirements for child task {child.id}: {child.description}",
            child.project,
            request_id=child.id,
            parent_task_id=parent.task_id,
            start_operation_id=child.start_operation_id,
        )

    def update_item(
        self,
        task_id: str,
        item_id: str,
        *,
        caller_role: CallerRole | None = None,
        **changes: object,
    ) -> ItemUpdateResult:
        validate_task_id(task_id)
        self._validate_caller_role(caller_role)
        unexpected = set(changes) - EDITABLE_WORK_ITEM_FIELDS - {"item"}
        if unexpected:
            raise StateError("unknown item field(s): " + ", ".join(sorted(unexpected)))
        custom = changes.pop("fields", None)
        if custom is not None and not isinstance(custom, dict):
            raise StateError("item fields must be a mapping")
        with self.tasks.lock_task(task_id):
            run_id = self.tasks.active_execution_run(task_id)
            if run_id is None:
                raise StateError(f"task {task_id!r} has no active workflow run")
            state, snapshot = self.load(task_id, run_id)
            if "item" in changes and not _collecting(state, snapshot):
                raise StateError(
                    "an item's text can change only while the collection step is "
                    "in progress"
                )
            items = list(self.tasks.read_items(task_id, run_id))
            index = next(
                (i for i, item in enumerate(items) if item.id == item_id), None
            )
            if index is None:
                raise StateError(f"item {item_id!r} was not found")
            try:
                updated = WorkItem.from_dict({**items[index].to_dict(), **changes})
                if custom is not None:
                    updated = updated.with_fields(dict(validate_item_fields(custom)))
            except ValueError as error:
                raise StateError(str(error)) from error
            if custom is not None:
                self._check_item_fields(
                    task_id, snapshot.plan, tuple(items), updated, adding=False
                )
            items[index] = updated
            self.commit(state, snapshot, items=tuple(items))
            self._share_items(task_id, snapshot.plan, tuple(items))
            instruction = self._tag_caller(self.render(state, snapshot), caller_role)
            return ItemUpdateResult(updated, instruction.continuation_command)

    def remove_item(self, task_id: str, item_id: str) -> WorkItem:
        """Drop an item while the collection step is in progress."""
        validate_task_id(task_id)
        with self.tasks.lock_task(task_id):
            run_id = self.tasks.active_execution_run(task_id)
            if run_id is None:
                raise StateError(f"task {task_id!r} has no active workflow run")
            state, snapshot = self.load(task_id, run_id)
            if not _collecting(state, snapshot):
                raise StateError(
                    "an item can be removed only while the collection step is in "
                    "progress"
                )
            items = self.tasks.read_items(task_id, run_id)
            removed = next((item for item in items if item.id == item_id), None)
            if removed is None:
                raise StateError(f"item {item_id!r} was not found")
            referrers = [item.id for item in items if item.reference_to_id == item_id]
            if referrers:
                raise StateError(
                    f"item {item_id!r} is referenced by " + ", ".join(referrers)
                )
            kept = tuple(item for item in items if item.id != item_id)
            self.commit(state, snapshot, items=kept)
            self._share_items(task_id, snapshot.plan, kept)
            return removed

    def initialize(
        self,
        *,
        workflows: str = DEFAULT_WORKFLOWS_YAML,
        project_config: str = DEFAULT_PROJECT_CONFIG_JSON,
        ignore_runtime: bool = False,
        skill_installs: tuple[tuple[str, str], ...] = (),
    ) -> InitializationResult:
        """Create the project files, with each chosen skill in its directory."""
        return self.storage.initialize_project(
            workflows,
            project_config,
            PROJECT_LAUNCHER,
            AGENT_INSTRUCTIONS,
            ignore_runtime=ignore_runtime,
            skills=tuple(
                (skill_location(directory, name), SKILLS[name])
                for directory, name in skill_installs
            ),
        )

    def drain(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        assignment_item_id: str | None = None,
        stop_at: int | None = None,
    ) -> tuple[ExecutionState, PlanSnapshot]:
        plan = snapshot.plan
        while state.cursor < len(plan.items):
            if stop_at is not None and state.cursor >= stop_at:
                return state, snapshot
            prior = state
            state = finish_loop_continue(state, plan, _now)
            if state is not prior:
                self.commit(state, snapshot)
            exiting_children = _exiting_children(state, plan)
            state = finish_loop_exit(state, plan, _now)
            if state is not prior:
                self.commit(
                    state,
                    snapshot,
                    children=(
                        skip_pending(
                            self.tasks.read_children(state.task_id, state.run_id)
                        )
                        if exiting_children and state.loop_exit_item_id is None
                        else None
                    ),
                )
            if state.cursor >= len(plan.items):
                break
            assignment = active_assignment(
                plan, assignment_item_id, runtime=state.workflow_runtime
            )
            if assignment_item_id is not None and (
                assignment is None or state.cursor >= assignment.stop
            ):
                return state, snapshot
            item = plan.items[state.cursor]
            if child_workflow(item) is not None:
                state = begin_child_workflow(state, item, _now)
                self.commit(state, snapshot)
                return state, snapshot
            blocked = self._block_unfinished_pass(state, plan)
            if blocked is not None:
                self.commit(blocked, snapshot)
                return blocked, snapshot
            record = state.item_executions[state.cursor]
            if needs_repair(state):
                return state, snapshot
            if record.status == "completed":
                state = advance_completed_item(state, _now)
                self.commit(state, snapshot)
                continue
            if workflow_transition(item) is not None:
                return self._handoff(state, snapshot, item)
            if loop_control(item) is not None:
                return state, snapshot
            if (
                item.name == INIT_STEP_NAME
                and item.step == INIT_STEP_NAME
                and item.phase == "step"
                and item.owner == "agent"
                and state.pending_init_artifact is not None
            ):
                state = begin_agent_item(
                    state,
                    plan,
                    item,
                    model=requested_setting(item.model) or state.model,
                    reasoning=requested_setting(item.reasoning) or state.reasoning,
                    now=_now,
                )
                self.commit(state, snapshot)
                return self._complete_initialization_item(state, snapshot, stop_at)
            if item.verifies is not None and not record.verification:
                # No round asks this verifier anything, for example after a
                # loop reset its record.
                state = skip_idle_verification(state, _now)
                self.commit(state, snapshot)
                continue
            if item.owner == "agent" and record.held_completion is not None:
                # Its verification finished, but ww stopped before recording
                # the completion: record it now.
                self._replay_held(state.task_id, state.cursor, caller_role=None)
                return self.load(state.task_id, state.run_id)
            if item.owner == "agent":
                unavailable = (
                    self._unavailable_values(state, plan, item)
                    if record.status != "in_progress"
                    else ()
                )
                state = (
                    stop_for_values(
                        state,
                        plan,
                        item,
                        self._unavailable_error(unavailable, state, item),
                        _now,
                    )
                    if unavailable
                    else pause_for_agent(state, _now)
                )
                self.commit(state, snapshot)
                return state, snapshot
            if pending_assessment(state, plan) is not None:
                # An outcome's automatic work waits for the chosen outcome,
                # as an outcome's agent work does.
                state = pause_for_agent(state, _now)
                self.commit(state, snapshot)
                return state, snapshot
            if not (item.execution == "automatic" and item.owner == "ww"):
                raise StateError(f"invalid automatic plan item {item.id!r}")
            missing = [
                value
                for value in item.provide
                if value.name not in dict(state.workflow_values)
            ]
            if missing:
                state = await_item_input(state, item, tuple(missing), _now)
                self.commit(state, snapshot)
                return state, snapshot
            state = self.actions.run(state, snapshot, item)
            if state.status == "failed":
                if item.on_failure == "fix":
                    state = request_repair(state, plan, item, _now)
                    self.commit(state, snapshot)
                return state, snapshot
        state = complete_run(state, plan, _now)
        self.commit(state, snapshot)
        return state, snapshot

    def _block_unfinished_pass(
        self, state: ExecutionState, plan: WorkflowPlan
    ) -> ExecutionState | None:
        """Stop before leaving an items pass whose declared phases are unmet.

        Checked once the pass's last stage is done and before the next item
        starts; only what the pass's stages declared, and actually ran, is
        required (see ``ww.item_passes``).  When that last stage is an
        assessment, the pass ends only once its outcome is chosen: an outcome
        whose work belongs to the pass runs first, and one that stops the
        workflow ends the run without a gate.
        """
        pass_id = leaving_pass(plan, state.cursor)
        following = state.item_executions[state.cursor]
        if (
            pass_id is None
            or following.status != "pending"
            or following.started_at is not None
            or pending_assessment(state, plan) is not None
        ):
            return None
        unfinished = pass_gate_failures(
            plan,
            state.item_executions,
            pass_id,
            self.tasks.read_items(state.task_id, state.run_id),
        )
        if not unfinished:
            return None
        message = (
            f"items pass {pass_id!r} cannot complete; its items lack what its "
            "stages declare: "
            + "; ".join(unfinished)
            + ". Record it with update-item, then retry"
        )
        return block_item_phase(state, message, _now)

    def _handoff(
        self, state: ExecutionState, snapshot: PlanSnapshot, item: PlanItem
    ) -> tuple[ExecutionState, PlanSnapshot]:
        if not snapshot.plan.handoff:
            raise StateError(
                "workflow transitions are only supported by handoff workflows"
            )
        decision = workflow_transition(item)
        if decision is None:
            raise StateError("handoff item has no workflow-transition capability")
        target = _render_template(
            decision.target,
            {
                **dict(state.workflow_values),
                **self._runtime_values(state, snapshot.plan),
            },
        )
        if not target or target == state.workflow:
            raise StateError("handoff target must name a different workflow")
        if self.tasks.read_handoff(state.task_id) is not None:
            raise StateError(f"task {state.task_id!r} already has a handoff marker")
        configuration = self._load_configuration()
        if target not in configuration.workflows_by_name:
            raise StateError(f"handoff target workflow not found: {target}")
        require_lane(configuration.workflows_by_name[target])
        new_snapshot = PlanSnapshot(
            schema_version=PLAN_SCHEMA_VERSION,
            compiler_version=PLAN_COMPILER_VERSION,
            configuration_digest=self._configuration_digest(configuration),
            compiled_at=_now(),
            plan=compile_workflow_plan(
                configuration,
                self.storage.root,
                target,
                state.agent,
                state.task_id,
                self.extensions,
                PlanCompilationOptions(
                    task_id=state.task_id,
                    project=dict(state.workflow_values).get(PROJECT) or None,
                    modes=state.modes,
                ),
                self.extensions.config,
            ),
        )
        # The selection workflow is a run in its own right: finish it, and give
        # the target its own numbered run rather than overwriting the plan and
        # state that recorded how the target was chosen.
        finished = project_steps(
            finish_selection(state, target, _now), snapshot.plan, _now
        )
        existing_runs, _ = self.tasks.read_task_aggregate(state.task_id)
        next_number = (
            max(
                (int(run.run_id.split("-", 1)[0]) for run in existing_runs),
                default=0,
            )
            + 1
        )
        run_id = self.tasks.run_id_for(next_number, target)
        carried = {
            name: value
            for name, value in state.workflow_values
            if name in {PROJECT, BRANCH_NAMING_STRATEGY}
        }
        # The target's declared settings apply to its run, as on ``start``;
        # the source run's values fill what it leaves out.
        declared = configuration.workflows_by_name[target]
        target_runtime = declared.runtime or state.workflow_runtime
        runtime_instruction(target_runtime)
        target_model = declared.model or state.model
        if declared.reasoning:
            target_reasoning = declared.reasoning
        elif target_model != state.model:
            target_reasoning = "auto"
        else:
            target_reasoning = state.reasoning
        base_state = initial_state(
            new_snapshot,
            state.modes,
            _now(),
            run_id=run_id,
            execution_instance_id=uuid.uuid4().hex,
            parent_task_id=state.parent_task_id,
            start_operation_id=state.start_operation_id,
            workflow_runtime=target_runtime,
            model=target_model,
            reasoning=target_reasoning,
        )
        next_state = replace(
            base_state,
            run_id=run_id,
            workflow_values=tuple(
                {**dict(base_state.workflow_values), **carried}.items()
            ),
            working_directory=self._project_directory(carried.get(PROJECT)),
            pending_init_artifact=(
                "Continue task requirements after handoff from "
                f"{state.workflow} to {target}."
            ),
        )
        runs, _ = self.tasks.read_task_aggregate(state.task_id)
        source_existing = next(
            (run for run in runs if run.run_id == state.run_id), None
        )
        source = TaskRunAggregate(
            run_id=finished.run_id,
            workflow=finished.workflow,
            snapshot=snapshot,
            state=finished,
            items=source_existing.items if source_existing else (),
            children=source_existing.children if source_existing else (),
            bootstrap_request_id=(
                source_existing.bootstrap_request_id if source_existing else None
            ),
        )
        target_run = TaskRunAggregate(
            run_id=run_id,
            workflow=target,
            snapshot=new_snapshot,
            state=next_state,
        )
        self._commit_runs(
            state.task_id,
            tuple(source if run.run_id == source.run_id else run for run in runs)
            + (() if any(run.run_id == source.run_id for run in runs) else (source,))
            + (target_run,),
            handoff=f"{state.workflow}={target}",
        )
        return self._complete_initialization(next_state, new_snapshot)

    def _complete_initialization(
        self, state: ExecutionState, snapshot: PlanSnapshot
    ) -> tuple[ExecutionState, PlanSnapshot]:
        """Advance only the persisted init lifecycle during start.

        Init preparation may itself pause, fail, or request input.  The
        submitted requirements stay on the state until the init item is
        reached; no first-user-step preparation can consume them during start.
        """
        return self.drain(
            state, snapshot, stop_at=self._initialization_stop(snapshot.plan)
        )

    @staticmethod
    def _initialization_stop(plan: WorkflowPlan) -> int:
        """Return the first item outside the implicit init lifecycle."""
        return next(
            (
                index
                for index, item in enumerate(plan.items)
                if item.step != INIT_STEP_NAME
            ),
            len(plan.items),
        )

    def _complete_initialization_item(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        stop_at: int | None,
    ) -> tuple[ExecutionState, PlanSnapshot]:
        """Complete init without opening a normal assignment completion window."""
        artifact = state.pending_init_artifact
        if artifact is None:  # pragma: no cover - guarded by _drain
            raise StateError("built-in init has no saved requirements")
        self._complete(
            state.task_id,
            (),
            artifact,
            (),
            initialization=True,
            drain_stop=stop_at,
        )
        return self.load(state.task_id, state.run_id)

    def _begin_assignment(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        *,
        model: str,
        reasoning: str,
        selected_agent: str | None = None,
        cursor: int | None = None,
    ) -> ExecutionState:
        """Persist the structural assignment selected by a manager command."""
        assignment = assignment_at(
            snapshot.plan,
            state.cursor if cursor is None else cursor,
            runtime=state.workflow_runtime,
        )
        if assignment is None:
            return state
        assigned_model = model if model != "auto" else None
        assigned_reasoning = reasoning if reasoning != "auto" else None
        token = secrets.token_hex(4)
        state = replace(
            state,
            assignment_item_id=assignment.first_item_id,
            assignment_token=token,
            assignment_log=(
                *state.assignment_log,
                (token, assignment.first_item_id),
            ),
            assignment_model=assigned_model,
            assignment_reasoning=assigned_reasoning,
            assignment_selected_agent=selected_agent,
            assignment_selected_model=assigned_model,
            assignment_selected_reasoning=assigned_reasoning,
        )
        self.commit(state, snapshot)
        return state

    def _activate_or_handoff(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        *,
        activate: bool = True,
    ) -> tuple[ExecutionState, PlanSnapshot]:
        """Drain one assignment, activate its next agent item, or hand it back.

        Without ``activate`` the assignment ends at its next agent item, which
        waits for the manager to dispatch it as a new assignment.
        """
        assignment_id = state.assignment_item_id
        if assignment_id is None:
            return state, snapshot
        state, snapshot = self.drain(state, snapshot, assignment_id)
        if needs_repair(state):
            return state, snapshot
        if state.status in {"failed", "interrupted", "awaiting_input"}:
            return state, snapshot
        assignment = active_assignment(
            snapshot.plan, assignment_id, runtime=state.workflow_runtime
        )
        if (
            not activate
            or state.status == "completed"
            or assignment is None
            or state.cursor >= assignment.stop
        ):
            # Records an assessment skipped sit right after the assignment's
            # last item; the next dispatch starts beyond them, not on one.
            while (
                state.cursor < len(snapshot.plan.items)
                and state.item_executions[state.cursor].status == "completed"
            ):
                state = advance_completed_item(state, _now)
            state = replace(
                state,
                assignment_item_id=None,
                assignment_token=None,
                assignment_model=None,
                assignment_reasoning=None,
                assignment_selected_agent=None,
                assignment_selected_model=None,
                assignment_selected_reasoning=None,
            )
            self.commit(state, snapshot)
            return state, snapshot
        item = snapshot.plan.items[state.cursor]
        if item.owner != "agent":
            raise StateError("assignment stopped on a non-agent item")
        state = begin_agent_item(
            state,
            snapshot.plan,
            item,
            model=state.assignment_model
            or requested_setting(item.model)
            or state.model,
            reasoning=state.assignment_reasoning
            or requested_setting(item.reasoning)
            or state.reasoning,
            selected_agent=state.assignment_selected_agent,
            selected_model=state.assignment_selected_model,
            selected_reasoning=state.assignment_selected_reasoning,
            change_mark=self._change_mark(state, snapshot.plan, item),
            resolution=self._resolution(state, snapshot.plan, item),
            now=_now,
        )
        self.commit(state, snapshot)
        return state, snapshot

    def _hold_for_verification(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        item: PlanItem,
        check_report: CheckReport | None,
        artifact: str | None,
        request: HeldCompletion,
    ) -> Instruction | None:
        """Hold a completion whose rules still need a verifier.

        Returns ``None`` when nothing is left to verify, and the completion
        is recorded as usual. Otherwise the completion, its change set, and
        its draft artifact are kept on the record, and a verification round
        opens for the rules that need one.
        """
        record = state.item_executions[state.cursor]
        needs = verification_needs(item, record)
        if not needs:
            return None
        directory = self._check_scope(state, snapshot.plan, item).directory
        if check_report is not None:
            mark = check_report.mark
        else:
            mark = take_mark(directory) if record.change_mark else None
        files, unmarked = change_set(directory, record.change_mark, mark)
        held = replace(
            request,
            mark=mark,
            files=files,
            all_files=unmarked,
            draft_ref=self._write_draft(state, item, record, artifact),
            report=check_report,
        )
        state = hold_completion(state, snapshot.plan, held, artifact, _now)
        state, snapshot = open_verification_round(state, snapshot, item, needs, _now)
        self.commit(state, snapshot)
        return replace(self.render(state, snapshot), completion_held=True)

    def _write_draft(
        self,
        state: ExecutionState,
        item: PlanItem,
        record: PlanItemExecution,
        artifact: str | None,
    ) -> str | None:
        """Write the held artifact where the verifiers can read it."""
        if artifact is None:
            return None
        return self.tasks.write_command_output(
            CommandOutputAddress(
                state.task_id,
                state.run_id or state.workflow,
                item.id,
                f"{record.operation_id or item.id}:draft",
                max(1, record.attempts),
                1,
                "stdout",
            ),
            artifact,
        )

    def _dispatch_repair(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        model: str = "auto",
        reasoning: str = "auto",
        selected_agent: str | None = None,
    ) -> Instruction:
        item = snapshot.plan.items[state.cursor]
        if state.active_item_id is not None:
            return self.render(state, snapshot)
        records = list(state.item_executions)
        records[state.cursor] = replace(
            records[state.cursor],
            selected_agent=selected_agent,
            selected_model=model if model != "auto" else None,
            selected_reasoning=reasoning if reasoning != "auto" else None,
        )
        token = secrets.token_hex(4) if state.workflow_runtime == "auto" else None
        state = replace(
            close_assignment(state),
            item_executions=tuple(records),
            status="in_progress",
            active_item_id=item.id,
            assignment_item_id=item.id if token is not None else None,
            assignment_token=token,
            assignment_log=(
                (*state.assignment_log, (token, item.id))
                if token is not None
                else state.assignment_log
            ),
            assignment_model=model
            if model != "auto"
            else requested_setting(item.model) or state.model,
            assignment_reasoning=reasoning
            if reasoning != "auto"
            else requested_setting(item.reasoning) or state.reasoning,
            assignment_selected_agent=selected_agent,
            assignment_selected_model=model if model != "auto" else None,
            assignment_selected_reasoning=reasoning if reasoning != "auto" else None,
            updated_at=_now(),
        )
        self.commit(state, snapshot)
        return self.render(state, snapshot)

    def _complete_repair(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        artifact: str | None,
        summary: str | None,
    ) -> Instruction:
        item = snapshot.plan.items[state.cursor]
        if state.active_item_id != item.id:
            raise StateError("no repair assignment is in progress; use next")
        if artifact is None or not artifact.strip():
            raise StateError(
                "repair completion requires an artifact describing the fix"
            )
        record = state.item_executions[state.cursor]
        reference = self.tasks.write_command_output(
            CommandOutputAddress(
                state.task_id,
                state.run_id,
                item.id,
                f"{record.operation_id or item.id}:repair",
                max(1, record.repair_failures),
                1,
                "stdout",
            ),
            artifact,
        )
        previous_assignment = state
        records = list(state.item_executions)
        records[state.cursor] = replace(
            record,
            repair_artifacts=(*record.repair_artifacts, reference),
            summary_for_next=summary,
        )
        state = replace(state, item_executions=tuple(records))
        state = close_assignment(retry_failed_item(state, snapshot.plan, _now))
        self.commit(state, snapshot)
        state, snapshot = self.drain(state, snapshot)
        if (
            needs_repair(state)
            and state.status != "failed"
            and state.workflow_runtime == "auto"
            and snapshot.plan.items[state.cursor].id == item.id
        ):
            # The same repair worker retains its assignment for another attempt.
            state = replace(
                state,
                status="in_progress",
                active_item_id=item.id,
                assignment_item_id=item.id,
                assignment_token=previous_assignment.assignment_token,
                assignment_model=previous_assignment.assignment_model,
                assignment_reasoning=previous_assignment.assignment_reasoning,
                assignment_selected_agent=previous_assignment.assignment_selected_agent,
                assignment_selected_model=previous_assignment.assignment_selected_model,
                assignment_selected_reasoning=previous_assignment.assignment_selected_reasoning,
            )
            self.commit(state, snapshot)
        return self.render(state, snapshot)

    def _complete_verification(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        item: PlanItem,
        artifact: str | None,
        rule_results: tuple[str, ...],
        *,
        caller_role: CallerRole | None,
    ) -> Instruction:
        """Record a verifier's verdicts, then continue the held step.

        A failing verdict sends the step back to its worker; otherwise the
        next verifier of the round goes on, or ww records the held completion.
        """
        record = state.item_executions[state.cursor]
        target = item.verifies
        assert target is not None
        if not record.verification:
            raise StateError(f"{item.name!r} has no rules to verify in this round")
        if artifact is None or not artifact.strip():
            raise StateError(
                f"artifact is required to complete {item.name!r}: pass your "
                "findings with --artifact"
            )
        plan = snapshot.plan
        index = index_of(plan, target.item_id)
        step = plan.items[index]
        results = parse_rule_results(rule_results, record.verification)
        artifact_reference, _ = write_completion_artifacts(
            self.tasks, state.task_id, state, snapshot, item, None, artifact
        )
        state = complete_agent_item(state, plan, {}, artifact_reference, _now)
        verdicts = verdicts_of(results, item.id)
        state = record_round(state, index, verdicts, _now)
        step_record = state.item_executions[index]
        if any(verdict.verdict == "fail" for verdict in verdicts):
            at_step = replace(close_round(state, plan, step.id, _now), cursor=index)
            held = step_record.held_completion
            assert held is not None
            attempt = max((entry.attempt for entry in item_reports(at_step)), default=0)
            state = reject_completion(
                at_step,
                plan,
                step,
                judged_report(verdicts, attempt + 1, _now(), held),
                step_record.draft_artifact,
                _now,
                keep_active=False,
            )
            self.commit(state, snapshot)
            return self._after_verification(state, snapshot, caller_role)
        if round_open(state, plan, step.id):
            self.commit(state, snapshot)
            return self._after_verification(state, snapshot, caller_role)
        self.commit(state, snapshot)
        return self._replay_held(state.task_id, index, caller_role=caller_role)

    def _after_verification(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        caller_role: CallerRole | None,
    ) -> Instruction:
        """End a verifier's assignment, or open the next agent item without one."""
        if state.status == "failed":
            return self.render(state, snapshot)
        if caller_role is not None:
            state, snapshot = self._activate_or_handoff(state, snapshot)
        else:
            state, snapshot = self.drain(state, snapshot)
        return self.render(state, snapshot)

    def _replay_held(
        self, task_id: str, index: int, *, caller_role: CallerRole | None
    ) -> Instruction:
        """Complete a held step exactly as its worker submitted it.

        The checks run again, reusing results while the tree is unchanged;
        what is still to verify opens another round. A caller with a role
        gets the step's own assignment, so the completion window and the
        automatic items that drain after it are the step worker's; the caller
        is a verifier, never that worker, so the assignment ends before its
        next agent item, which the manager dispatches anew.
        """
        state, snapshot = self.load(task_id)
        record = state.item_executions[index]
        held = record.held_completion
        if held is None:
            raise StateError("the step has no held completion to record")
        state = resume_held(state, snapshot.plan, index, _now)
        self.commit(state, snapshot)
        if caller_role is not None:
            state = self._begin_assignment(
                state,
                snapshot,
                model=record.model or state.model,
                reasoning=record.reasoning or state.reasoning,
                cursor=index,
            )
        return self._complete(
            task_id,
            held.variables,
            record.draft_artifact,
            held.metadata_values,
            selected_agent=held.selected_agent,
            selected_model=held.selected_model,
            selected_reasoning=held.selected_reasoning,
            summary_for_next=held.summary_for_next,
            adjustments=held.adjustments,
            caller_role=caller_role,
            stopping_loop=held.loop_control == "break",
            continuing_loop=held.loop_control == "continue",
            replayed=True,
        )

    def _resolution(
        self, state: ExecutionState, plan: WorkflowPlan, item: PlanItem
    ) -> tuple[tuple[RuleResolution, ...], tuple[PlannedCheck, ...]] | None:
        """How the rules without a command of a beginning step are enforced.

        Read from the store once, when the step first begins; a step that
        began before keeps what it began with. A converted check applies only
        where its configuration files exist in the directory the step's
        checks run in.
        """
        if (
            not judged_rules(item)
            or state.item_executions[state.cursor].rule_resolutions
        ):
            return None
        return resolve_rules(
            item,
            self.rule_store.load(),
            directory=self._check_scope(state, plan, item).directory,
        )

    def _check_scope(
        self, state: ExecutionState, plan: WorkflowPlan, item: PlanItem
    ) -> CheckScope:
        """The directory an item's checks run in and the values they render."""
        workspace, values = self.actions.item_scope(state, plan, item)
        return CheckScope(workspace or self.storage.root, values)

    def _change_mark(
        self, state: ExecutionState, plan: WorkflowPlan, item: PlanItem
    ) -> str | None:
        """The tree a step's change set starts from, taken as it begins.

        A step that already has one keeps it; a step without rules or checks,
        or a directory without git, has none. A step with rules needs it even
        without checks: its verifiers look at what it changed.
        """
        if (
            not (item.checks or item.rules)
            or state.item_executions[state.cursor].change_mark
        ):
            return None
        return take_mark(self._check_scope(state, plan, item).directory)

    def render(self, state: ExecutionState, snapshot: PlanSnapshot) -> Instruction:
        return self.instructions.build(state, snapshot)

    def resume(self, state: ExecutionState, snapshot: PlanSnapshot) -> Instruction:
        """Commit, continue the active assignment or drain, then instruct."""
        self.commit(state, snapshot)
        if state.assignment_item_id is not None:
            state, snapshot = self._activate_or_handoff(state, snapshot)
        else:
            state, snapshot = self.drain(state, snapshot)
        return self.render(state, snapshot)

    @staticmethod
    def _validate_caller_role(caller_role: CallerRole | None) -> None:
        if caller_role is not None and caller_role not in CALLER_ROLES:
            raise StateError("caller role must be 'manager' or 'worker'")

    def _reassign(self, task_id: str) -> Instruction:
        """Close the open assignment's token and issue a new one."""
        validate_task_id(task_id)
        with self.tasks.lock_task(task_id):
            state, snapshot = self.load(task_id)
            if state.assignment_item_id is None:
                raise StateError(
                    f"task {task_id!r} has no open assignment to reassign; "
                    "run next to dispatch one"
                )
            token = secrets.token_hex(4)
            # The old token is dead: its entry moves to the new one.
            log = tuple(
                (token if entry[0] == state.assignment_token else entry[0], entry[1])
                for entry in state.assignment_log
            )
            state = replace(state, assignment_token=token, assignment_log=log)
            self.commit(state, snapshot)
            return self.render(state, snapshot)

    def _authorize_worker(
        self,
        task_id: str,
        caller_role: CallerRole | None,
        assignment: str | None,
        *,
        read: bool = False,
    ) -> None:
        """Refuse a worker command that does not carry the open assignment's token.

        Only the ``auto`` runtime delegates, so only a worker there can outlive
        its assignment. A missing or stale token ends the worker's turn: it
        returns to the manager, who alone can re-issue the token. A ``read``
        while no assignment is open is answered: its page only sends the
        worker back to the manager.
        """
        if caller_role != "worker":
            return
        state, _ = self.load(task_id)
        if state.workflow_runtime != "auto":
            return
        manager = instruction_command(task_id, role="manager")
        if state.assignment_token is None:
            if read:
                return
            raise StateError(
                "no assignment is open: your assignment has ended. Stop here "
                "and return to your manager; run no further ww command."
            )
        if assignment is None:
            raise StateError(
                "a worker command needs --assignment <token>, which the "
                "manager's bootstrap command gives. Stop and return to your "
                f"manager; it gets the token with `{manager}`."
            )
        if assignment != state.assignment_token:
            raise StateError(
                f"assignment {assignment} is not open: your assignment has "
                "ended. Stop here and return to your manager; run no further "
                "ww command."
            )

    def _check_performer(
        self, task_id: str, caller_role: CallerRole | None, *, loop: bool = False
    ) -> None:
        """Refuse a completion by the role that does not perform the open step.

        In ``auto`` the manager performs its own steps (``role: manager``, and
        interactive ones), so a worker never completes one. The manager keeps
        every override on the other steps; ``loop`` stays a worker command
        there, as the pages give it only to the step's worker.
        """
        state, snapshot = self.load(task_id)
        plan = snapshot.plan
        item = (
            plan.items[state.cursor]
            if state.active_item_id is not None and state.cursor < len(plan.items)
            else None
        )
        managers = (
            state.workflow_runtime == "auto"
            and item is not None
            and item.id == state.active_item_id
            and item.role == "manager"
        )
        if caller_role == "worker" and managers:
            assert item is not None
            raise StateError(
                f"this step is the manager's: {item.name!r} is performed by the "
                "manager in its own session, so a worker cannot complete it. "
                "Stop here and return to your manager; run no further ww command."
            )
        if loop and caller_role == "manager" and not managers:
            raise StateError("loop --break/--continue is a worker-role command")

    def _open_assignment(
        self, task_id: str, caller_role: CallerRole | None
    ) -> OpenAssignment | None:
        """The worker's open assignment before its command runs, in ``auto``."""
        if caller_role != "worker":
            return None
        state, snapshot = self.load(task_id)
        if state.workflow_runtime != "auto" or state.assignment_token is None:
            return None
        if needs_repair(state):
            return OpenAssignment(
                state.assignment_token,
                (snapshot.plan.items[state.cursor].id,),
                state.active_item_id,
            )
        assignment = active_assignment(
            snapshot.plan, state.assignment_item_id, runtime=state.workflow_runtime
        )
        if assignment is None:
            return None
        return OpenAssignment(
            state.assignment_token,
            tuple(
                item.id
                for item in snapshot.plan.items[assignment.start : assignment.stop]
                if item.owner == "agent"
            ),
            state.active_item_id,
        )

    def _with_handoff(
        self,
        task_id: str,
        instruction: Instruction,
        opened: OpenAssignment | None,
        *,
        loop: str | None = None,
    ) -> Instruction:
        """Add ww's handoff block when the worker's command ended its turn.

        A ``continue`` resets the round's records into the history, so the
        items of the ended round are read from there.
        """
        if opened is None or instruction.next_role not in {"manager", "operator"}:
            return instruction
        state, snapshot = self.load(task_id)
        block, end_mark = self._handoff_block(state, snapshot, opened, loop=loop)
        if end_mark is not None:
            # Kept so a reprint can reproduce the change set without taking a
            # new mark of a tree that has moved on.
            state = replace(
                state,
                assignment_end_marks=(
                    *state.assignment_end_marks,
                    (opened.token, end_mark),
                ),
            )
            self.runs.commit_run(state, snapshot)
        return replace(instruction, handoff_block=block)

    def _handoff_block(
        self,
        state: ExecutionState,
        snapshot: PlanSnapshot,
        ended: OpenAssignment,
        *,
        loop: str | None = None,
        reprint: bool = False,
    ) -> tuple[HandoffBlock, str | None]:
        """The handoff block of one ended assignment, and its end mark.

        The end mark is the change mark taken when the block was first built
        (None on a reprint or when the assignment had no marked item).

        ``ended`` names its token and agent items. At the moment a worker's
        command ends the assignment, a ``continue`` has reset the round's
        records into the history, so the items of the ended round are read
        from there. A ``reprint`` is built later: an item whose record has
        been reset since is read from the history, and the loop outcome,
        which was known only then, is not reproduced, and the change set is
        reproduced only from the end mark stored with the assignment.
        """
        task_id = state.task_id
        items = {item.id: item for item in snapshot.plan.items}
        current = {record.plan_item_id: record for record in state.item_executions}
        if loop == "continue" or reprint:
            for record in state.execution_history:
                if (
                    loop == "continue"
                    or not current.get(record.plan_item_id, record).attempts
                ):
                    current[record.plan_item_id] = record
        performed = tuple(
            (items[item_id], current[item_id])
            for item_id in ended.items
            if item_id in items and item_id in current
        )
        marked = next(
            ((item, record) for item, record in performed if record.change_mark),
            None,
        )
        files: tuple[str, ...] | None = None
        reproducible = True
        taken: str | None = None
        if marked is not None:
            directory = self._check_scope(state, snapshot.plan, marked[0]).directory
            if reprint:
                end = dict(state.assignment_end_marks).get(ended.token)
            else:
                end = taken = take_mark(directory)
            if end is not None:
                files, _ = change_set(directory, marked[1].change_mark, end)
            elif reprint:
                reproducible = False
        parent_id, _, _ = task_id.rpartition("/")
        continuation = (
            parent_id if parent_id and not run_is_open(state.status) else None
        )
        block = handoff_block(
            task_id,
            ended.token,
            performed,
            root=self.storage.root,
            files=files,
            files_reproducible=reproducible,
            error=(
                state.last_error if state.status in {"failed", "interrupted"} else None
            ),
            loop_outcome=((ended.active, loop) if loop and ended.active else None),
            continuation_task_id=continuation,
        )
        return block, taken

    def handoff(
        self,
        task_id: str,
        run_id: str | None = None,
        assignment: str | None = None,
    ) -> HandoffBlock:
        """Rebuild the handoff block of an ended assignment, read only.

        Without ``assignment`` it is the most recently ended one of the run.
        """
        validate_task_id(task_id)
        state, snapshot = self.load(task_id, run_id)
        log = state.assignment_log
        if assignment is None:
            ended = [
                entry
                for entry in log
                if entry[0] != state.assignment_token or not run_is_open(state.status)
            ]
            if not ended:
                if state.assignment_token is not None:
                    raise StateError(
                        f"assignment {state.assignment_token} has not ended; "
                        "complete or fail its step first"
                    )
                raise StateError(f"task {task_id!r} has no ended assignment")
            token, first = ended[-1]
        else:
            entry = next((entry for entry in log if entry[0] == assignment), None)
            if entry is None:
                raise StateError(
                    f"task {task_id!r} has no assignment {assignment!r}"
                    + (
                        f"; its assignments: {', '.join(t for t, _ in log)}"
                        if log
                        else ""
                    )
                )
            token, first = entry
        if token == state.assignment_token and run_is_open(state.status):
            raise StateError(
                f"assignment {token} has not ended; complete or fail its step first"
            )
        plan = snapshot.plan
        index = next(
            (position for position, item in enumerate(plan.items) if item.id == first),
            None,
        )
        span = (
            assignment_at(plan, index, runtime=state.workflow_runtime)
            if index is not None
            else None
        )
        item_ids = (
            tuple(
                item.id
                for item in plan.items[span.start : span.stop]
                if item.owner == "agent"
            )
            if span is not None
            else ()
        )
        block, _ = self._handoff_block(
            state, snapshot, OpenAssignment(token, item_ids), reprint=True
        )
        return block

    def _require_manager(self, command: str, caller_role: CallerRole | None) -> None:
        self._validate_caller_role(caller_role)
        if caller_role == "worker":
            raise StateError(f"{command} is a manager-role command")

    @staticmethod
    def _tag_caller(
        instruction: Instruction, caller_role: CallerRole | None
    ) -> Instruction:
        return replace(instruction, caller_role=caller_role)

    def load(
        self, task_id: str, run_id: str | None = None
    ) -> tuple[ExecutionState, PlanSnapshot]:
        validate_task_id(task_id)
        return self.runs.load(task_id, run_id)

    def _with_steps(self, state: ExecutionState, plan: WorkflowPlan) -> ExecutionState:
        return project_steps(state, plan, _now)

    def plan_change(self, task_id: str) -> PlanChange | None:
        """How the configuration changed the task's open run, read only."""
        validate_task_id(task_id)
        state, snapshot = self.load(task_id)
        return self._detect_plan_change(state, snapshot)[1]

    def _detect_plan_change(
        self, state: ExecutionState, snapshot: PlanSnapshot
    ) -> tuple[str | None, PlanChange | None]:
        """The current configuration's digest and what it changes in the run.

        Nothing is compiled while the digest matches the one the plan was
        saved under. A configuration that no longer loads, or no longer
        defines the run's workflow, changes nothing here: the run goes on
        with its saved plan, and ``lint`` reports the configuration.
        """
        if not run_is_open(state.status):
            return None, None
        try:
            configuration = self._load_configuration()
            digest = self._configuration_digest(configuration)
            if digest == snapshot.configuration_digest:
                return digest, None
            if state.workflow not in configuration.workflows_by_name:
                return None, None
            template = compile_workflow_plan(
                configuration,
                self.storage.root,
                state.workflow,
                state.agent,
                state.task_id,
                self.extensions,
                PlanCompilationOptions(
                    task_id=state.task_id,
                    completed_bootstrap_step=snapshot.bootstrap_step,
                    project=dict(state.workflow_values).get(PROJECT) or None,
                    modes=state.modes,
                ),
                self.extensions.config,
            )
        except ConfigurationError:
            return None, None
        change = plan_change(state, snapshot, template, digest)
        lane_missing = missing_lane(configuration.workflows_by_name[state.workflow])
        if change is not None and change.refusal is None and lane_missing:
            change = replace(change, refusal=lane_missing)
        return digest, change

    def _plan_gate(
        self, task_id: str, *, replan: bool, keep_plan: bool
    ) -> Instruction | None:
        """Stop ``next`` at a changed plan, or apply the operator's choice.

        Returns the ``plan_changed`` page, or ``None`` for ``next`` to go on.
        A configuration whose change leaves the run's plan as it is is
        adopted silently, so it is not compiled again.
        """
        validate_task_id(task_id)
        with self.tasks.lock_task(task_id):
            state, snapshot = self.load(task_id)
            digest, change = self._detect_plan_change(state, snapshot)
            if digest is not None and digest != snapshot.configuration_digest:
                if change is None or keep_plan:
                    self.commit(*keep_plan_(state, snapshot, digest))
                    return None
                if replan:
                    if change.refusal is not None:
                        raise StateError(f"cannot replan: {change.refusal}")
                    self.commit(*replan_(state, snapshot, change, _now))
                    return None
                return _plan_changed_page(self.render(state, snapshot), change)
        if replan or keep_plan:
            raise StateError(
                "the workflow has not changed since this run's plan was saved; "
                "there is nothing to replan"
            )
        return None

    def _load_configuration(self) -> WorkflowConfiguration:
        """Load any notation through the shared normalized-model contract."""
        return validate_configuration(self.configuration_loader(), self.extensions)

    def _configuration_digest(self, configuration: WorkflowConfiguration) -> str:
        """Fingerprint semantics rather than one frontend's source bytes."""
        value = (configuration, self.extensions.config.builtins)
        return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()

    def _unknown_modes(
        self, mode_names: tuple[str, ...], configuration: WorkflowConfiguration
    ) -> set[str]:
        known = {mode.name for mode in configuration.modes}
        unknown = set()
        for name in mode_names:
            if name in known:
                continue
            if is_extension_reference(name):
                self.extensions.mode(name)
            else:
                unknown.add(name)
        return unknown

    def _task_exists(
        self,
        task_id: str,
        workflow_name: str | None = None,
        project: str | None = None,
    ) -> bool:
        return task_id_claimed(
            task_id,
            tasks=self.tasks,
            extensions=self.extensions,
            workflow_name=workflow_name,
            project=project,
            lane=self._configured_lane(workflow_name),
        )

    def _configured_lane(self, workflow_name: str | None) -> str | None:
        """The lane the configured workflow takes, else its own name.

        A configuration that does not load leaves the name as it is; the
        caller's own work reports that configuration.
        """
        if workflow_name is None:
            return None
        try:
            workflows = self._load_configuration().workflows_by_name
        except ConfigurationError:
            return workflow_name
        workflow = workflows.get(workflow_name)
        return workflow.lane if workflow is not None else workflow_name

    def _generated_child_id(
        self,
        parent_task_id: str,
        children: tuple[ChildTask, ...],
        project: str | None = None,
    ) -> str:
        """Use the ordinary task-ID convention inside a parent namespace.

        A child added with ``--project`` follows that project's task format.
        """
        existing = {child.id for child in children}
        for candidate in candidate_task_ids(self.extensions.task_format(project)):
            validate_child_id(candidate)
            if candidate not in existing and not self._task_exists(
                f"{parent_task_id}/{candidate}"
            ):
                return candidate
        raise StateError(
            f"cannot generate an unused child task ID beneath {parent_task_id!r}"
        )

    @staticmethod
    def _validate_execution_metadata(model: str, reasoning: str) -> None:
        if not model or not reasoning:
            raise StateError("execution requires non-empty --model and --reasoning")

    @staticmethod
    def _validate_selected_agent(agent: str | None) -> None:
        if agent is not None and (not agent.strip() or agent == "auto"):
            raise StateError("selected agent must be non-empty and must not be 'auto'")


def resolve_choice(item: PlanItem, choice: str) -> str:
    """Match an operator's pick to a declared choice by label or number."""
    labels = [option.label for option in item.choices]
    if not labels:
        raise StateError(f"{item.name!r} offers no choices")
    if choice in labels:
        return choice
    if choice.isdigit() and 1 <= int(choice) <= len(labels):
        return labels[int(choice) - 1]
    lowered = {label.lower(): label for label in labels}
    if choice.lower() in lowered:
        return lowered[choice.lower()]
    raise StateError(
        f"{choice!r} is not one of the choices of {item.name!r}: "
        + ", ".join(f"{n}. {label}" for n, label in enumerate(labels, 1))
    )


def _plan_changed_page(instruction: Instruction, change: PlanChange) -> Instruction:
    """The run's page turned into the operator's ``plan_changed`` stop."""
    return replace(
        instruction,
        plan_change=change,
        operator_reason="plan_changed",
        control="awaiting_operator",
        next_role="operator",
    )


def _manager_performs(instruction: Instruction) -> bool:
    """Whether the open item is the manager's own in the ``auto`` runtime."""
    return (
        instruction.workflow_runtime == "auto"
        and instruction.item_status == "in_progress"
        and instruction.status == "in_progress"
        and instruction.role == "manager"
    )


def _collecting(state: ExecutionState, snapshot: PlanSnapshot) -> bool:
    """Whether the run's collection step is the item in progress."""
    if not state.active_item_id or state.cursor >= len(snapshot.plan.items):
        return False
    item = snapshot.plan.items[state.cursor]
    return item.id == state.active_item_id and item.item_operation == "collect"


def _interactive_item(
    state: ExecutionState, snapshot: PlanSnapshot
) -> tuple[PlanItem, PlanItemExecution]:
    """The interactive step in progress whose conversation is still open."""
    if not state.active_item_id or state.cursor >= len(snapshot.plan.items):
        raise StateError("no agent item is in progress; use next")
    item = snapshot.plan.items[state.cursor]
    record = state.item_executions[state.cursor]
    if item.id != state.active_item_id or not item.interactive:
        raise StateError(
            f"the current step {item.name!r} is not interactive; "
            "interact only records an interactive step"
        )
    if record.interaction_ended:
        raise StateError(
            f"the interaction of {item.name!r} has ended; complete the step"
        )
    return item, record


def _run_windows(runs: tuple[TaskRunAggregate, ...]) -> list[RunWindow]:
    """The active window of each run: its start, and its end once closed."""
    windows: list[RunWindow] = []
    for run in runs:
        state = run.state
        try:
            start = datetime.fromisoformat(state.created_at.replace("Z", "+00:00"))
            end = (
                datetime.fromisoformat(state.updated_at.replace("Z", "+00:00"))
                if state.status in CLOSED_RUN_STATUSES
                else None
            )
        except ValueError:
            continue
        windows.append((start, end))
    return windows


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _render_template(template: str, values: dict[str, str]) -> str:
    missing = set(dependencies(template)) - set(values)
    if missing:
        raise StateError(
            "automatic handler is missing variable(s): " + ", ".join(sorted(missing))
        )
    return interpolate(template, values)


def _child_fields(fields: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """Validate ``--field`` values for a child, as for an item."""
    try:
        return validate_item_fields(dict(fields))
    except ValueError as error:
        raise StateError(str(error).replace("item field", "child field")) from error


def _exiting_children(state: ExecutionState, plan: WorkflowPlan) -> bool:
    """Whether a pending break ends the per-child stages."""
    stopped = next(
        (item for item in plan.items if item.id == state.loop_exit_item_id), None
    )
    return stopped is not None and stopped.breaks_children


def _index_for_id(plan: WorkflowPlan, item_id: str) -> int:
    for index, item in enumerate(plan.items):
        if item.id == item_id:
            return index
    raise StateError(f"plan item {item_id!r} is not in the task snapshot")
