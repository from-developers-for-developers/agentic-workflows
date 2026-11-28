# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive a fully automatic plan through the ordinary task lifecycle."""

from __future__ import annotations

from ww.config import load_configuration
from ww.control import child_workflow, workflow_transition
from ww.errors import StateError, WwError
from ww.executable import ww_command
from ww.extensions import ExtensionRegistry
from ww.instructions import Instruction
from ww.interpolation import dependencies
from ww.plan import (
    PlanCompilationOptions,
    PlanItem,
    WorkflowPlan,
    compile_workflow_plan,
)
from ww.service import WorkflowService
from ww.storage import Storage
from ww.workflow_config import INIT_STEP_NAME, binds_task_identity

TERMINAL_AGENT = "custom:terminal-invoked"


def _refuse(item: PlanItem, reason: str) -> StateError:
    return StateError(
        f"{item.workflow}/{item.step} ({item.phase}, {item.name}): {reason}. "
        f"Use {ww_command()} start for this workflow."
    )


def _validate_item(item: PlanItem) -> None:
    if item.verification_plan or item.assessment_question or item.selects_rules_for:
        raise _refuse(item, "requires an assessment or rule verdict")
    if item.on_failure == "fix":
        raise _refuse(item, "its failure handler requires agent repair")
    if item.summary and item.source == "internal":
        if tuple(value.name for value in item.provide) != ("summary",):
            raise _refuse(
                item, "the configured workflow report requires agent feedback"
            )
        if item.requested_run == "headless":
            raise _refuse(item, "the summary requests a headless agent")
        return
    if item.phase == "step" and item.name == item.step == INIT_STEP_NAME:
        return
    if item.requires_agent_input:
        raise _refuse(
            item, "requires input for " + ", ".join(v.name for v in item.provide)
        )
    if item.owner != "ww" or item.execution != "automatic" or child_workflow(item):
        raise _refuse(item, "requires an agent or operator")


def run_terminal(
    storage: Storage,
    extensions: ExtensionRegistry,
    workflow: str,
    modes: tuple[str, ...],
    project: str | None,
    requirements: str | None,
    description: str | None,
) -> Instruction:
    configuration = load_configuration(storage.config_path, extensions)
    declared = configuration.workflows_by_name.get(workflow)
    if declared and binds_task_identity(declared):
        raise StateError(
            f"{workflow}/{declared.steps[0].name} requires an agent to supply "
            "the task ID. "
            f"Use {ww_command()} start for this workflow."
        )

    def validate(plan: WorkflowPlan) -> None:
        """Check the actual plan before init, including every static successor."""
        visited = {plan.workflow}
        while True:
            target = None
            for item in plan.items:
                transition = workflow_transition(item)
                if transition is None:
                    _validate_item(item)
                    continue
                target = transition.target
                if len(visited) > 1:
                    raise _refuse(item, "a task can hand off only once")
                if dependencies(target) or target in visited:
                    raise _refuse(
                        item, "cannot prove the handoff target is fully automated"
                    )
            if target is None:
                return
            visited.add(target)
            plan = compile_workflow_plan(
                configuration,
                storage.root,
                target,
                TERMINAL_AGENT,
                plan.task_id,
                extensions,
                PlanCompilationOptions(
                    task_id=plan.task_id, project=project, modes=plan.modes
                ),
                extensions.config,
            )

    service = WorkflowService(
        storage,
        extensions=extensions,
        configuration_loader=lambda: configuration,
        validate_start_plan=validate,
    )
    page = service.start(
        workflow,
        None,
        modes,
        TERMINAL_AGENT,
        workflow_runtime="single",
        init_artifact=requirements
        if requirements is not None
        else f"Run workflow {workflow}.",
        description=description if description is not None else workflow[:120],
        project=project,
    )
    task_id = page.task_id
    try:
        while page.status == "pending" and page.control != "awaiting_operator":
            page = service.next(task_id, caller_role="manager")
        if page.status == "in_progress" and page.control != "awaiting_operator":
            state, snapshot = service.load(task_id)
            item = snapshot.plan.items[state.cursor]
            if item.summary and item.source == "internal" and item.id == page.item_id:
                results = [
                    f"- {step.step} ({step.name}): {record.status}"
                    for step, record in zip(
                        snapshot.plan.items, state.item_executions, strict=True
                    )
                    if step.owner == "ww"
                ]
                summary = f"Ran {state.workflow} from the terminal."
                page = service.complete(
                    task_id,
                    (("summary", summary),),
                    artifact="\n".join((summary, *results)),
                )
        return page
    except WwError as error:
        raise StateError(
            f"{task_id}: {error}. Inspect the current step with "
            f"{ww_command()} instruction {task_id}."
        ) from error
