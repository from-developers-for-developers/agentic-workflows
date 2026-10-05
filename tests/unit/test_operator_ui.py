# SPDX-License-Identifier: GPL-3.0-or-later
"""The operator page's sheet file, port, wait, and binding rules."""

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from ww.errors import StateError
from ww.operator_ui import server
from ww.operator_ui.server import (
    operator_page_port,
    operator_wait_seconds,
    serve_operator_page,
)
from ww.operator_ui.sheet import Answer, AnswerSheet
from ww.storage import Storage


def test_answers_are_recorded_replaced_and_removed_per_run(tmp_path: Path) -> None:
    sheet = AnswerSheet(Storage(tmp_path), "TASK-1", "2026-09-25T10:00:00Z")
    assert sheet.read("01-task") == {}
    sheet.record("01-task", "case-1", Answer("pass", "", "t1"))
    sheet.record("01-task", "case-2", Answer(None, "Broken.", "t2"))
    sheet.record("01-task", "case-1", Answer("fail", "Nope.", "t3"))
    sheet.record("02-task", "case-1", Answer("pass", "", "t4"))

    assert sheet.read("01-task") == {
        "case-1": Answer("fail", "Nope.", "t3"),
        "case-2": Answer(None, "Broken.", "t2"),
    }
    assert sheet.read("02-task") == {"case-1": Answer("pass", "", "t4")}
    sheet.remove("01-task", "case-1")
    sheet.remove("01-task", "missing")
    assert list(sheet.read("01-task")) == ["case-2"]

    path = tmp_path / ".ww/operator-ui/TASK-1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format"] == "ww.operator-ui"
    assert data["task_created_at"] == "2026-09-25T10:00:00Z"
    assert data["runs"]["01-task"]["case-2"] == {
        "choice": None,
        "comment": "Broken.",
        "at": "t2",
    }


def test_a_sheet_of_a_reset_task_is_discarded(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    AnswerSheet(storage, "TASK-1", "before").record(
        "01-task", "case-1", Answer("pass", "", "t1")
    )
    later = AnswerSheet(storage, "TASK-1", "after")
    assert later.read("01-task") == {}
    later.record("01-task", "case-2", Answer("fail", "", "t2"))
    assert list(later.read("01-task")) == ["case-2"]


def test_a_foreign_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / ".ww/operator-ui/TASK-1.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"runs": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="is not an operator page answer sheet"):
        AnswerSheet(Storage(tmp_path), "TASK-1", "x").read("01-task")


def test_the_port_is_stable_per_task_and_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WW_OPERATOR_PORT", raising=False)
    assert operator_page_port("TASK-1") == operator_page_port("TASK-1")
    assert 40000 <= operator_page_port("TASK-1") < 50000
    monkeypatch.setenv("WW_OPERATOR_PORT", "8123")
    assert operator_page_port("TASK-1") == 8123
    monkeypatch.setenv("WW_OPERATOR_PORT", "eighty")
    with pytest.raises(StateError, match="must be a port number"):
        operator_page_port("TASK-1")


def test_the_wait_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WW_OPERATOR_WAIT", raising=False)
    assert operator_wait_seconds() == 90.0
    monkeypatch.setenv("WW_OPERATOR_WAIT", "2.5")
    assert operator_wait_seconds() == 2.5
    monkeypatch.setenv("WW_OPERATOR_WAIT", "soon")
    with pytest.raises(StateError, match="number of seconds"):
        operator_wait_seconds()


def test_a_busy_port_is_reported_not_hung(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_BIND_RETRY_SECONDS", 0.2)
    monkeypatch.setattr(server, "_BIND_RETRY_INTERVAL", 0.05)
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        with pytest.raises(StateError, match=f"cannot listen on port {port}"):
            serve_operator_page(
                port=port, state=dict, act=lambda _: None, timeout=1, open_browser=None
            )


def test_a_poll_that_follows_a_closing_notice_always_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The waiter is slow to wake: the tab says it is closing and polls again
    # (a reload) before the waiter looks.  The poll came after the notice, so
    # the wait must not end as closed under the live tab.
    monkeypatch.setattr(server, "_CLOSING_GRACE", 0.2)
    page = server._Server(0, dict, lambda _: None, "")
    thread = threading.Thread(target=page.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{page.server_address[1]}"
        request = urllib.request.Request(f"{base}/closing", data=b"", method="POST")
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
        with urllib.request.urlopen(f"{base}/state") as response:
            assert response.status == 200
        outcome = server._wait(page, time.monotonic() + 0.5)
    finally:
        page.shutdown()
        page.server_close()
        thread.join()
    assert outcome == "timed_out"
