# SPDX-License-Identifier: GPL-3.0-or-later
"""Feature maps in a run: what its reading steps reported, what a writing
step must answer for, and the task lists it leaves behind.

The run's change set is every file changed in its workspace since the run's
branch forked from its ww/git base branch (or since ``HEAD`` without one),
the maps' own files left out. The page of a writing step and its completion
both read it from here, so they always agree on what is due.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ww.config_snapshots import GIT_EXTENSION
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
from ww.plan import WorkflowPlan
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


def run_maps_due(
    root: Path,
    state: ExecutionState,
    plan: WorkflowPlan,
    config: ProjectConfig,
    read_artifact: Callable[[str], str],
) -> MapsDue | None:
    """What a writing step of this run answers for; ``None`` without Git."""
    directory = resolve_workspace(root, state.working_directory) or root
    changed = changed_paths(directory, run_base_branch(config, plan))
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
        "the feature maps are not up to date with this run's changes.\n\n"
        + "\n".join(problems)
    )


def record_run_tasks(due: MapsDue, state: ExecutionState) -> None:
    """Add the task to the list of every map its run's changes touched."""
    for feature in due.affected:
        record_task(due.directory, feature.name, state.task_id, state.description or "")
