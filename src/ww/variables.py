# SPDX-License-Identifier: GPL-3.0-or-later
"""Names and values for workflow variables owned by ww core."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ww.workspace import Workdir, item_workspace, resolve_workspace

TASK_ID = "__task_id"
WORKFLOWS = "__workflows"
TASK_WORKSPACE_DIR = "__task_workspace_dir"
BRANCH_NAMING_STRATEGY = "__branch_naming_strategy"
# The configured project a task works in, its directory, and every configured
# project name.  ``__project_dir`` stays the project's own directory even when
# an extension moves the task workspace, for example into a Git worktree.
PROJECT = "__project"
PROJECT_DIR = "__project_dir"
PROJECTS = "__projects"

CORE_VARIABLE_NAMES = (
    TASK_ID,
    WORKFLOWS,
    TASK_WORKSPACE_DIR,
    PROJECT,
    PROJECT_DIR,
    PROJECTS,
)
OVERRIDABLE_CORE_VARIABLE_NAMES = (TASK_WORKSPACE_DIR,)


def compile_variable_values(
    workflow_names: tuple[str, ...], task_id: str | None
) -> dict[str, str]:
    """Return core values known while a workflow plan is compiled."""
    values = {WORKFLOWS: ",".join(workflow_names)}
    if task_id is not None:
        values[TASK_ID] = task_id
    return values


def runtime_variable_values(
    root: Path,
    task_id: str,
    workspace: str | None = None,
    project: str | None = None,
    projects: tuple[str, ...] = (),
    project_dir: str | None = None,
) -> dict[str, str]:
    """Return core values resolved from current task execution state."""
    directory = (root / workspace).resolve() if workspace else root.resolve()
    return {
        TASK_ID: task_id,
        TASK_WORKSPACE_DIR: str(directory),
        PROJECT: project or "",
        PROJECT_DIR: project_dir or "",
        PROJECTS: ",".join(projects),
    }


def item_workspace_values(
    root: Path,
    workdir: Workdir,
    working_directory: str | None,
    values: Mapping[str, str],
) -> tuple[Path | None, dict[str, str]]:
    """The directory a plan item works in, and ``values`` as that item sees them.

    An item with its own ``workdir`` reads that directory as
    ``__task_workspace_dir``; a ``task`` item keeps the task's values, which
    an extension may already have pointed at its checkout.
    """
    if workdir == "task":
        return resolve_workspace(root, working_directory), dict(values)
    directory = item_workspace(
        root, workdir, working_directory, values.get(PROJECT_DIR) or None
    )
    return directory, {**values, TASK_WORKSPACE_DIR: str(directory)}
