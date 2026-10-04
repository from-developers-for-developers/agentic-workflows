# SPDX-License-Identifier: GPL-3.0-or-later
"""``limits.auto_retries``: ww retries a failed automatic step before stopping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.cli import main
from ww.errors import ConfigurationError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "T1"
# Succeeds from the ``succeed_on``-th run on; counts its runs in ``count``.
FLAKY = (
    "n=$(cat count 2>/dev/null || echo 0); n=$((n+1)); echo $n > count; "
    'echo "attempt $n failed"; test $n -ge {succeed_on}'
)


def _service(
    tmp_path: Path, succeed_on: int, *, retries: int | None, policy: str = "operator"
) -> WorkflowService:
    (tmp_path / "ww.yaml").write_text(
        f"""workflows:
  - name: task
    steps:
      - prepare: Prepare the work.
      - flaky: ~
        shell: '{FLAKY.format(succeed_on=succeed_on)}'
        on_failure: {policy}
      - finish: Finish the work.
""",
        encoding="utf-8",
    )
    if retries is not None:
        (tmp_path / "ww.json").write_text(
            json.dumps({"limits": {"auto_retries": retries}}), encoding="utf-8"
        )
    service = WorkflowService(Storage(tmp_path))
    service.start("task", TASK, agent="codex", init_artifact="Do it.")
    service.next(TASK)
    return service


def _complete_prepare(service: WorkflowService):
    return service.complete(TASK, artifact="Prepared.", summary_for_next="Ready.")


def _count(tmp_path: Path) -> int:
    return int((tmp_path / "count").read_text())


def test_without_retries_the_first_failure_stops_the_task(tmp_path: Path) -> None:
    service = _service(tmp_path, succeed_on=2, retries=None)
    stopped = _complete_prepare(service)
    assert stopped.operator_reason is not None
    assert _count(tmp_path) == 1
    assert "retried this step" not in (stopped.error or "")


def test_a_step_that_succeeds_on_the_third_attempt_needs_no_operator(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, succeed_on=3, retries=2)
    page = _complete_prepare(service)
    assert page.operator_reason is None
    assert page.item_name == "finish"
    assert _count(tmp_path) == 3
    state, snapshot = service.load(TASK)
    record = next(
        record
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.name == "flaky"
    )
    assert record.status == "completed"
    assert len(record.retry_errors) == 2
    assert all("automatic handler failed (1)" in e for e in record.retry_errors)
    assert all("attempt" in e for e in record.retry_errors)
    assert record.attempts == 3


def test_a_step_that_keeps_failing_reaches_the_operator_after_the_retries(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, succeed_on=99, retries=2)
    stopped = _complete_prepare(service)
    assert stopped.operator_reason is not None
    assert _count(tmp_path) == 3
    assert "ww retried this step 2 time(s) itself" in (stopped.error or "")
    assert "attempt 1:" in (stopped.error or "")
    page = MarkdownOutputAdapter().render_instruction(service.instruction(TASK))
    assert "ww retried this step 2 time(s) itself" in page
    # An operator retry starts the automatic attempts over.
    retried = service.next(TASK, retry=True)
    assert _count(tmp_path) == 6
    assert "ww retried this step 2 time(s) itself" in (retried.error or "")


def test_retries_come_before_a_repair_assignment(tmp_path: Path) -> None:
    service = _service(tmp_path, succeed_on=99, retries=2, policy="fix")
    page = _complete_prepare(service)
    assert page.handler_repair is not None
    assert _count(tmp_path) == 3
    assert "ww retried this step 2 time(s) itself" in (page.action_text or "")


def test_negative_retries_are_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ww.json").write_text(
        json.dumps({"limits": {"auto_retries": -1}}), encoding="utf-8"
    )
    (tmp_path / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - a: Do it.\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="non-negative integer"):
        WorkflowService(Storage(tmp_path)).start("task", TASK, agent="codex")
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "start",
                "-w",
                "task",
                "-a",
                "codex",
                "--requirements",
                "x",
            ]
        )
        != 0
    )
    assert "non-negative integer" in capsys.readouterr().err
