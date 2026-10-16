# SPDX-License-Identifier: GPL-3.0-or-later
"""The Tasks tab: the task store as the page shows it, read through the core.

Tasks, runs, metadata and direct work come from the task store adapter;
which tasks cannot be read, from ``unreadable_tasks``; why a run waits for
the operator, from the instruction policy; a step's rules, from the same
``rule_outcomes`` its artifact's ``## Rules`` section is written from; and
artifacts, from the service's listing, the only files the page will serve.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ww.completion_artifacts import rule_outcomes
from ww.direct_work import task_commits
from ww.errors import StateError, WwError
from ww.execution_models import ExecutionState, PlanItemExecution, TaskRunAggregate
from ww.instructions.policy import operator_reason
from ww.open_work import unreadable_tasks
from ww.operations import ChildWorkflowRun, WorkflowHandoff
from ww.plan import PlanItem, WorkflowPlan
from ww.rule_checks import item_reports
from ww.rule_verification import recorded_verdicts
from ww.service import WorkflowService
from ww.storage_adapters.base import TaskStorageAdapter

from .project import Project

JSON = dict[str, Any]


def task_list(project: Project) -> JSON:
    """Every task, children after their parent, unreadable ones with the reason."""
    tasks = project.storage.task_persistence
    unreadable = {entry.task_id: entry.reason for entry in unreadable_tasks(tasks)}
    listed: list[JSON] = []
    for task_id in tasks.task_ids():
        for candidate in (task_id, *tasks.child_task_ids(task_id)):
            if candidate in unreadable:
                listed.append(
                    {
                        "id": candidate,
                        "status": "unreadable",
                        "summary": unreadable[candidate],
                    }
                )
            else:
                listed.append(_summary(tasks, candidate))
    return {"tasks": listed}


def _summary(tasks: TaskStorageAdapter, task_id: str) -> JSON:
    runs, _, _ = tasks.read_task_record(task_id)
    ledger = {run.run_id: run for run in tasks.summarize_runs(runs)}
    direct = tasks.read_direct_work(task_id)
    last = runs[-1] if runs else None
    status = _run_status(last) if last else "direct"
    summary = ledger[last.run_id].summary if last else None
    if last and not summary:
        summary = next(
            (
                record.summary_for_next
                for record in reversed(last.state.item_executions)
                if record.summary_for_next
            ),
            None,
        )
    if not summary and direct:
        summary = direct[-1].summary
    times = [run.state.updated_at for run in runs] + [
        entry.recorded_at for entry in direct
    ]
    return {
        "id": task_id,
        "status": status,
        "workflows": [run.workflow for run in runs],
        "summary": summary or "",
        "updated_at": max(times) if times else None,
        "current_step": _current_step(last) if last and status != "completed" else None,
        "direct_work": len(direct),
    }


def _run_status(run: TaskRunAggregate) -> str:
    """The run's status, with a stop for the operator called that."""
    if operator_reason(run.state, run.snapshot.plan) is not None:
        return "awaiting_operator"
    return run.state.status


def _current_step(run: TaskRunAggregate) -> str | None:
    state, plan = run.state, run.snapshot.plan
    if state.cursor >= len(plan.items):
        return None
    return plan.items[state.cursor].step


def task_detail(project: Project, task_id: str) -> JSON:
    """One task with every run, its items, their rules and their artifacts."""
    tasks = project.storage.task_persistence
    try:
        runs, handoff, _ = tasks.read_task_record(task_id)
    except StateError as error:
        return {"id": task_id, "status": "unreadable", "unreadable": str(error)}
    metadata = tasks.read_task_metadata(task_id)
    direct = tasks.read_direct_work(task_id)
    config = project.extensions.config if project.extensions else None
    ledger = {run.run_id: run for run in tasks.summarize_runs(runs)}
    service = project.service()
    detail_runs = []
    for run in runs:
        try:
            artifacts = service.artifacts(task_id, run.run_id)
        except (WwError, ValueError):
            artifacts = ()
        detail_runs.append(_run(project, run, ledger[run.run_id].summary, artifacts))
    last = runs[-1] if runs else None
    return {
        "id": task_id,
        "status": _run_status(last) if last else "direct",
        "handoff": handoff,
        "metadata": dict(metadata.values) if metadata else {},
        "direct_work": [entry.to_dict() for entry in direct],
        "values": _namespace_values(project, last) if last else {},
        "commits": [
            commit.to_dict()
            for commit in (
                task_commits(project.storage.root, config, task_id, direct)
                if config is not None
                else ()
            )
        ],
        "documents": _documents(service, task_id),
        "children": list(tasks.child_task_ids(task_id)),
        "runs": detail_runs,
    }


