# SPDX-License-Identifier: GPL-3.0-or-later
"""Task identity validation, generation, and claim checks."""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

from ww.errors import StateError
from ww.extensions import ExtensionRegistry
from ww.service import WorkflowService
from ww.storage import Storage
from ww.storage_adapters import MemoryTaskStorageAdapter
from ww.task_ids import (
    ID_ATTEMPT_LIMIT,
    candidate_task_ids,
    generated_bootstrap_id,
    generated_task_id,
    is_bootstrap_request,
    task_id_claimed,
    validate_child_id,
    validate_task_id,
)


@pytest.mark.parametrize(
    "task_id", ["TASK-1", "PROJ-123", "backend.PROJ-1", "a_b", "P/c1", "1"]
)
def test_valid_task_ids(task_id: str) -> None:
    validate_task_id(task_id)


@pytest.mark.parametrize(
    "task_id",
    ["", "a/b/c", "a/", "/a", ".", "..", "a/..", "PROJ 1", "repo:1", "a\\b", "é"],
)
def test_invalid_task_ids(task_id: str) -> None:
    with pytest.raises(StateError, match="invalid task ID"):
        validate_task_id(task_id)


def test_child_ids_are_single_segments() -> None:
    validate_child_id("c1")
    with pytest.raises(StateError, match="child ID must be a single normalized name"):
        validate_child_id("a/b")
    with pytest.raises(StateError, match="invalid task ID"):
        validate_child_id("a b")


def test_bootstrap_requests_are_recognised_by_prefix() -> None:
    request = generated_bootstrap_id()

    assert is_bootstrap_request(request)
    assert re.fullmatch(r"REQUEST-\d{20}", request)
    assert not is_bootstrap_request("TASK-1")


def test_generated_ids_follow_the_format() -> None:
    assert re.fullmatch(r"TASK-\d{14}", generated_task_id())
    assert re.fullmatch(r"JOB-\d{14}", generated_task_id("JOB-{timestamp}"))
    assert re.fullmatch(
        r"ID-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        generated_task_id("ID-{uuid}"),
    )


def test_digit_formats_count_without_limit() -> None:
    candidates = list(itertools.islice(candidate_task_ids("TASK-{digit}"), 100))

    assert candidates[:3] == ["TASK-1", "TASK-2", "TASK-3"]
    assert candidates[-1] == "TASK-100"


def test_other_formats_try_bounded_suffixes() -> None:
    candidates = list(candidate_task_ids("FIXED"))

    assert candidates[:3] == ["FIXED", "FIXED-2", "FIXED-3"]
    assert len(candidates) == ID_ATTEMPT_LIMIT
    assert candidates[-1] == f"FIXED-{ID_ATTEMPT_LIMIT}"


def test_claims_come_from_task_state(tmp_path: Path) -> None:
    assert not task_id_claimed(
        "TASK-1",
        tasks=MemoryTaskStorageAdapter(),
        extensions=ExtensionRegistry(tmp_path),
    )

    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("task", "TASK-1", agent="codex")

    claimed = {
        task_id: task_id_claimed(
            task_id, tasks=service.tasks, extensions=service.extensions
        )
        for task_id in ("TASK-1", "TASK-2")
    }
    assert claimed == {"TASK-1": True, "TASK-2": False}
