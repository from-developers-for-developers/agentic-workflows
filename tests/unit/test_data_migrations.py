# SPDX-License-Identifier: GPL-3.0-or-later
"""Load-time migrations of persisted data: the chain, its limits, its loaders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ww import __version__
from ww.errors import StateError
from ww.migrations import data as migrations
from ww.migrations.data import (
    FORMATS,
    WRITTEN_BY,
    DataFormat,
    UnsupportedSchemaError,
    upgrade,
)
from ww.rule_disputes import DISPUTES_FILE, DisputeLog


def _rename_note(document: dict[str, Any]) -> dict[str, Any]:
    """A test-only migration 1 -> 2: ``note`` became ``reason``."""
    upgraded = {key: value for key, value in document.items() if key != "note"}
    upgraded["reason"] = document.get("note")
    return upgraded


def _count_reasons(document: dict[str, Any]) -> dict[str, Any]:
    """A test-only migration 2 -> 3: a ``reasons`` count joins ``reason``."""
    return {**document, "reasons": 1 if document.get("reason") else 0}


EXAMPLE = DataFormat(
    "rule dispute log",
    current=3,
    migrations={1: _rename_note, 2: _count_reasons},
)


def test_a_current_mapping_is_returned_as_it_is() -> None:
    document = {"schema_version": 3, "reason": "x"}

    assert EXAMPLE.upgrade(document) is document


def test_an_older_mapping_runs_every_step_of_the_chain_without_changing_it() -> None:
    document = {"schema_version": 1, WRITTEN_BY: "1.0.0", "note": "why"}

    upgraded = EXAMPLE.upgrade(document)

    assert upgraded == {
        "schema_version": 3,
        WRITTEN_BY: "1.0.0",
        "reason": "why",
        "reasons": 1,
    }
    assert document == {"schema_version": 1, WRITTEN_BY: "1.0.0", "note": "why"}
    assert EXAMPLE.upgrade({"schema_version": 2, "reason": None})["reasons"] == 0


def test_a_mapping_a_newer_ww_wrote_asks_for_an_upgrade() -> None:
    with pytest.raises(UnsupportedSchemaError) as raised:
        EXAMPLE.upgrade({"schema_version": 4, WRITTEN_BY: "2.1.0"})

    assert raised.value.newer
    assert str(raised.value) == (
        "rule dispute log written by ww 2.1.0 (schema 4); this ww reads up to "
        "schema 3: upgrade ww"
    )


def test_a_higher_schema_without_a_writer_is_a_beta_from_before_renumbering() -> None:
    with pytest.raises(UnsupportedSchemaError) as raised:
        EXAMPLE.upgrade({"schema_version": 4})

    assert not raised.value.newer
    assert str(raised.value) == (
        "rule dispute log written by a beta ww from before the schema versions "
        "were renumbered (schema 4); finish or reset it with that ww"
    )


def test_a_nested_record_at_a_higher_schema_asks_for_an_upgrade() -> None:
    # A nested record never carries ``written_by``; its enclosing file names
    # the writer and was checked first, so a higher schema is a newer ww.
    with pytest.raises(UnsupportedSchemaError) as raised:
        upgrade("plan snapshot", {"schema_version": 3})

    assert raised.value.newer
    assert str(raised.value) == (
        "plan snapshot written by a newer ww (schema 3); this ww reads up to "
        "schema 2: upgrade ww"
    )


def test_a_mapping_older_than_the_chain_names_the_ww_to_finish_with() -> None:
    retired = DataFormat(
        "rule dispute log", current=3, oldest=2, migrations={2: _count_reasons}
    )

    with pytest.raises(UnsupportedSchemaError) as raised:
        retired.upgrade({"schema_version": 1, WRITTEN_BY: "1.0.0"})

    assert not raised.value.newer
    assert str(raised.value) == (
        "rule dispute log written by ww 1.0.0 (schema 1); this ww reads schema 2 "
        "and later: finish or reset it with ww 1.0.0"
    )
    with pytest.raises(UnsupportedSchemaError, match="the older ww that wrote it"):
        retired.upgrade({"schema_version": 1})


@pytest.mark.parametrize("version", [None, True, 1.0, "1"])
def test_a_version_that_is_not_an_integer_is_refused(version: object) -> None:
    with pytest.raises(ValueError, match="unsupported rule dispute log schema"):
        EXAMPLE.upgrade({"schema_version": version})


def test_a_format_reads_its_own_version_key() -> None:
    stats = DataFormat(
        "rule stats", current=2, version_key="schema", migrations={1: dict}
    )

    assert stats.upgrade({"schema": 1, "rules": {}}) == {"schema": 2, "rules": {}}
    assert stats.header() == {"schema": 2, WRITTEN_BY: __version__}


@pytest.mark.parametrize(
    ("oldest", "steps"),
    [
        (1, {1: dict}),
        (1, {1: dict, 2: dict, 3: dict}),
        (2, {1: dict, 2: dict}),
        (4, {}),
    ],
)
def test_a_chain_must_cover_every_step_exactly(
    oldest: int, steps: dict[int, Any]
) -> None:
    with pytest.raises(ValueError, match="rule stats"):
        DataFormat("rule stats", current=3, oldest=oldest, migrations=steps)


def test_every_registered_format_is_current_at_its_declared_version() -> None:
    for name, data_format in FORMATS.items():
        assert data_format.name == name
        current = {data_format.version_key: data_format.current}
        assert upgrade(data_format.name, current) is current


def test_a_loader_migrates_through_a_registered_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispute log, at a test-only version 2, still loads a version 1 file
    and writes the current version back with its own ``written_by``."""

    def reason_from_note(document: dict[str, Any]) -> dict[str, Any]:
        return {
            **document,
            "disputes": [
                {
                    **{key: value for key, value in entry.items() if key != "note"},
                    "reason": entry["note"],
                }
                for entry in document["disputes"]
            ],
        }

    monkeypatch.setitem(
        migrations.FORMATS,
        "rule dispute log",
        DataFormat("rule dispute log", current=2, migrations={1: reason_from_note}),
    )
    path = tmp_path / DISPUTES_FILE
    path.parent.mkdir(parents=True)
    entry = {
        "check": "lint",
        "task_id": "TASK-1",
        "step": "develop",
        "note": "Generated file.",
        "attempt": 1,
        "disputed_at": "2026-11-01T00:00:00Z",
    }
    path.write_text(
        json.dumps({"schema_version": 1, "disputes": [entry]}), encoding="utf-8"
    )
    log = DisputeLog(tmp_path)

    (loaded,) = log.load()
    assert loaded.reason == "Generated file."

    log.append(loaded)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["schema_version"] == 2
    assert written[WRITTEN_BY] == __version__
    assert [item["reason"] for item in written["disputes"]] == ["Generated file."] * 2