def _documents(service: WorkflowService, task_id: str) -> list[JSON]:
    """The task's own documents that have a file, with their last update.

    Project and user documents are shared by every task; the library lists them.
    """
    try:
        listing = service.documents_listing(task_id)
    except (WwError, OSError, ValueError):
        return []
    return [
        {key: entry[key] for key in ("name", "description", "scope", "last_update")}
        for entry in listing
        if entry["exists"] and entry["scope"] == "task"
    ]


def document_text(project: Project, task_id: str | None, name: str) -> str:
    """A declared document's text, where the service lists its file.

    Without a task only project and user documents are listed.
    """
    for entry in project.service().documents_listing(task_id):
        if entry["name"] == name and entry["exists"]:
            return Path(str(entry["path"])).read_text(
                encoding="utf-8", errors="replace"
            )
    owner = task_id or "the project"
    raise StateError(f"{owner} has no document {name!r} with a file")


def _namespace_values(project: Project, run: TaskRunAggregate) -> dict[str, str]:
    """What the configured extensions know about the task, such as its branch."""
    extensions = project.extensions
    if extensions is None:
        return {}
    state = run.state
    try:
        return extensions.namespace_values(
            extensions.namespace_variables(),
            task_id=state.task_id,
            run_id=state.run_id,
            workflow=state.workflow,
            workflow_values=dict(state.workflow_values),
            workspace=Path(state.working_directory)
            if state.working_directory
            else None,
            project=None,
            lane=run.snapshot.plan.hooks_from,
        )
    except (WwError, OSError, ValueError):
        return {}


def _run(
    project: Project,
    run: TaskRunAggregate,
    summary: str | None,
    artifacts: tuple[dict[str, str], ...],
) -> JSON:
    state, plan = run.state, run.snapshot.plan
    references = {entry["artifact"] for entry in artifacts if "artifact" in entry}
    # Each assessment's answer: only the outcome it chose ran, or will run.
    chosen = {
        item.step: record.assessment_outcome
        for item, record in zip(plan.items, state.item_executions, strict=True)
        if item.assessment_question
    }
    # The containers an item sits in that the page shows as rows: steps and
    # plain groups, not an assessment's outcome or a child's stage slot.
    outcomes = {
        f"{item.step}/{label}"
        for item in plan.items
        for label in (*item.assessment_outcomes, "positive", "negative", "mixed")
        if item.assessment_question
    }
    items = []
    previous: PlanItem | None = None
    for index, (item, record) in enumerate(
        zip(plan.items, state.item_executions, strict=True)
    ):
        if item.verifies is not None and record.status == "pending":
            continue
        if (
            item.assessment_parent is not None
            and chosen.get(item.assessment_parent) != item.assessment_outcome
            and _never_ran(record)
        ):
            continue
        depth = sum(
            1
            for ancestor in item.ancestors
            if not ancestor.endswith(("/{child}", "/{item}"))
            and ancestor not in outcomes
        )
        # A step's own hooks and verifiers sit under it, and so does every
        # member of a handler group after its first.
        if (
            item.phase in {"before_start", "before_complete", "after_complete"}
            or item.verifies is not None
            or (
                previous is not None
                and previous.phase == "step"
                and item.phase == "step"
                and previous.step == item.step
            )
        ):
            depth += 1
        previous = item
        items.append(
            {**_item(state, plan, index, item, record, references), "depth": depth}
        )
    reason = operator_reason(state, plan)
    return {
        "id": run.run_id,
        "workflow": run.workflow,
        "status": _run_status(run),
        "operator_reason": reason,
        "last_error": state.last_error if reason else None,
        "agent": state.agent,
        "runtime": state.workflow_runtime,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "config_ref": state.config_ref,
        "config_commit": state.config_commit,
        "modes": list(state.modes),
        "summary": summary,
        "cursor": state.cursor,
        "gotos": [goto.to_dict() for goto in state.gotos],
        "items": items,
        "history": [
            _history(plan, record)
            for record in state.execution_history
            if record.status in {"completed", "failed"}
        ],
        # Repair results and command output, which no item row shows.
        "extra_artifacts": [entry for entry in artifacts if "artifact" not in entry],
    }


def _never_ran(record: PlanItemExecution) -> bool:
    """Not run: still pending, or passed over when another outcome was chosen.

    A repeating assessment may run an outcome in an earlier round and choose
    another last; what ran stays on the page.
    """
    return record.status == "pending" or (record.result or "").startswith("skipped:")


