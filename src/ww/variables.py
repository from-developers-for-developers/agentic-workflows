# SPDX-License-Identifier: GPL-3.0-or-later
"""Names and values for workflow variables owned by ww core."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ww.executable import ww_command
from ww.workspace import Workdir, item_workspace, resolve_workspace

TASK_ID = "ww.task.id"
WORKFLOWS = "ww.task.workflows"
TASK_WORKSPACE_DIR = "ww.task.workspace_dir"
# Kept in a run's values for ww/git, which offers it as
# ``{{ww.git.branch_strategy}}``; never a template name of its own.
BRANCH_NAMING_STRATEGY = "__branch_naming_strategy"
# The configured project a task works in, its directory, and every configured
# project name.  ``ww.project.dir`` stays the project's own directory even
# when an extension moves the task workspace, for example into a Git worktree.
PROJECT = "ww.project.name"
PROJECT_DIR = "ww.project.dir"
PROJECTS = "ww.project.names"
# How a printed command invokes ww (``./ww`` or the configured executable), so
# a step's text can name a ww command the way ww's own pages do.
EXECUTABLE = "ww.executable"
# What a template reads about saved metadata, documents, and the stage's item.
METADATA_PREFIX = "ww.metadata."
PROJECT_METADATA_PREFIX = "ww.project_metadata."
DOCUMENTS_PREFIX = "ww.documents."
ITEM_ID = "ww.item.id"
ITEM_TEXT = "ww.item.text"
ITEM_FIELD_PREFIX = "ww.item.field."
# Values resolved while the task runs rather than when its plan is compiled.
RUNTIME_PREFIXES = (
    METADATA_PREFIX,
    PROJECT_METADATA_PREFIX,
    "ww.item.",
    "ww.child.",
)

CORE_VARIABLE_NAMES = (
    TASK_ID,
    WORKFLOWS,
    TASK_WORKSPACE_DIR,
    PROJECT,
    PROJECT_DIR,
    PROJECTS,
    EXECUTABLE,
)
OVERRIDABLE_CORE_VARIABLE_NAMES = (TASK_WORKSPACE_DIR,)

def unknown_template_message(names: set[str] | tuple[str, ...]) -> str:
    """Name the template values a handler references that are unavailable."""
    return "handler references unavailable variable(s): " + ", ".join(sorted(names))


# Every ww-provided template value lives under ``ww.``; an extension's
# namespace sits there too (ww/git provides ``{{ww.git.branch}}``).  The names
# ww keeps for its own values may not be claimed as an extension namespace.
WW_NAMESPACE = "ww"
RESERVED_NAMESPACES = (
    "executable",
    "task",
    "project",
    "documents",
    "metadata",
    "project_metadata",
    "item",
    "child",
)


# What a per-child parent stage reads about its child. The plan compiler
# accepts these only under ``children.steps`` (and checks the exact names
# there); ``{{ww.child.field.<name>}}`` is open-ended, and every extension
# namespace value is offered for the child as ``{{ww.child.<namespace>.*}}``.
CHILD_VALUE_PREFIX = "ww.child."
CHILD_FIELD_PREFIX = "ww.child.field."
CHILD_VALUE_NAMES = ("ww.child.id", "ww.child.text", "ww.child.project")


def child_value_name(name: str) -> str:
    """The child's counterpart of a ``ww.`` value (``ww.git.x``: ``ww.child.git.x``)."""
    _, _, rest = name.partition(".")
    return f"{CHILD_VALUE_PREFIX}{rest}"


def child_values(
    child_id: str,
    text: str,
    project: str | None,
    fields: tuple[tuple[str, str], ...],
) -> dict[str, str]:
    """``{{ww.child.*}}`` from a child's own record."""
    return {
        "ww.child.id": child_id,
        "ww.child.text": text,
        "ww.child.project": project or "",
        **{f"{CHILD_FIELD_PREFIX}{name}": value for name, value in fields},
    }


def namespaced(namespace: str, name: str) -> str:
    """The template name of ``name`` in an extension's ``namespace``."""
    return f"{WW_NAMESPACE}.{namespace}.{name}"


def is_reserved_name(name: str) -> bool:
    """Whether a provided or output value name would shadow a ww value."""
    return name.startswith("__") or name.split(".", 1)[0] == WW_NAMESPACE


# ww's own values, resolved when the plan is compiled or read as they stand
# (saved metadata, the stage's item); never a reason to stop before a step.
_CORE_NAMESPACES = tuple(
    [namespace] for namespace in RESERVED_NAMESPACES if namespace != "child"
)


def unavailable_ww_values(
    names: tuple[str, ...], values: Mapping[str, str]
) -> tuple[str, ...]:
    """The ``ww.`` names among ``names`` that ``values`` has no value for.

    The plan compiler accepts only ``ww.`` names something provides, so one
    missing here is a value its provider cannot give for this task yet, such
    as ``{{ww.git.branch}}`` before ww/git recorded the branch.  ww's own
    values (``ww.task.*``, ``ww.metadata.*``, ``ww.item.*``, ...) are never
    counted: they are known when the plan is compiled, or read as they stand.
    """
    return tuple(
        dict.fromkeys(
            name
            for name in names
            if name.split(".", 1)[0] == WW_NAMESPACE
            and name.split(".")[1:2] not in _CORE_NAMESPACES
            and name not in values
        )
    )


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
        EXECUTABLE: ww_command(),
    }


def item_workspace_values(
    root: Path,
    workdir: Workdir,
    working_directory: str | None,
    values: Mapping[str, str],
) -> tuple[Path | None, dict[str, str]]:
    """The directory a plan item works in, and ``values`` as that item sees them.

    An item with its own ``workdir`` reads that directory as
    ``{{ww.task.workspace_dir}}``; a ``task`` item keeps the task's values, which
    an extension may already have pointed at its checkout.
    """
    if workdir == "task":
        return resolve_workspace(root, working_directory), dict(values)
    directory = item_workspace(
        root, workdir, working_directory, values.get(PROJECT_DIR) or None
    )
    return directory, {**values, TASK_WORKSPACE_DIR: str(directory)}
