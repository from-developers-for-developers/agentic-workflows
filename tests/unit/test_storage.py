# SPDX-License-Identifier: GPL-3.0-or-later
"""Project storage: execution logs and their rotation."""

import json
from pathlib import Path
from stat import S_IMODE

import ww.storage as storage_module
from ww.storage import Storage


def test_execution_log_rotation_retains_a_bounded_history(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(storage_module, "EXECUTION_LOG_MAX_BYTES", 80)
    monkeypatch.setattr(storage_module, "EXECUTION_LOG_RETENTION", 2)
    storage = Storage(tmp_path)

    for invocation in range(4):
        storage.append_log({"invocation_id": str(invocation), "detail": "x" * 40})

    logs = tmp_path / ".ww"
    assert json.loads((logs / "executions.jsonl").read_text())["invocation_id"] == "3"
    assert json.loads((logs / "executions.jsonl.1").read_text())["invocation_id"] == "2"
    assert json.loads((logs / "executions.jsonl.2").read_text())["invocation_id"] == "1"
    assert not (logs / "executions.jsonl.3").exists()


def test_execution_logs_are_owner_readable_even_when_an_older_log_is_not(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path)
    log = tmp_path / ".ww" / "executions.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"existing": true}\n', encoding="utf-8")
    log.chmod(0o644)

    storage.append_log({"invocation_id": "new"})

    assert S_IMODE(log.stat().st_mode) == 0o600