def _item(
    state: ExecutionState,
    plan: WorkflowPlan,
    index: int,
    item: PlanItem,
    record: PlanItemExecution,
    references: set[str],
) -> JSON:
    return {
        "name": item.name,
        "path": item.step,
        "phase": item.phase,
        "owner": item.owner,
        "kind": _kind(item),
        "builtin": item.source == "internal" or item.step == "init",
        "verifies": _target_step(plan, item),
        "status": record.status,
        "current": index == state.cursor
        and state.status not in {"completed", "abandoned"},
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "attempts": record.attempts,
        "model": record.selected_model or record.model,
        "agent": record.selected_agent,
        "result": record.result,
        "error": record.error,
        "summary": record.summary_for_next,
        "outcome": record.assessment_outcome,
        "chosen": record.chosen,
        "round": record.goto_round or record.repeat_round or 0,
        "artifact": record.artifact if record.artifact in references else None,
        "draft_artifact": record.draft_artifact,
        "rules": _rules(state, plan, index, item, record),
    }


def _kind(item: PlanItem) -> str:
    if isinstance(item.operation, ChildWorkflowRun):
        return "workflow"
    if isinstance(item.operation, WorkflowHandoff):
        return "handoff"
    if item.verifies is not None:
        return "verification"
    if item.assessment_question:
        return "assess"
    return "ww" if item.owner == "ww" else "agent"


def _target_step(plan: WorkflowPlan, item: PlanItem) -> str | None:
    if item.verifies is None:
        return None
    target = next(
        (entry for entry in plan.items if entry.id == item.verifies.item_id), None
    )
    return target.step if target else None


def _rules(
    state: ExecutionState,
    plan: WorkflowPlan,
    index: int,
    item: PlanItem,
    record: PlanItemExecution,
) -> JSON | None:
    """How the step's rules and checks worked, as its artifact reports them.

    A step that has not been checked yet reports nothing but what applies.
    """
    if item.verifies is not None:
        return {
            "verifies": [rule.to_dict() for rule in record.verification],
            "results": list(record.verification_results),
        }
    if not item.rules and not item.checks:
        return None
    applied = [
        {"id": rule.id, "summary": rule.summary, "checked": rule.has_command}
        for rule in item.rules
    ] + [
        {"id": check.id, "summary": check.summary, "checked": True, "hook": True}
        for check in item.checks
        if check.source == "hook"
    ]
    reports = item_reports(state, index)
    checked = bool(reports) or record.status == "completed"
    summary = (
        rule_outcomes(
            state,
            item,
            None,
            index=index,
            verdicts=recorded_verdicts(plan, state, index),
        )
        if checked
        else None
    )
    return {
        "applied": applied,
        "outcomes": [
            {
                "id": outcome.id,
                "status": outcome.status,
                "hook": outcome.hook,
                "detail": outcome.detail,
            }
            for outcome in summary.outcomes
        ]
        if summary
        else [],
        "fix_attempts": summary.fix_attempts if summary else 0,
        "waived": [
            {"id": check, "reason": reason} for check, reason in record.checks_waived
        ],
        "reports": [report.to_dict() for report in reports],
        "disputes": [dispute.to_dict() for dispute in record.disputes],
        "resolutions": [entry.to_dict() for entry in record.rule_resolutions],
        "not_applicable": list(record.rules_not_applicable),
        "left_out": list(record.rules_left_out),
    }


def _history(plan: WorkflowPlan, record: PlanItemExecution) -> JSON:
    """An earlier round of an item, kept when a goto or retry started it over."""
    item = next(
        (entry for entry in plan.items if entry.id == record.plan_item_id), None
    )
    return {
        "name": item.name if item else record.plan_item_id,
        "path": item.step if item else None,
        "status": record.status,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "round": record.goto_round or record.repeat_round or 0,
        "result": record.result,
        "summary": record.summary_for_next,
        "artifact": record.artifact,
    }


def artifact_text(project: Project, task_id: str, run_id: str, reference: str) -> str:
    """An artifact's text, when the run records it.

    The page may ask only for what the service lists for the run, or for an
    earlier round's artifact a goto or retry kept in the run's history, so it
    cannot be asked for any other file.
    """
    service = project.service()
    listed = {
        entry[key]: entry["path"]
        for entry in service.artifacts(task_id, run_id)
        for key in ("artifact", "repair_artifact", "command_output")
        if key in entry
    }
    if reference not in listed:
        state, _ = service.load(task_id, run_id)
        earlier = {
            ref
            for record in state.execution_history
            for ref in (record.artifact, record.draft_artifact)
            if ref is not None
        }
        if reference not in earlier:
            raise StateError(
                f"{reference!r} is not an artifact of {task_id} run {run_id}"
            )
        listed[reference] = str(project.storage.root / reference)
    return Path(listed[reference]).read_text(encoding="utf-8", errors="replace")
