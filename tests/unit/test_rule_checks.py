# SPDX-License-Identifier: GPL-3.0-or-later
"""A check that cannot run is unavailable: how results classify and count."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ww.execution_models import CheckReport, CheckResult, PlanItemExecution
from ww.rule_checks import _unavailable_reason, missing_file

NOW = "2026-10-08T12:00:00Z"


def test_missing_file_names_the_first_file_the_directory_lacks(
    tmp_path: Path,
) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/lint.sh").write_text("", encoding="utf-8")

    assert missing_file((), tmp_path) is None
    assert missing_file(("tools/lint.sh",), tmp_path) is None
    assert missing_file(("tools/lint.sh", "tools/lint.toml"), tmp_path) == (
        "tools/lint.toml"
    )
    # A path reaching outside the directory is missing however it resolves.
    assert missing_file(("../lint.sh",), tmp_path / "tools") == "../lint.sh"
    assert missing_file((str(tmp_path / "tools/lint.sh"),), tmp_path) == str(
        tmp_path / "tools/lint.sh"
    )
    # Without a directory nothing can be missing: ``ww check`` on a bare plan.
    assert missing_file(("tools/lint.sh",), None) is None


def test_the_unavailable_reason_names_the_exit_and_the_last_line() -> None:
    not_found = subprocess.CompletedProcess(
        ["sh"], 127, stdout="", stderr="sh: tools/lint.sh: not found\n"
    )
    silent = subprocess.CompletedProcess(["sh"], 126, stdout="", stderr="")

    assert _unavailable_reason(not_found) == (
        "command not found (exit 127): sh: tools/lint.sh: not found"
    )
    assert _unavailable_reason(silent) == "the command is not executable (exit 126)"


def _report(attempt: int, *results: CheckResult) -> CheckReport:
    return CheckReport(attempt, NOW, results)


def test_an_unavailable_result_is_neither_failed_nor_counted() -> None:
    unavailable = CheckResult(
        "docs/lint", "rule", "unavailable", output="command not found (exit 127)"
    )
    failed = CheckResult("docs/header", "rule", "failed", output="notes.md")
    record = PlanItemExecution(
        "task:develop",
        2,
        check_reports=(
            _report(1, unavailable, failed),
            _report(2, unavailable, failed),
        ),
    )

    report = record.check_reports[-1]
    assert report.failed == (failed,)
    assert report.unavailable == (unavailable,)
    assert record.check_failures("docs/header") == 2
    assert record.check_failures("docs/lint") == 0
    assert CheckReport.from_dict(report.to_dict()) == report
