# SPDX-License-Identifier: GPL-3.0-or-later
"""Feature maps in a run: what its reading steps reported, what a step that
writes them must answer for, and the task lists it leaves behind.

A ``write_feature_maps`` step answers for the run's change set: every file
changed in its workspace since the run's branch forked from its ww/git base
branch (or since ``HEAD`` without one). In a workflow without one, every agent
step answers for its own change set, since the mark taken as it began. The
maps' own files never count. A step's page and its completion both read what
is due from here, so they always agree.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ww.builtin_workflows import builtin_workflow_names
from ww.changes import take_mark, touched_files
from ww.config_snapshots import GIT_EXTENSION
from ww.executable import ww_command
from ww.execution_models import ExecutionState
from ww.features import (
    FEATURE_MAPS_HEADING,
    MapsDue,
    MapsReport,
    changed_paths,
    maps_due,
    read_maps_answers,
    read_maps_report,
    record_task,
)
from ww.plan import PlanItem, WorkflowPlan
from ww.project_config import ProjectConfig
from ww.workspace import resolve_workspace


def run_base_branch(config: ProjectConfig, plan: WorkflowPlan) -> str | None:
    """The branch a run's work forks from: the ww/git ``base_branches`` entry
    of its lane, else the default one."""
    bases = config.settings_for(GIT_EXTENSION).get("base_branches")
    return next(
        (
            value
            for key in (plan.lane, "default")
            if isinstance(bases, dict)
            and isinstance(value := bases.get(key), str)
            and value
        ),
        None,
    )


def maps_reports(
    state: ExecutionState, plan: WorkflowPlan, read_artifact: Callable[[str], str]
) -> tuple[tuple[str, MapsReport], ...]:
    """What the run's completed reading steps reported, by step."""
    items = {item.id: item for item in plan.items}
    reports: list[tuple[str, MapsReport]] = []
    for record in (*state.execution_history, *state.item_executions):
        item = items.get(record.plan_item_id)
        if (
            item is None
            or not item.read_feature_maps
            or record.status != "completed"
            or not record.artifact
        ):
            continue
        report = read_maps_report(read_artifact(record.artifact))
        if report is not None:
            reports.append((item.step, report))
    return tuple(reports)


def writes_maps(
    plan: WorkflowPlan, item: PlanItem, config: ProjectConfig
) -> Literal["run", "step"] | None:
    """Which changes ``item`` brings the feature maps up to date with.

    ``run``: a ``write_feature_maps`` step answers for the whole run's. ``step``:
    in a writable workflow of the project's with no such step, every step an
    agent performs answers for its own, so the maps never fall behind. ww's
    own built-in workflows change its configuration, not the project's
    features, so they answer only through a writing step. ``None`` otherwise,
    and always while the project keeps no feature maps.
    """
    if not config.feature_maps or item.owner != "agent" or item.phase != "step":
        return None
    if item.write_feature_maps:
        return "run"
    if item.verifies is not None or not falls_back(plan):
        return None
    return "step"


def falls_back(plan: WorkflowPlan) -> bool:
    """Whether every agent step of ``plan`` answers for its own changes: a
    writable workflow of the project's without a ``write_feature_maps`` step."""
    return not (
        plan.readonly
        or plan.workflow in builtin_workflow_names()
        or any(item.write_feature_maps for item in plan.items)
    )


def maps_due_for(
    root: Path,
    state: ExecutionState,
    plan: WorkflowPlan,
    item: PlanItem,
    config: ProjectConfig,
    read_artifact: Callable[[str], str],
) -> MapsDue | None:
    """What ``item`` answers for (see :func:`writes_maps`); ``None`` when it
    writes no maps, or without Git to measure its changes."""
    scope = writes_maps(plan, item, config)
    if scope is None:
        return None
    directory = resolve_workspace(root, state.working_directory) or root
    if scope == "run":
        changed = changed_paths(directory, run_base_branch(config, plan))
    else:
        since = state.item_executions[state.cursor].change_mark
        now = take_mark(directory)
        changed = (
            touched_files(directory, since, now)
            if since is not None and now is not None
            else None
        )
    if changed is None:
        return None
    gaps = tuple(
        f"{step}: {gap}"
        for step, report in maps_reports(state, plan, read_artifact)
        for gap in report.gaps
    )
    return maps_due(directory, changed, gaps)


def refusal(due: MapsDue, artifact: str | None) -> str | None:
    """Why a writing step may not complete yet, or ``None`` when it may."""
    problems: list[str] = []
    if due.broken:
        problems.append(
            "These pointers no longer resolve; fix them in their maps:\n"
            + "".join(f"- {problem}\n" for problem in due.broken)
        )
    unanswered = due.unanswered(read_maps_answers(artifact or ""))
    if unanswered:
        problems.append(
            f"The artifact's {FEATURE_MAPS_HEADING} section has no answer for "
            "these; give each one a line, `<map or file>: <answer>` (updated, no "
            "change needed, added to <map>, not feature code):\n"
            + "".join(f"- {subject}\n" for subject in unanswered)
        )
    if not problems:
        return None
    return (
        "the feature maps are not up to date with these changes; bring them up "
        f"to date (`{ww_command()} features show <name>` prints a map) and "
        "complete again.\n\n" + "\n".join(problems)
    )


def record_run_tasks(due: MapsDue, state: ExecutionState) -> None:
    """Add the task to the list of every map its run's changes touched."""
    for feature in due.affected:
        record_task(due.directory, feature.name, state.task_id, state.description or "")
