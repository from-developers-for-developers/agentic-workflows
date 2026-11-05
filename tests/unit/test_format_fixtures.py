# SPDX-License-Identifier: GPL-3.0-or-later
"""Every sample of every persisted format still loads with the current code.

``tests/fixtures/formats/<format>/v<N>.json`` keeps one real file per format
and schema version, as the ww of that version wrote it. A sample is never
edited or removed while its version is readable: when a format's version
changes, its migration lands with a new sample beside the old ones. This
test fails only when a file an earlier ww wrote stops loading, which is a
break the version policy (``documentation/compatibility.md``) forbids.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ww.execution_models import ExecutionState, PlanSnapshot
from ww.feedback import FeedbackStore
from ww.migrations.data import FORMATS, WRITTEN_BY
from ww.rule_disputes import DISPUTES_FILE, DisputeLog
from ww.rule_stats import RuleStatsStore
from ww.rule_store import STORE_FILE, RuleStore
from ww.run_reports import KINDS, RunReportStore
from ww.storage import Storage
from ww.storage_adapters.filesystem import FileTaskStorageAdapter
from ww.storage_adapters.task_document import decode_run_document, decode_task_index

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "formats"
SAMPLE = re.compile(r"v([1-9][0-9]*)\.json")


def _rule_stats(root: Path, data: dict[str, Any]) -> None:
    store = RuleStatsStore(Storage(root))
    _write(store.path, data)
    # The store reads damage as empty, so an unread sample shows as no rules.
    assert set(store.load()) == set(data["rules"])


def _rule_store(root: Path, data: dict[str, Any]) -> None:
    _write(root / STORE_FILE, data)
    loaded = RuleStore(root).load()
    assert set(loaded.rules) == set(data["rules"])
    assert set(loaded.checks) == set(data["checks"])


def _dispute_log(root: Path, data: dict[str, Any]) -> None:
    _write(root / DISPUTES_FILE, data)
    assert len(DisputeLog(root).load()) == len(data["disputes"])


def _feedback_store(root: Path, data: dict[str, Any]) -> None:
    store = FeedbackStore(Storage(root))
    _write(store.path, data)
    assert {point["id"] for point in store.listing()["points"]} == set(data["points"])


def _run_report(root: Path, data: dict[str, Any]) -> None:
    store = RunReportStore(Storage(root), KINDS[data["kind"]])
    _write(store.directory / f"{data['id']}.json", data)
    assert store.get(data["id"])["run_id"] == data["run_id"]


LOADERS: dict[str, Callable[[Path, dict[str, Any]], None]] = {
    "task state": lambda _, data: decode_task_index(data, data["task_id"]),
    "run state": lambda _, data: decode_run_document(
        data, data["task_id"], data["run"]["run_id"], data["revision"]
    ),
    "plan snapshot": lambda _, data: PlanSnapshot.from_dict(data),
    "execution state": lambda _, data: ExecutionState.from_dict(data),
    "rule automation store": _rule_store,
    "rule dispute log": _dispute_log,
    "rule stats": _rule_stats,
    "feedback store": _feedback_store,
    "run report": _run_report,
}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _samples() -> list[tuple[str, Path]]:
    return [
        (directory.name.replace("-", " "), sample)
        for directory in sorted(FIXTURES.iterdir())
        for sample in sorted(directory.glob("v*.json"))
    ]


def test_every_format_has_a_loader_and_a_sample_of_its_current_version() -> None:
    assert set(LOADERS) == set(FORMATS)
    for name, data_format in FORMATS.items():
        directory = FIXTURES / name.replace(" ", "-")
        assert (directory / f"v{data_format.current}.json").is_file(), name
        for sample in directory.iterdir():
            assert SAMPLE.fullmatch(sample.name), sample


@pytest.mark.parametrize(
    "without_writer", [False, True], ids=["as-written", "unsigned"]
)
@pytest.mark.parametrize(
    ("name", "sample"),
    _samples(),
    ids=[f"{path.parent.name}/{path.name}" for _, path in _samples()],
)
def test_a_sample_loads_with_the_current_code(
    tmp_path: Path, name: str, sample: Path, without_writer: bool
) -> None:
    """``unsigned`` is the same file as written before ww recorded its writer."""
    data = json.loads(sample.read_text(encoding="utf-8"))
    match = SAMPLE.fullmatch(sample.name)
    assert match is not None
    assert data[FORMATS[name].version_key] == int(match.group(1))
    if without_writer:
        data.pop(WRITTEN_BY, None)

    LOADERS[name](tmp_path, data)


def test_a_task_index_and_its_run_load_together_from_disk(tmp_path: Path) -> None:
    index = json.loads((FIXTURES / "task-state/v1.json").read_text(encoding="utf-8"))
    run = json.loads((FIXTURES / "run-state/v1.json").read_text(encoding="utf-8"))
    task = tmp_path / ".ww" / "tasks" / index["task_id"]
    _write(task / "state.json", index)
    _write(task / "runs" / run["run"]["run_id"] / f"state.{run['revision']}.json", run)

    runs, _, revision = FileTaskStorageAdapter(tmp_path).read_task_record(
        index["task_id"]
    )

    assert revision == index["revision"]
    assert [loaded.run_id for loaded in runs] == [run["run"]["run_id"]]
