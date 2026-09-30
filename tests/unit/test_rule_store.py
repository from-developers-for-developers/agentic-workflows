# SPDX-License-Identifier: GPL-3.0-or-later
"""The rule-automation store: format, strict reading, and locked changes."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from ww.actions import AssertionDefinition, CommandDefinition, Commands
from ww.config.rules import rule_text_hash
from ww.errors import StateError
from ww.locking import FileLocks
from ww.rule_store import (
    STORE_FILE,
    CheckEntry,
    CheckSpec,
    RuleAutomation,
    RuleEntry,
    RuleStore,
    is_check_name,
)

TEXT = "Controllers must not instantiate services; inject them."


def _check(covers: tuple[str, ...] = ()) -> CheckEntry:
    return CheckEntry(
        CheckSpec(
            Commands(
                (CommandDefinition(shell="grep -L foo $WW_STEP_CHANGED_FILES"),),
                AssertionDefinition("empty"),
            ),
            config=("deptrac.yaml",),
            covers=covers,
            proven=True,
        ),
        "converted",
        proposed_at="2026-09-29T10:00:00Z",
        approved_at="2026-09-29T11:00:00Z",
        proposed_in="TASK-1:develop:verify:1",
    )


def test_a_missing_store_is_empty(tmp_path: Path) -> None:
    store = RuleStore(tmp_path)

    assert not store.exists()
    assert store.load() == RuleAutomation()


def test_the_store_round_trips_rules_and_checks(tmp_path: Path) -> None:
    text_hash = rule_text_hash(TEXT)
    automation = (
        RuleAutomation()
        .with_rule(
            text_hash,
            RuleEntry(
                TEXT,
                "converted",
                interpretation="No `new *Service(` in src/Controller.",
                approach="deptrac layer rule",
                check="deptrac",
                proposed_in="TASK-1:develop:verify:1",
            ),
        )
        .with_check("deptrac", _check((text_hash,)))
    )
    store = RuleStore(tmp_path)

    store.modify(lambda _: automation)

    assert store.load() == automation
    data = json.loads((tmp_path / STORE_FILE).read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert data["checks"]["deptrac"]["shell"].startswith("grep -L foo")
    assert data["checks"]["deptrac"]["assert"] == {"operator": "empty"}
    assert data["checks"]["deptrac"]["covers"] == [text_hash]
    assert automation.converted_check(text_hash) == ("deptrac", _check((text_hash,)))


def test_a_pending_revision_is_kept_beside_the_approved_check(tmp_path: Path) -> None:
    revision = CheckSpec(Commands((CommandDefinition(argv=("deptrac", "analyse")),)))
    entry = CheckEntry(_check().spec, "converted", pending=revision)
    store = RuleStore(tmp_path)

    store.modify(lambda automation: automation.with_check("deptrac", entry))

    loaded = store.load().checks["deptrac"]
    assert loaded.pending == revision
    assert loaded.undecided
    assert loaded.spec == _check().spec


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"schema_version": 3, "rules": {}, "checks": {}}),
        json.dumps(
            {
                "schema_version": 2,
                "rules": {
                    "h": {"text": "x", "status": "converted", "approved_by": "bot"}
                },
            }
        ),
        json.dumps({"schema_version": 1, "rules": {}, "checks": {}, "extra": 1}),
        json.dumps({"schema_version": 1, "rules": {"h": {"text": "x"}}}),
        json.dumps(
            {"schema_version": 1, "rules": {"h": {"text": "x", "status": "maybe"}}}
        ),
        json.dumps(
            {
                "schema_version": 1,
                "rules": {"h": {"text": "x", "status": "rejected", "colour": 1}},
            }
        ),
        json.dumps({"schema_version": 1, "checks": {"Bad Name": {}}}),
        json.dumps(
            {
                "schema_version": 1,
                "checks": {"lint": {"status": "converted", "config": []}},
            }
        ),
    ],
)
def test_a_malformed_store_is_an_error_never_an_empty_one(
    tmp_path: Path, content: str
) -> None:
    (tmp_path / STORE_FILE).write_text(content, encoding="utf-8")

    with pytest.raises(StateError, match="invalid rule automation store"):
        RuleStore(tmp_path).load()


def test_an_unchanged_store_is_not_rewritten(tmp_path: Path) -> None:
    store = RuleStore(tmp_path)

    store.modify(lambda automation: automation)

    assert not store.exists()


def test_changes_from_two_stores_keep_each_other(tmp_path: Path) -> None:
    first, second = RuleStore(tmp_path), RuleStore(tmp_path)

    first.modify(lambda a: a.with_rule("a" * 64, RuleEntry("A.", "rejected")))
    second.modify(lambda a: a.with_rule("b" * 64, RuleEntry("B.", "rejected")))

    assert set(RuleStore(tmp_path).load().rules) == {"a" * 64, "b" * 64}


def test_a_change_holds_the_store_lock_for_its_whole_read_modify_write(
    tmp_path: Path,
) -> None:
    store = RuleStore(tmp_path)
    inside = threading.Event()
    release = threading.Event()
    finished: list[str] = []

    def slow(automation: RuleAutomation) -> RuleAutomation:
        inside.set()
        release.wait(5)
        return automation.with_rule("a" * 64, RuleEntry("A.", "rejected"))

    def other() -> None:
        inside.wait(5)
        RuleStore(tmp_path).modify(
            lambda a: a.with_rule("b" * 64, RuleEntry("B.", "rejected"))
        )
        finished.append("other")

    worker = threading.Thread(target=other)
    worker.start()
    holder = threading.Thread(target=lambda: store.modify(slow))
    holder.start()
    inside.wait(5)
    # The second change waits on the lock, so it cannot finish yet.
    worker.join(0.2)
    assert finished == []
    release.set()
    holder.join(5)
    worker.join(5)

    assert set(store.load().rules) == {"a" * 64, "b" * 64}
    assert FileLocks(tmp_path).lock_path(tmp_path / STORE_FILE).is_file()


def test_the_hash_ignores_whitespace_but_not_wording() -> None:
    assert rule_text_hash(f"  {TEXT}\n") == rule_text_hash(TEXT.replace(" ", "\n  "))
    assert rule_text_hash(TEXT) != rule_text_hash(TEXT.replace("must", "should"))


def test_check_names_are_short_kebab_case() -> None:
    assert is_check_name("deptrac")
    assert is_check_name("no-print-calls")
    assert not is_check_name("No-Print")
    assert not is_check_name("a" * 41)
    assert not is_check_name("x--y")


def test_a_version_1_store_is_read_with_an_unknown_approver(tmp_path: Path) -> None:
    text_hash = rule_text_hash(TEXT)
    (tmp_path / STORE_FILE).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": {
                    text_hash: {"text": TEXT, "status": "converted", "check": "deptrac"}
                },
                "checks": {"deptrac": _check((text_hash,)).to_dict()},
            }
        ),
        encoding="utf-8",
    )
    store = RuleStore(tmp_path)

    loaded = store.load()

    assert loaded.rules[text_hash].approved_by is None
    assert loaded.checks["deptrac"].approved_by is None
    assert loaded.converted_check(text_hash) is not None
    entry = loaded.rules[text_hash]
    store.modify(lambda automation: automation.with_rule("other", entry))
    data = json.loads((tmp_path / STORE_FILE).read_text(encoding="utf-8"))
    assert data["schema_version"] == 2


def test_approval_provenance_round_trips(tmp_path: Path) -> None:
    text_hash = rule_text_hash(TEXT)
    check = CheckEntry(
        _check((text_hash,)).spec,
        "rejected",
        reason="revoked by the operator: too slow",
        proposed_run="TASK-1/01-task",
        approved_by="auto",
        approved_in="TASK-1/01-task",
    )
    rule = RuleEntry(
        TEXT,
        "converted",
        check="deptrac",
        proposed_run="TASK-1/01-task",
        approved_by="operator",
        approved_in="TASK-2/01-task",
    )
    store = RuleStore(tmp_path)

    store.modify(
        lambda automation: automation.with_rule(text_hash, rule).with_check(
            "deptrac", check
        )
    )

    loaded = store.load()
    assert loaded.checks["deptrac"] == check
    assert loaded.rules[text_hash] == rule
    data = json.loads((tmp_path / STORE_FILE).read_text(encoding="utf-8"))
    assert data["checks"]["deptrac"]["approved_by"] == "auto"
    assert data["rules"][text_hash]["approved_in"] == "TASK-2/01-task"
