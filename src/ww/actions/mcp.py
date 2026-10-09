# SPDX-License-Identifier: GPL-3.0-or-later
"""MCP action."""

from __future__ import annotations

from typing import Any

from ww.errors import ConfigurationError
from ww.interpolation import interpolate
from ww.validation import expect_keys, expect_string

from .contracts import (
    Action,
    InstructionContent,
    InstructionContext,
    Mcp,
    ResolutionContext,
)


class McpAction(Action[Mcp, Mcp]):
    identifier = "mcp"
    planned_type = Mcp

    def parse(
        self, source: dict[str, Any], name: str, description: str, path: str
    ) -> Mcp:
        connection = source.get("mcp")
        if not isinstance(connection, str) or not connection.strip():
            raise ConfigurationError(f"{path} mcp must be non-empty")
        return Mcp(connection, description or name)

    def validate(self, definition: Mcp, path: str) -> None:
        if not definition.connection or not definition.prompt:
            raise ConfigurationError(f"{path} MCP requires a connection and prompt")

    def templates(self, definition: Mcp) -> tuple[str, ...]:
        return (definition.connection, definition.prompt)

    def plan(self, definition: Mcp, context: ResolutionContext) -> Mcp:
        return Mcp(
            context.interpolate(definition.connection),
            context.interpolate(definition.prompt),
        )

    def instruction(
        self, planned: Mcp, context: InstructionContext
    ) -> InstructionContent:
        prompt = (
            interpolate(planned.prompt, context.task_values)
            if context.task_values
            else planned.prompt
        )
        text = (
            f"For the following work use `{planned.connection}` mcp connection:\n"
            f"{prompt or context.description or context.name}\n\n"
            "If your result from the MCP call is erroneous, do not proceed to "
            "the next step. Use the `fail` command to register that and notify "
            "the caller of this task."
        )
        return InstructionContent(
            text,
            (
                "**Prompt**",
                "",
                f"For the following work use `{planned.connection}` mcp connection:",
                "",
                prompt,
                "",
            ),
        )

    def encode(self, planned: Mcp) -> dict[str, object]:
        return {"connection": planned.connection, "prompt": planned.prompt}

    def decode(self, data: dict[str, Any]) -> Mcp:
        expect_keys(
            data, {"connection", "prompt"}, f"action {self.identifier!r} payload"
        )
        return Mcp(
            expect_string(data["connection"], "MCP connection"),
            expect_string(data["prompt"], "MCP prompt"),
        )
