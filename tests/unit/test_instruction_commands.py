# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the command lines rendered into instructions."""

from __future__ import annotations

from ww.instructions.commands import (
    instruction_command,
    recovery_commands,
    start_child_command,
)


def test_placeholders_stay_bare_while_real_values_are_quoted() -> None:
    assert (
        instruction_command("TASK-1", "<run-id>", role="manager")
        == "./ww instruction TASK-1 --run <run-id> --role manager"
    )
    assert (
        start_child_command("TASK-1", "<odd id>")
        == "./ww start-child TASK-1 '<odd id>'"
    )
    assert (
        recovery_commands("TASK-1")[1].command
        == './ww next TASK-1 --force --reason "<reason>" --yes --role manager'
    )
