# SPDX-License-Identifier: GPL-3.0-or-later
"""Skill action."""

from __future__ import annotations

from typing import Any

from ww.errors import ConfigurationError
from ww.validation import expect_keys, expect_string

from .contracts import (
    Action,
    ActionTraits,
    InstructionContent,
    InstructionContext,
    ResolutionContext,
    Skill,
)


class SkillAction(Action[Skill, Skill]):
    identifier = "skill"
    planned_type = Skill

    def parse(
        self, source: dict[str, Any], name: str, description: str, path: str
    ) -> Skill:
        # ``kind: skill`` (or ``action: {type: skill}``) already chose this
        # action; the skill is the handler's name.
        return Skill(name)

    def validate(self, definition: Skill, path: str) -> None:
        if not definition.name:
            raise ConfigurationError(f"{path} skill requires a name")

    def templates(self, definition: Skill) -> tuple[str, ...]:
        return ()

    def plan(self, definition: Skill, context: ResolutionContext) -> Skill:
        if definition.name not in context.available.skills:
            raise ConfigurationError(
                f"configured skill not found for {context.agent}: {definition.name}"
            )
        return Skill(context.interpolate(definition.name))

    def instruction(
        self, planned: Skill, context: InstructionContext
    ) -> InstructionContent:
        text = f"Use the `{planned.name}` skill." + (
            f" {context.description}" if context.description else ""
        )
        return InstructionContent(text, ("**Use skill**", "", f"`{planned.name}`", ""))

    def traits(self, planned: Skill) -> ActionTraits:
        return ActionTraits(artifact_attribution=planned.name)

    def encode(self, planned: Skill) -> dict[str, object]:
        return {"name": planned.name}

    def decode(self, data: dict[str, Any]) -> Skill:
        expect_keys(data, {"name"}, f"action {self.identifier!r} payload")
        return Skill(expect_string(data["name"], "skill name"))
