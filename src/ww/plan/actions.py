# SPDX-License-Identifier: GPL-3.0-or-later
"""Handler references and registry-based action planning."""

from __future__ import annotations

from dataclasses import replace

from ww.actions import (
    ActionPayload,
    DefinedAction,
    Extension,
    Prompt,
    ResolutionContext,
    Skill,
    SlashCommand,
    actions,
)
from ww.contracts import PlanItemKind, PlanItemOwner
from ww.discovery import AvailableActions
from ww.errors import ConfigurationError
from ww.extensions import ExtensionRegistry, is_extension_reference
from ww.workflow_config import (
    HandlerDefinition,
    WorkflowConfiguration,
)
from ww.workspace import Workdir


class ActionResolver:
    """Resolve handlers without taking part in plan ordering or item allocation."""

    def __init__(
        self,
        configuration: WorkflowConfiguration,
        agent: str,
        extensions: ExtensionRegistry | None,
        available: AvailableActions,
        builtins: dict[str, str],
        project: str | None = None,
    ) -> None:
        self.configuration = configuration
        self.agent = agent
        self.extensions = extensions
        self.available = available
        self.builtins = builtins
        self.project = project

    def _handler(
        self, value: HandlerDefinition, *, resolve_reference: bool = False
    ) -> tuple[HandlerDefinition, str | None, HandlerDefinition | None]:
        if not resolve_reference or not value.is_reference:
            return value, None, None
        if is_extension_reference(value.name):
            definition = self._extension_handler(value.name)
            return replace(definition), value.name, definition
        registered = self.configuration.handlers_by_name.get(value.name)
        if registered is not None:
            return replace(registered), registered.name, registered
        return value, None, None

    def _extension_handler(self, value: str) -> HandlerDefinition:
        if self.extensions is None:
            raise ConfigurationError(
                f"{value!r} references an extension, but no extensions are loaded"
            )
        handler = self.extensions.handler(value)
        return HandlerDefinition(
            handler.name,
            description=handler.description,
            provide=handler.provide,
            outputs=handler.outputs,
            action=DefinedAction("extension", Extension(value)),
        )

    def _resolve(
        self, handler: HandlerDefinition
    ) -> tuple[PlanItemKind, PlanItemOwner, DefinedAction]:
        """Normalize implicit discovery once, then dispatch through the registry."""
        if isinstance(handler.action, DefinedAction):
            kind = handler.action.identifier
            return kind, actions.get(kind).owner, handler.action
        if handler.name in self.available.skills:
            payload: ActionPayload = Skill(handler.name)
            kind = "skill"
        elif handler.name in self.available.slash_commands:
            payload = SlashCommand(handler.name)
            kind = "slash_command"
        else:
            payload = Prompt(handler.description or handler.name)
            kind = "prompt"
        return kind, actions.get(kind).owner, DefinedAction(kind, payload)

    def plan_action(
        self, action: DefinedAction, allowed: set[str], workdir: Workdir = "task"
    ) -> object:
        """Plan one action as the item working in ``workdir`` will run it.

        An item working in the root follows the root's extension settings;
        one working in the task workspace or the project directory follows
        the run's project, when it has one.
        """
        context = ResolutionContext(
            self.agent,
            self.available,
            self.extensions,
            self.builtins,
            frozenset(allowed),
            project=self.project if workdir != "root" else None,
        )
        return actions.get(action.identifier).plan(action.payload, context)

    def _interpolate(self, value: object, allowed: set[str]) -> str:
        if not isinstance(value, str):
            raise AssertionError("plan actions must be strings")
        return ResolutionContext(
            self.agent,
            self.available,
            self.extensions,
            self.builtins,
            frozenset(allowed),
        ).interpolate(value)
