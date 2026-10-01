# SPDX-License-Identifier: GPL-3.0-or-later
"""What the operator is told when an automatic handler fails."""

from __future__ import annotations

from ww.actions.command import _failure_message, _tail
from ww.actions.contracts import CommandOutcome


def test_diagnostics_printed_to_stdout_reach_the_operator() -> None:
    """pytest, ruff and mypy all report on stdout, not stderr."""
    message = _failure_message(
        ("python", "-m", "pytest"),
        0,
        CommandOutcome(ok=False, stdout="2 failed, 1 passed", exit_code=1),
    )

    assert "2 failed, 1 passed" in message
    assert "python -m pytest" in message
    assert "(1)" in message


def test_stderr_is_preferred_when_both_streams_spoke() -> None:
    message = _failure_message(
        ("ruff", "check"),
        0,
        CommandOutcome(ok=False, stdout="noise", stderr="the real error", exit_code=2),
    )

    assert "the real error" in message
    assert "noise" not in message


def test_a_silent_failure_says_where_to_look() -> None:
    message = _failure_message(("false",), 0, CommandOutcome(ok=False, exit_code=1))

    assert "printed nothing" in message
    assert "artifact" in message


def test_the_command_is_quoted_so_it_can_be_rerun() -> None:
    message = _failure_message(
        ("sh", "-c", "exit 3"), 0, CommandOutcome(ok=False, exit_code=3)
    )

    assert "sh -c 'exit 3'" in message


def test_a_resumed_segment_without_a_rendered_command_still_reports() -> None:
    """After an interruption the command is read back, not re-rendered."""
    message = _failure_message(
        (), 2, CommandOutcome(ok=False, stdout="boom", exit_code=1)
    )

    assert "at command 3" in message
    assert "boom" in message


def test_only_the_tail_of_a_long_output_is_shown() -> None:
    kept = _tail("\n".join(str(number) for number in range(200)), limit=10)

    assert kept.splitlines()[0].startswith("[190 earlier line(s) omitted")
    assert kept.splitlines()[-1] == "199"
    assert "100" not in kept.splitlines()[1:]


def test_short_output_is_left_whole() -> None:
    assert _tail("one\ntwo") == "one\ntwo"