def test_a_loader_refuses_a_file_a_newer_ww_wrote(tmp_path: Path) -> None:
    path = tmp_path / DISPUTES_FILE
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema_version": 7, WRITTEN_BY: "3.0.0", "disputes": []}),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match=r"ww 3\.0\.0 \(schema 7\).*: upgrade ww"):
        DisputeLog(tmp_path).load()


def test_a_plan_snapshot_1_lists_every_items_artifact_dependency() -> None:
    def item(dependency: str | None, **nested: object) -> dict[str, object]:
        return {"id": "x", "artifact_dependency": dependency, **nested}

    verification = {"verifier": [item("develop")], "on_failure": [item(None)]}
    snapshot = {
        "schema_version": 1,
        "plan": {"items": [item(None), item("research", verification_plan=None)]},
        "template_plan": {
            "items": [item("plan", verification_plan=verification)],
        },
    }

    upgraded = upgrade("plan snapshot", snapshot)

    assert upgraded["schema_version"] == 2
    assert [entry["artifact_dependencies"] for entry in upgraded["plan"]["items"]] == [
        [],
        ["research"],
    ]
    template = upgraded["template_plan"]["items"][0]
    assert template["artifact_dependencies"] == ["plan"]
    assert "artifact_dependency" not in template
    assert template["verification_plan"]["verifier"][0]["artifact_dependencies"] == [
        "develop"
    ]
    assert template["verification_plan"]["on_failure"][0]["artifact_dependencies"] == []
    assert snapshot["plan"]["items"][1]["artifact_dependency"] == "research"
