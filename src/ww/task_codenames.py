# SPDX-License-Identifier: GPL-3.0-or-later
"""A task's codename: an alias the operator gives a task, such as ``Atlas``.

It never replaces the task ID, which stays the name of the task's branch and
files; it only lets the operator name the task, usually a long-lived parent
such as an epic, the way they think of it. Every command that addresses an
existing task takes it in place of the ID (``resolve_task``), and ``lookup``
matches it.

A codename starts with a letter, holds letters, digits, ``.``, ``_`` and
``-``, and is at most 40 characters long. It is unique in the project,
compared without regard to letter case, and must not read as a task ID: not
an existing task's, not one the project's task format builds from it, and
not a tracker key such as ``PROJ-123``.
"""

from __future__ import annotations

import re

from ww.errors import StateError
from ww.storage_adapters import TaskStorageAdapter
from ww.task_references import formatted_task_id, resolve_task_reference

CODENAME_LIMIT = 40
_CODENAME = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")
# A tracker key such as "PROJ-123" or "ab_7": letters, a separator, digits.
_TRACKER_KEY = re.compile(r"[A-Za-z]+[-_]\d+")


def validate_codename(text: str) -> str:
    """The codename as stored, or a ``StateError`` saying what is wrong."""
    codename = text.strip()
    if not codename:
        raise StateError("a codename must not be empty")
    if len(codename) > CODENAME_LIMIT:
        raise StateError(
            f"codename {codename!r} is longer than {CODENAME_LIMIT} characters"
        )
    if not _CODENAME.fullmatch(codename):
        raise StateError(
            f"codename {codename!r} must start with a letter and hold only "
            "letters, digits, '.', '_' and '-'"
        )
    if _TRACKER_KEY.fullmatch(codename):
        raise StateError(
            f"codename {codename!r} reads as a tracker key; choose a name, "
            "such as Atlas"
        )
    return codename


def codenames(tasks: TaskStorageAdapter) -> dict[str, str]:
    """Every task's codename by task ID, children included.

    A codename file that does not read is left out, as if the task had none.
    """
    found: dict[str, str] = {}
    for task_id in tasks.task_ids():
        for candidate in (task_id, *tasks.child_task_ids(task_id)):
            try:
                codename = tasks.read_codename(candidate)
            except StateError:
                continue
            if codename is not None:
                found[candidate] = codename
    return found


def codename_owner(tasks: TaskStorageAdapter, name: str) -> str | None:
    """The task whose codename is ``name``, in any letter case."""
    folded = name.strip().casefold()
    return next(
        (
            task_id
            for task_id, codename in codenames(tasks).items()
            if codename.casefold() == folded
        ),
        None,
    )


def resolve_task(tasks: TaskStorageAdapter, reference: str) -> str:
    """The task ID ``reference`` names: itself, or the task its codename names.

    An existing task ID always wins. A child is named through its parent's
    codename too: ``Atlas/api`` is ``TASK-64/api`` when ``Atlas`` names
    ``TASK-64``. Anything else is returned unchanged, for the command to
    accept or refuse as a task ID.
    """
    if not reference or tasks.task_exists(reference):
        return reference
    head, separator, rest = reference.partition("/")
    owner = codename_owner(tasks, head)
    if owner is None:
        return reference
    return f"{owner}/{rest}" if separator else owner


def set_codename(
    tasks: TaskStorageAdapter,
    task_id: str,
    codename: str | None,
    task_format: str | None,
) -> str | None:
    """Give ``task_id`` the codename, or clear it with ``None``; return it.

    The check and the write hold the codename lock, so two tasks never end up
    with the same codename.
    """
    if codename is None:
        tasks.write_codename(task_id, None)
        return None
    codename = validate_codename(codename)
    with tasks.lock_codenames():
        _require_free(tasks, task_id, codename, task_format)
        tasks.write_codename(task_id, codename)
    return codename


def check_codename(
    tasks: TaskStorageAdapter, task_id: str, codename: str, task_format: str | None
) -> str:
    """Refuse a codename ``task_id`` could not take, before anything is written."""
    codename = validate_codename(codename)
    _require_free(tasks, task_id, codename, task_format)
    return codename


def require_id_not_a_codename(tasks: TaskStorageAdapter, task_id: str) -> None:
    """Refuse a new task ID that another task already uses as its codename."""
    owner = codename_owner(tasks, task_id)
    if owner is not None and owner != task_id:
        raise StateError(
            f"{task_id!r} is the codename of {owner}; choose another task ID, "
            f"or continue {owner}"
        )


def _require_free(
    tasks: TaskStorageAdapter, task_id: str, codename: str, task_format: str | None
) -> None:
    owner = codename_owner(tasks, codename)
    if owner is not None and owner != task_id:
        raise StateError(f"codename {codename!r} already names {owner}")
    built = formatted_task_id(codename, task_format)
    if built is not None:
        raise StateError(
            f"codename {codename!r} reads as task ID {built} under the task "
            "format; choose a name, such as Atlas"
        )
    known = [
        candidate
        for top in tasks.task_ids()
        for candidate in (top, *tasks.child_task_ids(top))
        if candidate != task_id
    ]
    clash = resolve_task_reference(codename, task_format, known).matches
    if clash:
        raise StateError(
            f"codename {codename!r} reads as task ID {clash[0]}; choose another"
        )
