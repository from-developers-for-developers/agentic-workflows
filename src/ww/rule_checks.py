# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a step's checks against the files it changed.

A check is a planned command: a rule's own ``check``, a ``before_complete``
hook with ``on_failure: fix``, or a derived check the operator approved into
the rule-automation store, resolved when the step began. ww runs every check
of the completing step in plan order, derived checks last, each seeing the
step's change set in ``WW_STEP_CHANGED_FILES`` (newline-separated, relative
to the step's directory) narrowed to the check's globs. A check whose globs
select no changed file is not applicable and does not run. A check fails on
a non-zero exit or a failed assertion.

Checks read the working tree and report; they change no workflow state, so
running them again after an interruption is harmless and they keep no
command ledger. Their full output is stored as command-output artifacts.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ww.action_execution import STATE_OUTPUT_PREVIEW_LIMIT, bounded
from ww.actions.command import CommandAction
from ww.changes import all_files, changed_files, select_files, take_mark
from ww.errors import StateError
from ww.execution_models import CheckReport, CheckResult, ExecutionState
from ww.plan import PlanItem, PlannedCheck
from ww.storage_adapters import CommandOutputAddress

CHANGED_FILES_VARIABLE = "WW_STEP_CHANGED_FILES"
# Lines of a check's output kept in state and shown on the fix page.
OUTPUT_TAIL_LINES = 40

WriteOutput = Callable[[CommandOutputAddress, str], str]


@dataclass(frozen=True)
class CheckScope:
    """Where the checks of one completing item run, and with which values."""

    directory: Path
    values: dict[str, str]


class RuleChecker:
    """Run the checks of one agent item and report every outcome.

    Without ``write_output`` the full streams are not kept: ``ww check``
    previews a completion and leaves nothing behind.
    """

    def __init__(
        self, write_output: WriteOutput | None, now: Callable[[], str]
    ) -> None:
        self.write_output = write_output
        self.now = now

    def run(
        self,
        state: ExecutionState,
        item: PlanItem,
        scope: CheckScope,
        *,
        reuse: CheckReport | None = None,
    ) -> CheckReport:
        """Run the item's planned checks, then the derived ones it resolved.

        ``reuse`` is an earlier passing report of the same completion: while
        the working tree is still at the tree that report was measured to,
        the checks it passed are not run again. A check the operator waived
        for this step does not run and is not in the report.
        """
        record = state.item_executions[state.cursor]
        waived = dict(record.checks_waived)
        attempt = max((report.attempt for report in item_reports(state)), default=0) + 1
        mark_b = take_mark(scope.directory) if record.change_mark else None
        files, unmarked = change_set(scope.directory, record.change_mark, mark_b)
        kept = (
            {result.id: result for result in reuse.results if result.status != "failed"}
            if reuse is not None and mark_b is not None and reuse.mark == mark_b
            else {}
        )
        results = tuple(
            kept.get(check.id)
            or self._run_check(state, item, check, index, attempt, files, scope)
            for index, check in enumerate((*item.checks, *record.resolved_checks), 1)
            if check.id not in waived
        )
        return CheckReport(
            attempt=attempt,
            checked_at=self.now(),
            results=results,
            mark=mark_b,
            all_files=unmarked,
        )

    def _run_check(
        self,
        state: ExecutionState,
        item: PlanItem,
        check: PlannedCheck,
        index: int,
        attempt: int,
        files: tuple[str, ...],
        scope: CheckScope,
    ) -> CheckResult:
        selected = select_files(files, check.paths, check.contains, scope.directory)
        if (check.paths or check.contains) and not selected:
            return CheckResult(check.id, check.source, "not_applicable")
        action = CommandAction()
        outputs: list[str] = []
        stdout_ref = stderr_ref = None
        exit_code: int | None = 0
        shown: list[str] = []
        for segment, command in enumerate(check.command.commands, 1):
            try:
                argv, environment = action.render_command(command, scope.values)
            except StateError as error:
                return CheckResult(check.id, check.source, "failed", output=str(error))
            shown.append(
                command.shell if command.shell is not None else shlex.join(argv)
            )
            try:
                completed = subprocess.run(
                    argv,
                    cwd=scope.directory,
                    capture_output=True,
                    text=True,
                    check=False,
                    shell=False,
                    env={
                        **os.environ,
                        **environment,
                        CHANGED_FILES_VARIABLE: "\n".join(selected),
                    },
                )
            except OSError as error:
                return CheckResult(
                    check.id,
                    check.source,
                    "failed",
                    command="\n".join(shown),
                    output=f"could not launch the command: {error}",
                )
            address = CommandOutputAddress(
                state.task_id,
                state.run_id or state.workflow,
                item.id,
                f"{state.item_executions[state.cursor].operation_id}:check:{check.id}",
                attempt,
                segment,
                "stdout",
            )
            if completed.stdout and self.write_output is not None:
                stdout_ref = self.write_output(address, completed.stdout)
            if completed.stderr and self.write_output is not None:
                stderr_ref = self.write_output(
                    replace(address, stream="stderr"), completed.stderr
                )
            outputs.append(completed.stdout)
            exit_code = completed.returncode
            if completed.returncode != 0:
                return CheckResult(
                    check.id,
                    check.source,
                    "failed",
                    command="\n".join(shown),
                    output=_tail(completed.stdout + completed.stderr),
                    exit_code=completed.returncode,
                    stdout_ref=stdout_ref,
                    stderr_ref=stderr_ref,
                )
        output = "\n".join(part for part in outputs if part).strip()
        assertion = check.command.assertion
        passed = assertion is None or assertion.holds(output)
        return CheckResult(
            check.id,
            check.source,
            "passed" if passed else "failed",
            command="\n".join(shown),
            output="" if passed else _tail(output),
            exit_code=exit_code,
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
        )


def change_set(
    directory: Path, mark_a: str | None, mark_b: str | None
) -> tuple[tuple[str, ...], bool]:
    """The files changed between two marks, or every file when unmarked.

    The flag is true without a change set: no git, or state written before
    the step took its mark.
    """
    if mark_a and mark_b:
        return changed_files(directory, mark_a, mark_b), False
    return all_files(directory), True


def item_reports(state: ExecutionState) -> tuple[CheckReport, ...]:
    """Every check report of the current item's operation, retries included.

    An operator retry moves a copy of the record into the history, so a
    report may appear twice; attempts number the reports of one operation,
    and each attempt is one report.
    """
    record = state.item_executions[state.cursor]
    kept = [
        entry
        for entry in state.execution_history
        if entry.operation_id is not None and entry.operation_id == record.operation_id
    ]
    unique: dict[int, CheckReport] = {}
    for entry in (*kept, record):
        for report in entry.check_reports:
            unique.setdefault(report.attempt, report)
    return tuple(unique[attempt] for attempt in sorted(unique))


def _tail(output: str) -> str:
    """The last lines of a check's output, bounded for state and the page."""
    lines = output.strip().splitlines()
    kept = "\n".join(lines[-OUTPUT_TAIL_LINES:])
    return bounded(kept, STATE_OUTPUT_PREVIEW_LIMIT * 4)
