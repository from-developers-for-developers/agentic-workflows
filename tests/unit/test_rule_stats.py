# SPDX-License-Identifier: GPL-3.0-or-later
"""The local rule statistics store: counting, locking, and tolerated damage."""

from __future__ import annotations

import json
from pathlib import Path

from ww.rule_stats import (
    RuleEvaluation,
    RuleFailure,
    RuleStats,
    RuleStatsStore,
)
from ww.storage import Storage

HEADER = RuleEvaluation("docs/header", "a" * 64, "check failed")
CLI = RuleEvaluation("develop/1", "b" * 64, "verdict fail")


def _store(root: Path) -> RuleStatsStore:
    return RuleStatsStore(Storage(root))


def _record(store: RuleStatsStore, *outcomes: RuleEvaluation, now: str) -> None:
    store.record_outcomes("TASK-1", "develop", outcomes, now)


def test_each_settled_completion_counts_once_per_rule(tmp_path: Path) -> None:
    store = _store(tmp_path)

    _record(store, HEADER, now="t1")
    _record(store, HEADER, now="t2")
    _record(
        store,
        RuleEvaluation("docs/header", "a" * 64, "check passed"),
        RuleEvaluation("develop/1", "b" * 64, "verdict pass"),
        now="t3",
    )

    stats = store.load()
    assert stats["docs/header"] == RuleStats(
        applied=3,
        checked=3,
        check_failures=2,
        last_applied_at="t3",
        last_failed_at="t2",
        last_failure=RuleFailure("TASK-1", "develop", "check"),
        text_hash="a" * 64,
    )
    assert stats["develop/1"] == RuleStats(
        applied=1, judged=1, last_applied_at="t3", text_hash="b" * 64
    )
    assert stats["docs/header"].failures == 2
    written = json.loads(store.path.read_text(encoding="utf-8"))
    assert written["schema"] == 1
    assert list(written["rules"]) == ["develop/1", "docs/header"]


def test_verdicts_waivers_and_not_applicable_count_apart(tmp_path: Path) -> None:
    store = _store(tmp_path)

    _record(store, CLI, now="t1")
    _record(store, RuleEvaluation("develop/1", "b" * 64, "verdict pass"), now="t2")
    _record(store, RuleEvaluation("develop/1", "b" * 64, "waived"), now="t3")
    _record(store, RuleEvaluation("develop/1", "c" * 64, "not applicable"), now="t4")

    assert store.load()["develop/1"] == RuleStats(
        applied=2,
        judged=2,
        judged_failures=1,
        waived=1,
        not_applicable=1,
        last_applied_at="t2",
        last_failed_at="t1",
        last_failure=RuleFailure("TASK-1", "develop", "verdict"),
        text_hash="c" * 64,
    )


def test_nothing_is_written_without_an_evaluation(tmp_path: Path) -> None:
    store = _store(tmp_path)

    _record(store, now="t1")

    assert not store.path.exists()
    assert store.load() == {}


def test_the_store_is_written_under_its_own_lock(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    store = RuleStatsStore(storage)

    _record(store, HEADER, now="t1")

    assert storage.locks.lock_path(store.path).exists()


def test_an_unreadable_file_counts_as_empty_and_is_replaced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() == {}
    store.path.write_text(json.dumps({"schema": 99, "rules": {}}), encoding="utf-8")
    assert store.load() == {}

    _record(store, HEADER, now="t1")

    assert store.load()["docs/header"].check_failures == 1


def test_a_write_error_is_dropped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.parent.mkdir(parents=True)
    # The store's directory is a file: nothing under it can be written.
    store.path.parent.write_text("", encoding="utf-8")

    _record(store, HEADER, now="t1")

    assert store.load() == {}


def test_counters_round_trip_through_their_dict_form() -> None:
    stats = RuleStats(
        applied=2,
        checked=1,
        judged=1,
        judged_failures=1,
        last_applied_at="t2",
        last_failed_at="t2",
        last_failure=RuleFailure("TASK-1", "develop", "verdict"),
        text_hash="a" * 64,
    )

    assert RuleStats.from_dict(json.loads(json.dumps(stats.to_dict()))) == stats
