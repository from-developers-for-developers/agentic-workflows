# SPDX-License-Identifier: GPL-3.0-or-later
"""Disputes and waivers as persisted, the dispute log, and the store's orphans."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ww.actions import Commands
from ww.actions.contracts import CommandDefinition
from ww.errors import StateError
from ww.execution_models import EXECUTION_SCHEMA_VERSION, Dispute, PlanItemExecution
from ww.rule_disputes import DISPUTES_FILE, DisputeEntry, DisputeLog
from ww.rule_store import CheckEntry, CheckSpec, RuleAutomation, RuleEntry
from ww.rule_views import Orphans, orphans, prune


def _record(**fields: Any) -> dict[str, Any]:
    record = PlanItemExecution("task:develop", 1).to_dict()
    record.update(fields)
    return record


def test_the_state_schema_is_ten() -> None:
    assert EXECUTION_SCHEMA_VERSION == 11


def test_a_dispute_and_waivers_round_trip_on_the_record() -> None:
    record = PlanItemExecution(
        "task:develop",
        1,
        checks_waived=(("docs/header", "Scratch file."), ("develop/sh", "Known.")),
        dispute=Dispute(
            check="docs/header",
            reason="Scratch file.",
            attempt=2,
            disputed_at="2026-09-30T10:00:00Z",
            command="grep -L foo",
            output="notes.md",
        ),
    )

    data = record.to_dict()

    assert data["checks_waived"] == {
        "docs/header": "Scratch file.",
        "develop/sh": "Known.",
    }
    assert PlanItemExecution.from_dict(data) == record


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        # The earlier single reason is not read as a waiver of everything.
        ({"checks_waived": "Checked by hand."}, "checks waived must map"),
        ({"checks_waived": {"docs/header": ""}}, "checks waived must map"),
        ({"dispute": {"check": "x"}}, "dispute"),
        (
            {
                "dispute": {
                    "check": "x",
                    "reason": "r",
                    "attempt": 0,
                    "disputed_at": "now",
                    "command": "",
                    "output": "",
                }
            },
            "dispute.attempt",
        ),
        (
            {
                "dispute": {
                    "check": "x",
                    "reason": "r",
                    "attempt": 1,
                    "disputed_at": "now",
                    "command": "",
                    "output": "",
                    "verdict": "mine",
                }
            },
            "unknown keys",
        ),
    ],
)
def test_malformed_disputes_and_waivers_are_refused(
    fields: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        PlanItemExecution.from_dict(_record(**fields))


def _entry(check: str = "docs/header", task: str = "TASK-1") -> DisputeEntry:
    return DisputeEntry(
        check=check,
        task_id=task,
        step="develop",
        reason="Scratch file.",
        attempt=1,
        disputed_at="2026-09-30T10:00:00Z",
        text_hash="a" * 64,
        run_id="01-task",
    )


def test_the_dispute_log_appends_and_reads_back(tmp_path: Path) -> None:
    log = DisputeLog(tmp_path)
    assert log.load() == ()

    log.append(_entry())
    log.append(_entry("develop/sh", "TASK-2"))

    assert DisputeLog(tmp_path).load() == (_entry(), _entry("develop/sh", "TASK-2"))
    data = json.loads((tmp_path / DISPUTES_FILE).read_text(encoding="utf-8"))
    assert data["schema_version"] == 1


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"schema_version": 2, "disputes": []}),
        json.dumps({"schema_version": 1, "disputes": {}}),
        json.dumps({"schema_version": 1, "disputes": [], "extra": 1}),
        json.dumps({"schema_version": 1, "disputes": [{"check": "x"}]}),
    ],
)
def test_a_malformed_dispute_log_is_an_error(tmp_path: Path, content: str) -> None:
    path = tmp_path / DISPUTES_FILE
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")

    with pytest.raises(StateError, match="invalid rule dispute log"):
        DisputeLog(tmp_path).load()


def _check(*covers: str, pending: tuple[str, ...] | None = None) -> CheckEntry:
    command = Commands((CommandDefinition(argv=("tool",)),))
    return CheckEntry(
        spec=CheckSpec(command, covers=covers),
        status="converted",
        pending=CheckSpec(command, covers=pending) if pending is not None else None,
    )


def test_orphans_are_undeclared_rules_and_the_checks_only_they_need() -> None:
    automation = RuleAutomation(
        rules={
            "gone": RuleEntry("Old.", "converted", check="old"),
            "live": RuleEntry("New.", "converted", check="shared"),
        },
        checks={
            "old": _check("gone"),
            "shared": _check("gone", "live"),
            "growing": _check("gone", pending=("gone", "live")),
        },
    )

    found = orphans(automation, frozenset({"live"}))

    assert found == Orphans(rules=("gone",), checks=("old",))
    pruned = prune(automation, found, frozenset({"live"}))
    assert set(pruned.rules) == {"live"}
    assert set(pruned.checks) == {"shared", "growing"}


def test_prune_keeps_an_entry_declared_again_since_it_was_listed() -> None:
    automation = RuleAutomation(rules={"back": RuleEntry("Back.", "rejected")})
    listed = orphans(automation, frozenset())

    pruned = prune(automation, listed, frozenset({"back"}))

    assert set(pruned.rules) == {"back"}
    assert not orphans(automation, frozenset({"back"}))
