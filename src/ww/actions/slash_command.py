# SPDX-License-Identifier: GPL-3.0-or-later
"""Slash-command action."""

from __future__ import annotations

from typing import Any

from ww.errors import ConfigurationError
from ww.validation import expect_keys, expect_string

from .contracts import (
    Action,
    InstructionContent,
    InstructionContext,
    ResolutionContext,
    SlashCommand,
)


class SlashCommandAction(Action[SlashCommand, SlashCommand]):
    identifier = "slash_command"
    planned_type = SlashCommand

    def parse(
        self, source: dict[str, Any], name: str, description: str, path: str
    ) -> SlashCommand:
        # ``kind: slash_command`` already chose this action; the command is
        # the handler's name.
        return SlashCommand(name)

    def validate(self, definition: SlashCommand, path: str) -> None:
        if not definition.name:
            raise ConfigurationError(f"{path} slash command requires a name")

    def templates(self, definition: SlashCommand) -> tuple[str, ...]:
        return ()

    def plan(
        self, definition: SlashCommand, context: ResolutionContext
    ) -> SlashCommand:
        if definition.name not in context.available.slash_commands:
            raise ConfigurationError(
                f"configured slash command not found for {context.agent}: "
                f"{definition.name}"
            )
        return SlashCommand(context.interpolate(definition.name))

    def instruction(
        self, planned: SlashCommand, context: InstructionContext
    ) -> InstructionContent:
        text = f"Run the `/{planned.name}` slash command." + (
            f" {context.description}" if context.description else ""
        )
        return InstructionContent(
            text, ("**Run slash command**", "", f"`/{planned.name}`", "")
        )

    def encode(self, planned: SlashCommand) -> dict[str, object]:
        return {"name": planned.name}

    def decode(self, data: dict[str, Any]) -> SlashCommand:
        expect_keys(data, {"name"}, f"action {self.identifier!r} payload")
        return SlashCommand(expect_string(data["name"], "slash command name"))
