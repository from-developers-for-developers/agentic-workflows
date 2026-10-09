# SPDX-License-Identifier: GPL-3.0-or-later
"""Artifact deduction, stable point IDs, recency and explicit maintenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.errors import ConfigurationError, StateError
from ww.feedback import STALE_TASKS, FeedbackStore
from ww.project_config import load_project_config
from ww.storage import Storage


def _sources(at: str = "2026-10-03T12:00:00Z") -> list[dict[str, str]]:
    return [
        {
            "id": "artifact-1",
            "artifact": "review.md",
            "step": "review",
            "encountered_at": at,
            "content": "Use English names. Again, use English names.",
        }
    ]


def _analysis(identifier: str | None = None) -> list[dict[str, object]]:
    point: dict[str, object] = {
        "summary": "Use English variable names.",
        "reason": "Unspecified naming language can recur in new code.",
        "enforcement": "reasoning",
        "approach": "Review identifiers; dictionaries have false positives.",
        "evidence": [{"source": "artifact-1", "quote": "Use English names."}],
    }
    if identifier:
        point["id"] = identifier
    return [point]


def test_retries_and_explicit_id_updates_preserve_counts_and_time(
    tmp_path: Path,
) -> None:
    store = FeedbackStore(Storage(tmp_path))
    store.record("TASK-1", "01-task", _analysis(), _sources())
    store.record("TASK-1", "01-task", _analysis(), _sources())
    point = store.listing()["points"][0]
    assert point["occurrences"] == 1
    assert point["last_encountered_at"] == "2026-10-03T12:00:00Z"
    assert point["events"][0]["artifact"] == "review.md"
    assert store.get(point["id"])["occurrences"] == 1
    with pytest.raises(StateError, match="pass its id"):
        store.record("TASK-2", "01-task", _analysis(), _sources())
    store.record(
        "TASK-2", "01-task", _analysis(point["id"]), _sources("2026-10-04T12:00:00Z")
    )
    store.complete_task("TASK-3")
    point = store.get(point["id"])
    assert point["occurrences"] == 2
    assert point["task_occurrences"] == 2
    assert point["task_ratio"] == pytest.approx(2 / 3)
    assert point["last_encountered_at"] == "2026-10-04T12:00:00Z"
    # Deduction of older artifacts never moves the timestamp backwards.
    store.record(
        "TASK-4", "01-task", _analysis(point["id"]), _sources("2026-09-01T12:00:00Z")
    )
    assert store.get(point["id"])["last_encountered_at"] == "2026-10-04T12:00:00Z"


def test_completion_never_deletes_and_pruning_is_explicit(tmp_path: Path) -> None:
    store = FeedbackStore(Storage(tmp_path))
    store.record("TASK-1", "01-task", _analysis(), _sources())
    identifier = store.listing()["points"][0]["id"]
    for index in range(2, STALE_TASKS + 2):
        store.complete_task(f"TASK-{index}")
    assert store.get(identifier)["tasks_since_last_seen"] == STALE_TASKS
    before = store.path.read_bytes()
    assert store.prune(dry_run=True)["pruned"] == [identifier]
    assert store.path.read_bytes() == before
    assert store.prune(keep=(identifier,))["pruned"] == []
    assert store.get(identifier)
    assert store.prune()["pruned"] == [identifier]
    assert store.listing()["points"] == []


def test_new_match_refreshes_pruning_recency_and_duplicate_does_not(
    tmp_path: Path,
) -> None:
    store = FeedbackStore(Storage(tmp_path))
    store.record("TASK-1", "01-task", _analysis(), _sources())
    identifier = store.listing()["points"][0]["id"]
    for index in range(2, STALE_TASKS + 2):
        store.complete_task(f"TASK-{index}")
    store.record("TASK-1", "01-task", _analysis(identifier), _sources())
    assert store.prune(dry_run=True)["pruned"] == [identifier]
    store.record("TASK-7", "01-task", _analysis(identifier), _sources())
    assert store.prune(dry_run=True)["pruned"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"evidence": [{"source": "missing", "quote": "Use English names."}]},
        {"evidence": [{"source": "artifact-1", "quote": "invented"}]},
        {"evidence": []},
        {"enforcement": "guess"},
        {"id": "missing"},
        {"summary": ""},
    ],
)
def test_invalid_batch_changes_nothing(tmp_path: Path, bad: dict[str, object]) -> None:
    store = FeedbackStore(Storage(tmp_path))
    store.record("TASK-1", "01-task", _analysis(), _sources())
    before = store.path.read_bytes()
    point = {**_analysis()[0], **bad}
    with pytest.raises(StateError):
        store.record("TASK-1", "01-task", [_analysis()[0], point], _sources())
    assert store.path.read_bytes() == before


def test_setting_defaults_true_and_rejects_non_booleans(tmp_path: Path) -> None:
    path = tmp_path / "ww.json"
    assert load_project_config(path).feedback_learning is True
    path.write_text(json.dumps({"feedback_learning": False}))
    assert load_project_config(path).feedback_learning is False
    for value in (1, "true", None, {}):
        path.write_text(json.dumps({"feedback_learning": value}))
        with pytest.raises(ConfigurationError, match="true or false"):
            load_project_config(path)


def test_corrupt_store_is_reported_without_overwriting(tmp_path: Path) -> None:
    store = FeedbackStore(Storage(tmp_path))
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"schema": 999}')
    with pytest.raises(StateError, match="unsupported"):
        store.complete_task("TASK-1")
    assert store.path.read_text() == '{"schema": 999}'


def test_multiple_quotes_do_not_inflate_distinct_task_frequency(tmp_path: Path) -> None:
    store = FeedbackStore(Storage(tmp_path))
    analysis = _analysis()
    evidence = analysis[0]["evidence"]
    evidence.extend(
        [{"source": "artifact-1", "quote": "Again, use English names."}] * 2
    )
    store.record("TASK-1", "01-task", analysis, _sources())
    point = store.listing()["points"][0]
    assert point["occurrences"] == 2
    assert point["task_ratio"] == 1
    assert point["occurrence_ratio"] == 2
    store.record("TASK-2", "01-task", [], [])
    point = store.get(point["id"])
    assert point["occurrence_ratio"] == 1
    assert point["task_ratio"] == 0.5
