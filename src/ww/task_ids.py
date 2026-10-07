# SPDX-License-Identifier: GPL-3.0-or-later
"""Task identity policy: validation, generation, and claim checks."""

from __future__ import annotations

import itertools
import re
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

from ww.contracts import BOOTSTRAP_REQUEST_PREFIX
from ww.errors import StateError
from ww.extensions import ExtensionRegistry
from ww.project_config import EXPLICIT_TASK_FORMAT
from ww.storage_adapters import TaskStorageAdapter

GENERATED_TASK_PREFIX = "TASK-"
# A task ID is ``parent`` or ``parent/child``; deeper nesting is unsupported.
MAX_TASK_ID_SEGMENTS = 2
# Suffixed candidates tried for a template without a ``{{digit}}`` counter.
ID_ATTEMPT_LIMIT = 50
# One task ID segment, e.g. "TASK-12" or "FOOBAR_1.2"; "TASK 12" does not match.
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")


def validate_task_id(task_id: str) -> None:
    segments = task_id.split("/")
    if (
        not task_id
        or len(segments) > MAX_TASK_ID_SEGMENTS
        or any(
            segment in {".", ".."} or not _SEGMENT.fullmatch(segment)
            for segment in segments
        )
    ):
        raise StateError(
            "invalid task ID; use one or two slash-separated normalized names"
        )


def validate_child_id(child_id: str) -> None:
    validate_task_id(child_id)
    if "/" in child_id:
        raise StateError("child ID must be a single normalized name")


def is_bootstrap_request(task_id: str) -> bool:
    return task_id.startswith(BOOTSTRAP_REQUEST_PREFIX)


def bare_request_id(task_id: str) -> str:
    """``task_id`` without the parent qualifier of a child's identity request.

    A parent's child record names its child's request as ``EPIC-1/REQUEST-…``;
    the request itself is stored and addressed by the bare ``REQUEST-…``.  Any
    other ID is returned unchanged.
    """
    parent, _, last = task_id.rpartition("/")
    return last if parent and is_bootstrap_request(last) else task_id


def generated_task_id(task_format: str | None = None) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if task_format is None:
        return GENERATED_TASK_PREFIX + timestamp
    return task_format.replace("{{timestamp}}", timestamp).replace(
        "{{uuid}}", str(uuid.uuid4())
    )


def generated_bootstrap_id() -> str:
    return BOOTSTRAP_REQUEST_PREFIX + datetime.now(timezone.utc).strftime(
        "%Y%m%d%H%M%S%f"
    )


def candidate_task_ids(task_format: str | None) -> Iterator[str]:
    """Yield IDs to try, in order, for a generated task.

    Generated IDs are second-resolution, so two starts in the same second
    would otherwise collide.  A template with ``{{digit}}`` counts upward
    without limit; any other template tries the base name and then a bounded
    number of numeric suffixes.  The ``explicit`` format yields nothing.
    """
    if task_format == EXPLICIT_TASK_FORMAT:
        raise StateError(
            "this project requires an explicit task ID (task_format: explicit); "
            "pass the external key, such as the tracker issue key, to start"
        )
    base = generated_task_id(task_format)
    if task_format is not None and "{{digit}}" in task_format:
        for attempt in itertools.count(1):
            yield base.replace("{{digit}}", str(attempt))
        return
    yield base
    for attempt in range(2, ID_ATTEMPT_LIMIT + 1):
        yield f"{base}-{attempt}"


def task_id_claimed(
    task_id: str,
    *,
    tasks: TaskStorageAdapter,
    extensions: ExtensionRegistry,
    workflow_name: str | None = None,
    project: str | None = None,
    lane: str | None = None,
) -> bool:
    """Return whether task state or an extension already claims ``task_id``.

    An extension's artefact (for example a Git worktree) or record (for
    example a Git branch record) can outlive the task state it belonged to.
    Extensions report such paths and claims themselves, so core needs no
    knowledge of any one extension's settings; a task starting in a
    configured project asks under that project's settings.
    """
    if tasks.task_exists(task_id):
        return True
    if any(
        path.exists()
        for path in extensions.reserved_paths(task_id, workflow_name, project, lane)
    ):
        return True
    return extensions.claims_task(task_id, workflow_name, project, lane)
