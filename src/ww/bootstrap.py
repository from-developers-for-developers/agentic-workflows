# SPDX-License-Identifier: GPL-3.0-or-later
"""Bootstrap external task IDs before a workflow run exists."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

from ww.artifacts import render_step_artifact
from ww.completion_inputs import validate_requested_values, validate_values
from ww.errors import ConfigurationError, StateError
from ww.extensions import ExtensionRegistry
from ww.instructions import Instruction, action_text, build_bootstrap_instruction
from ww.plan import PlanCompilationOptions, PlanItem, compile_workflow_plan
from ww.storage import Storage
from ww.storage_adapters import ArtifactAddress, TaskStorageAdapter
from ww.task_ids import validate_child_id
from ww.workflow_config import ProvidedVariable, WorkflowConfiguration


class StartBootstrapRun(Protocol):
    """Start the normal workflow run created by a resolved bootstrap request."""

    def __call__(
        self,
        task_id: str,
        workflow_name: str,
        mode_names: tuple[str, ...],
        agent: str,
        *,
        bootstrap_step: str,
        bootstrap_values: tuple[tuple[str, str], ...],
        bootstrap_request_id: str,
        model: str,
        reasoning: str,
        workflow_runtime: str,
        branch_naming_strategy: str | None,
        init_artifact: str,
        project: str | None,
        parent_task_id: str | None = None,
        start_operation_id: str | None = None,
    ) -> Instruction: ...


class BindChild(Protocol):
    """Rename a parent's child record once the child's external ID is known."""

    def __call__(
        self, parent_task_id: str, temporary_id: str, child_task_id: str
    ) -> None: ...


class ReadWorkflowStatus(Protocol):
    """Read the instruction for an existing workflow run."""

    def __call__(self, task_id: str, run_id: str | None) -> Instruction: ...


class BootstrapCoordinator:
    """Own bootstrap-request persistence and external-ID binding.

    The service supplies the two workflow lifecycle operations at the binding
    boundary. This keeps task locks and the transition into a normal workflow
    run explicit without coupling this coordinator to ``WorkflowService``.
    """

    def __init__(
        self,
        storage: Storage,
        tasks: TaskStorageAdapter,
        extensions: ExtensionRegistry,
        *,
        now: Callable[[], str],
        generated_request_id: Callable[[], str],
        validate_task_id: Callable[[str], None],
        task_exists: Callable[[str], bool],
    ) -> None:
        self.storage = storage
        self.tasks = tasks
        self.extensions = extensions
        self.now = now
        self.generated_request_id = generated_request_id
        self.validate_task_id = validate_task_id
        self.task_exists = task_exists

    def step(
        self,
        configuration: WorkflowConfiguration,
        workflow_name: str,
        mode_names: tuple[str, ...],
        agent: str,
        unknown_modes_for: Callable[[tuple[str, ...], WorkflowConfiguration], set[str]],
        project: str | None = None,
    ) -> PlanItem | None:
        """Return the first agent step that establishes an external task ID."""
        if workflow_name not in configuration.workflows_by_name:
            raise ConfigurationError(f"workflow not found: {workflow_name}")
        workflow = configuration.workflows_by_name[workflow_name]
        unknown_modes = unknown_modes_for(mode_names, configuration)
        if unknown_modes:
            raise StateError("unknown mode(s): " + ", ".join(sorted(unknown_modes)))
        providers = [
            (index, step)
            for index, step in enumerate(workflow.steps)
            if any(value.name == "task_id" for value in step.provide)
        ]
        if not providers:
            return None
        if len(providers) != 1 or providers[0][0] != 0:
            raise ConfigurationError(
                "provide: task_id is only supported on the first workflow step "
                "when start has no task ID"
            )
        step = providers[0][1]
        if (
            step.hooks
            or step.child_steps
            or step.loop_steps
            or step.items is not None
            or step.children is not None
        ):
            raise ConfigurationError(
                "the bootstrap task_id step cannot have hooks, nested steps, "
                "or children"
            )
        plan = compile_workflow_plan(
            configuration,
            self.storage.root,
            workflow_name,
            agent,
            None,
            self.extensions,
            PlanCompilationOptions(project=project, modes=mode_names or None),
            self.extensions.config,
        )
        item = next(
            (
                entry
                for entry in plan.items
                if entry.phase == "step" and entry.step == step.name
            ),
            None,
        )
        if item is None or item.owner != "agent":
            raise ConfigurationError("the bootstrap task_id step must be agent-owned")
        if tuple(value.name for value in item.provide) != ("task_id",):
            raise ConfigurationError(
                "the bootstrap task_id step must provide exactly task_id"
            )
        return item

    def start(
        self,
        workflow_name: str,
        mode_names: tuple[str, ...],
        agent: str,
        item: PlanItem,
        model: str,
        reasoning: str,
        workflow_runtime: str,
        branch_naming_strategy: str | None,
        init_artifact: str,
        project: str | None = None,
        *,
        request_id: str | None = None,
        parent_task_id: str | None = None,
        start_operation_id: str | None = None,
    ) -> Instruction:
        """Record an identity request; a child's request reuses its temporary ID."""
        explicit = request_id is not None
        request_id = request_id or self.generated_request_id()
        request: dict[str, object] = {
            "request_id": request_id,
            "workflow": workflow_name,
            "modes": list(mode_names),
            "agent": agent,
            "model": model,
            "reasoning": reasoning,
            "requested_agent": item.requested_agent,
            "requested_model": item.requested_model,
            "requested_reasoning": item.requested_reasoning,
            "requested_profile": item.profile,
            "workflow_runtime": workflow_runtime,
            "branch_naming_strategy": branch_naming_strategy,
            "init_artifact": init_artifact,
            "project": project,
            "parent_task_id": parent_task_id,
            "start_operation_id": start_operation_id,
            "step": item.step,
            "item_id": item.id,
            "item_name": item.name,
            "action_kind": item.kind,
            "action_text": action_text(item),
            "profile_instruction": item.profile_instruction,
            "profile_path": item.profile_path,
            "step_modes": [mode.to_dict() for mode in item.modes],
            "status": "pending",
            "created_at": self.now(),
        }
        with self.storage.lock_project():
            while self.storage.read_bootstrap(request_id) is not None:
                if explicit:
                    raise StateError(f"identity request {request_id!r} already exists")
                request_id = self.generated_request_id()
                request["request_id"] = request_id
            self.storage.write_bootstrap(request_id, request)
        return self.instruction(request)

    def next(
        self,
        request: dict[str, object],
        force: bool,
        *,
        selected_agent: str | None,
        selected_model: str | None,
        selected_reasoning: str | None,
        start_task: StartBootstrapRun,
        status_task: ReadWorkflowStatus,
        bind_child: BindChild | None = None,
    ) -> Instruction:
        status = request.get("status")
        if status == "completed":
            raise StateError("bootstrap request is already completed")
        if status == "binding":
            resolved = request.get("resolved_task_id")
            if isinstance(resolved, str) and resolved:
                return self.complete(
                    str(request["request_id"]),
                    request,
                    (),
                    None,
                    start_task=start_task,
                    status_task=status_task,
                    bind_child=bind_child,
                )
            raise StateError(
                "binding bootstrap request is missing its resolved task ID"
            )
        if status == "failed":
            if not force:
                return self.instruction(request)
            request["status"] = "pending"
            request.pop("error", None)
        if request.get("status") == "in_progress":
            raise StateError("a bootstrap step is already in progress; use complete")
        request["status"] = "in_progress"
        request["selected_agent"] = selected_agent
        request["selected_model"] = selected_model
        request["selected_reasoning"] = selected_reasoning
        self.storage.write_bootstrap(str(request["request_id"]), request)
        return self.instruction(request)

    def complete(
        self,
        request_id: str,
        request: dict[str, object],
        variables: tuple[tuple[str, str], ...],
        artifact: str | None,
        *,
        start_task: StartBootstrapRun,
        status_task: ReadWorkflowStatus,
        bind_child: BindChild | None = None,
    ) -> Instruction:
        """Serialize a bootstrap binding and refresh stale request state."""
        request_path = self.storage.runtime_path / "bootstrap" / f"{request_id}.json"
        with self.storage.locks.lock(
            request_path, purpose=f"bootstrap request {request_id!r}"
        ):
            current = self.storage.read_bootstrap(request_id)
            if current is not None:
                request = current
            return self._complete_locked(
                request_id,
                request,
                variables,
                artifact,
                start_task=start_task,
                status_task=status_task,
                bind_child=bind_child,
            )

    def _complete_locked(
        self,
        request_id: str,
        request: dict[str, object],
        variables: tuple[tuple[str, str], ...],
        artifact: str | None,
        *,
        start_task: StartBootstrapRun,
        status_task: ReadWorkflowStatus,
        bind_child: BindChild | None = None,
    ) -> Instruction:
        if request.get("status") not in {"in_progress", "binding"}:
            raise StateError("no bootstrap step is in progress; use next")
        supplied = validate_values(variables)
        parent_task_id = request.get("parent_task_id")
        parent = str(parent_task_id) if isinstance(parent_task_id, str) else None
        if request.get("status") == "binding":
            resolved_id = str(request.get("resolved_task_id", ""))
            if supplied:
                validate_requested_values(supplied, (ProvidedVariable("task_id"),))
        else:
            validate_requested_values(supplied, (ProvidedVariable("task_id"),))
            resolved_id = supplied["task_id"]
            if parent is not None:
                # A child's external ID is one segment beneath its parent.
                validate_child_id(resolved_id)
                resolved_id = f"{parent}/{resolved_id}"
        self.validate_task_id(resolved_id)
        if request.get("status") != "binding" and self.task_exists(resolved_id):
            raise StateError(
                f"task {resolved_id!r} already exists and cannot be rebound"
            )
        workflow = str(request["workflow"])
        modes = tuple(str(value) for value in cast(list[object], request["modes"]))
        agent = str(request["agent"])
        step = str(request["step"])
        request["status"] = "binding"
        request["resolved_task_id"] = resolved_id
        request["binding_started_at"] = self.now()
        if artifact is not None:
            request["binding_artifact"] = artifact
        self.storage.write_bootstrap(request_id, request)
        with self.tasks.lock_task(resolved_id):
            if self.task_exists(resolved_id):
                runs, _ = self.tasks.read_task_aggregate(resolved_id)
                if not any(run.bootstrap_request_id == request_id for run in runs):
                    raise StateError(
                        f"task {resolved_id!r} exists but is not bound to "
                        "bootstrap request "
                        f"{request_id!r}"
                    )
                instruction = status_task(resolved_id, None)
            else:
                instruction = start_task(
                    resolved_id,
                    workflow,
                    modes,
                    agent,
                    bootstrap_step=step,
                    bootstrap_values=(("task_id", resolved_id),),
                    bootstrap_request_id=request_id,
                    model=str(request.get("model", "auto")),
                    reasoning=str(request.get("reasoning", "auto")),
                    workflow_runtime=str(request.get("workflow_runtime", "single")),
                    branch_naming_strategy=(
                        str(request["branch_naming_strategy"])
                        if request.get("branch_naming_strategy") is not None
                        else None
                    ),
                    init_artifact=str(request["init_artifact"]),
                    project=(
                        str(request["project"])
                        if request.get("project") is not None
                        else None
                    ),
                    parent_task_id=parent,
                    start_operation_id=(
                        str(request["start_operation_id"])
                        if request.get("start_operation_id") is not None
                        else None
                    ),
                )
            artifact = artifact or (
                str(request["binding_artifact"])
                if request.get("binding_artifact") is not None
                else None
            )
            if artifact is not None:
                run_id = self.tasks.active_execution_run(resolved_id)
                if run_id is not None:
                    self.tasks.write_execution_artifact(
                        ArtifactAddress(
                            resolved_id,
                            workflow,
                            step,
                            2,
                            str(request["item_name"]),
                            "step",
                            run_id=run_id,
                            step_ordinals=(2,),
                        ),
                        render_step_artifact(
                            task_id=resolved_id,
                            workflow=workflow,
                            step=step,
                            step_number=1,
                            step_total=1,
                            skill="auto",
                            result=artifact,
                        ),
                    )
        if parent is not None:
            if bind_child is None:  # pragma: no cover - service always binds
                raise StateError("child identity requests require a parent binding")
            bind_child(parent, request_id, resolved_id)
        request["status"] = "completed"
        request["completed_at"] = self.now()
        self.storage.write_bootstrap(request_id, request)
        return instruction

    def instruction(self, request: dict[str, object]) -> Instruction:
        return build_bootstrap_instruction(request, self.storage.root)
