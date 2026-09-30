# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the command lines rendered into instructions."""

from __future__ import annotations

from ww.instructions.commands import (
    add_item_command,
    instruction_command,
    item_command,
    recovery_commands,
    update_item_command,
)


def test_placeholders_stay_bare_while_real_values_are_quoted() -> None:
    assert add_item_command() == "./ww add-item <task-id> --id <id> --text <text>"
    assert (
        instruction_command("TASK-1", "<run-id>", role="manager")
        == "./ww instruction TASK-1 --run <run-id> --role manager"
    )
    assert item_command("TASK-1", "<odd id>") == "./ww item TASK-1 --id '<odd id>'"
    assert item_command("TASK-1", '"quoted"') == "./ww item TASK-1 --id '\"quoted\"'"
    assert (
        recovery_commands("TASK-1")[1].command
        == './ww next TASK-1 --force --reason "<reason>" --yes --role manager'
    )


def test_item_update_keeps_its_literal_flag_placeholders() -> None:
    assert update_item_command("TASK-1", "<odd id>", "process_item") == (
        "./ww update-item TASK-1 --id '<odd id>' "
        '--processed-item="<agent-friendly analysis>"'
    )
