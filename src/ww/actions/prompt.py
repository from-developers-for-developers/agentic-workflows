# SPDX-License-Identifier: GPL-3.0-or-later
"""Prompt action."""

from __future__ import annotations

from typing import Any

from ww.errors import ConfigurationError
from ww.interpolation import interpolate
from ww.validation import expect_keys, expect_string

from .contracts import (
    Action,
    DefinitionOverrideContext,
    InstructionContent,
    InstructionContext,
    Prompt,
    ResolutionContext,
)


class PromptAction(Action[Prompt, Prompt]):
    identifier = "prompt"
    planned_type = Prompt

    def parse(
        self, source: dict[str, Any], name: str, description: str, path: str
    ) -> Prompt:
        return Prompt(description or name)

    def validate(self, definition: Prompt, path: str) -> None:
        if not definition.text:
            raise ConfigurationError(f"{path} prompt requires text")

    def override_definition(
        self,
        local: Prompt,
        inherited: Prompt,
        context: DefinitionOverrideContext,
    ) -> Prompt:
        """A prompt step inherits catalog text until it supplies a description."""
        return Prompt(
            context.local_description or context.local_name
            if context.description_is_explicit
            else inherited.text
        )

    def templates(self, definition: Prompt) -> tuple[str, ...]:
        return (definition.text,)

    def plan(self, definition: Prompt, context: ResolutionContext) -> Prompt:
        return Prompt(context.interpolate(definition.text))

    def instruction(
        self, planned: Prompt, context: InstructionContext
    ) -> InstructionContent:
        text = (
            interpolate(planned.text, context.task_values)
            if context.task_values
            else planned.text
        )
        return InstructionContent(
            text or context.description or context.name,
            ("**Prompt**", "", text, ""),
            show_context=text != context.description,
        )

    def encode(self, planned: Prompt) -> dict[str, object]:
        return {"text": planned.text}

    def decode(self, data: dict[str, Any]) -> Prompt:
        expect_keys(data, {"text"}, f"action {self.identifier!r} payload")
        return Prompt(expect_string(data["text"], "prompt text"))
