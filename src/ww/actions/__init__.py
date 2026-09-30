# SPDX-License-Identifier: GPL-3.0-or-later
"""Internal registry and public imports for workflow action contracts."""

from .command import CommandAction
from .contracts import (
    Action,
    ActionPayload,
    ActionRegistry,
    ActionResult,
    ActionTraits,
    AssertionCondition,
    AssertionDefinition,
    AttestationField,
    AutomaticAction,
    CommandDefinition,
    CommandOutcome,
    CommandRequest,
    Commands,
    CommandService,
    DefinedAction,
    DefinitionOverrideContext,
    ExecutionContext,
    Extension,
    ExtensionBinding,
    ExtensionIdentityService,
    ExtensionService,
    InstructionContent,
    InstructionContext,
    Mcp,
    PlannedAction,
    PreflightContext,
    Prompt,
    RecoveryCheckResult,
    RecoveryContext,
    RecoveryExtensionService,
    ResolutionContext,
    Skill,
    SlashCommand,
    actions,
)
from .extension import ExtensionAction
from .mcp import McpAction
from .prompt import PromptAction
from .skill import SkillAction
from .slash_command import SlashCommandAction

__all__ = [
    "Action",
    "ActionResult",
    "ActionTraits",
    "AttestationField",
    "AutomaticAction",
    "ActionPayload",
    "ActionRegistry",
    "CommandAction",
    "CommandOutcome",
    "CommandRequest",
    "CommandService",
    "AssertionCondition",
    "AssertionDefinition",
    "CommandDefinition",
    "Commands",
    "ExecutionContext",
    "DefinedAction",
    "DefinitionOverrideContext",
    "Extension",
    "ExtensionBinding",
    "ExtensionService",
    "ExtensionIdentityService",
    "ExtensionAction",
    "InstructionContent",
    "InstructionContext",
    "Mcp",
    "PlannedAction",
    "PreflightContext",
    "Prompt",
    "ResolutionContext",
    "RecoveryCheckResult",
    "RecoveryContext",
    "RecoveryExtensionService",
    "Skill",
    "SlashCommand",
    "actions",
]

for _action in (
    PromptAction(),
    SkillAction(),
    SlashCommandAction(),
    McpAction(),
    CommandAction(),
    ExtensionAction(),
):
    actions.register(_action)
