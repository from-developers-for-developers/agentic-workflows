# SPDX-License-Identifier: GPL-3.0-or-later
"""Feedback provenance, recurrence counting, retirement and atomic rejection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.errors import ConfigurationError, StateError
from ww.feedback import STALE_TASKS, FeedbackStore
from ww.interactions import InteractionLog
from ww.project_config import load_project_config
from ww.storage import Storage


def _store(root: Path, task: str = "TASK-1") -> FeedbackStore:
    storage = Storage(root)
    InteractionLog(storage).append_entries(
        task,
        [("agent", "I used naam."), ("operator", "Use English names.")],
        run_id="01-task",
        step="review",
        at="2026-10-03T12:00:00Z",
    )
    return FeedbackStore(storage)


def _analysis(identifier: str | None = None) -> list[dict[str, object]]:
    point: dict[str, object] = {
        "summary": "Use English variable names.",
        "reason": "Language was unspecified; the same ambiguity applies to new code.",
        "enforcement": "reasoning",
        "approach": "Review identifier language; dictionaries have false positives.",
        "entries": [2],
    }
    if identifier:
        point["id"] = identifier
    return [point]


def test_retries_match_ids_and_preserve_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record("TASK-1", "01-task", "review", None, _analysis())
    store.record("TASK-1", "01-task", "review", None, _analysis())
    point = store.listing()["points"][0]
    assert point["occurrences"] == 1
    assert point["events"][0]["quote"] == "Use English names."
    _store(tmp_path, "TASK-2")
    analysis = _analysis(point["id"])
    analysis[0]["summary"] = "Keep identifiers in English."
    store.record("TASK-2", "01-task", "review", None, analysis)
    store.complete_task("TASK-1")
    store.complete_task("TASK-2")
    store.complete_task("TASK-3")
    point = store.listing()["points"][0]
    assert point["occurrences"] == 2
    assert point["task_occurrences"] == 2
    assert point["task_ratio"] == pytest.approx(2 / 3)
    assert point["completed_task_ratio"] == pytest.approx(2 / 3)


def test_stale_points_retire_by_completed_tasks_not_rounds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record("TASK-1", "01-task", "review", None, _analysis())
    store.complete_task("TASK-1")
    for _ in range(STALE_TASKS + 1):
        store.complete_task("TASK-1")
    assert store.listing()["points"]
    for index in range(2, STALE_TASKS + 1):
        store.complete_task(f"TASK-{index}")
    assert store.listing()["points"]
    store.complete_task(f"TASK-{STALE_TASKS + 1}")
    assert store.listing()["points"] == []
    assert store.evidence("TASK-1")["entries"][1]["text"] == "Use English names."


def test_pending_tasks_are_not_treated_as_negative_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record("TASK-1", "01-task", "review", None, _analysis())
    for index in range(2, STALE_TASKS + 3):
        store.complete_task(f"TASK-{index}")
    assert store.listing()["points"]


@pytest.mark.parametrize(
    "bad",
    [
        {"entries": [1]},
        {"entries": [True]},
        {"entries": [99]},
        {"enforcement": "guess"},
        {"id": "missing"},
        {"summary": ""},
    ],
)
def test_invalid_batch_changes_nothing(tmp_path: Path, bad: dict[str, object]) -> None:
    store = _store(tmp_path)
    store.record("TASK-1", "01-task", "review", None, _analysis())
    before = store.path.read_bytes()
    point = {**_analysis()[0], **bad}
    with pytest.raises(StateError):
        store.record("TASK-1", "01-task", "review", None, [_analysis()[0], point])
    assert store.path.read_bytes() == before


def test_evidence_must_belong_to_current_step(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(StateError, match="this step"):
        store.record("TASK-1", "01-task", "other", None, _analysis())
    assert not store.path.exists()


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
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"schema": 999}')
    with pytest.raises(StateError, match="unsupported"):
        store.complete_task("TASK-1")
    assert store.path.read_text() == '{"schema": 999}'


def test_multiple_comments_count_occurrences_without_inflating_task_frequency(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.log.append(
        "TASK-1",
        run_id="01-task",
        step="review",
        speaker="operator",
        text="Again, use English names.",
        at="2026-10-03T12:01:00Z",
    )
    analysis = _analysis()
    analysis[0]["entries"] = [2, 3, 3]
    store.record("TASK-1", "01-task", "review", None, analysis)
    point = store.listing()["points"][0]
    assert point["occurrences"] == 2
    assert point["task_ratio"] == 1
    assert point["occurrence_ratio"] == 2
    store.record("TASK-2", "01-task", "review", None, [])
    point = store.listing()["points"][0]
    assert point["occurrence_ratio"] == 1
    assert point["task_ratio"] == 0.5
