# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON catalogs of modes, workflows, runtimes, agents, and extensions."""

from __future__ import annotations

import json

from ww.config import load_configuration, load_modes
from ww.discovery import AGENT_DIRECTORIES, CUSTOM_AGENT_PREFIX
from ww.executable import ww_command
from ww.extensions import ExtensionRegistry
from ww.runtimes import RUNTIME_INSTRUCTIONS
from ww.storage import Storage


def _catalog_extensions(extensions: ExtensionRegistry) -> str:
    """Render every discovered extension and what it contributes."""
    return json.dumps(
        {
            "extensions": [
                {
                    "id": extension.identifier,
                    "version": extension.version,
                    "api_version": extension.api_version,
                    "source": extensions.identity(extension.identifier).source,
                    "description": extension.description,
                    "handlers": [
                        {
                            "reference": (
                                f"ext/{extension.identifier}/handlers:{handler.name}"
                            ),
                            "description": handler.description,
                            "inputs": [value.to_dict() for value in handler.provide],
                            "outputs": list(handler.outputs),
                        }
                        for handler in extension.handlers
                    ],
                    "modes": [
                        {
                            "reference": (
                                f"ext/{extension.identifier}/modes:{mode.name}"
                            ),
                            "description": list(mode.description),
                        }
                        for mode in extension.modes
                    ],
                    "commands": [
                        {
                            "name": command.name,
                            "description": command.description,
                            "usage": (
                                f"{ww_command()} extension "
                                f"{extension.identifier} "
                                f"{command.usage or command.name}"
                            ),
                        }
                        for command in extension.commands
                    ],
                    "variable_overrides": [
                        variable.name for variable in extension.variables
                    ],
                }
                for extension in extensions.extensions
            ]
        },
        indent=2,
    )


def _catalog_modes(storage: Storage, extensions: ExtensionRegistry) -> str:
    """Render configured and extension modes after validating the workflow file."""
    modes = load_modes(storage.config_path, extensions)
    modes.update({mode.name: mode for mode in extensions.qualified_modes()})
    return json.dumps({"modes": [mode.to_dict() for mode in modes.values()]}, indent=2)


def _catalog_workflows(storage: Storage, extensions: ExtensionRegistry) -> str:
    """Render the configured workflow choices from validated configuration."""
    configuration = load_configuration(storage.config_path, extensions)
    return json.dumps(
        {
            "workflows": [
                {
                    "name": workflow.name,
                    "description": workflow.description,
                    "modes": list(workflow.modes),
                    "runtime": workflow.runtime,
                    "manual": workflow.manual,
                }
                for workflow in configuration.workflows
            ]
        },
        indent=2,
    )


def _catalog_runtimes() -> str:
    """Render the fixed runtime registry."""
    return json.dumps(
        {
            "runtimes": [
                {"name": name, "instructions": list(instructions)}
                for name, instructions in RUNTIME_INSTRUCTIONS.items()
            ]
        },
        indent=2,
    )


def _catalog_projects(extensions: ExtensionRegistry) -> str:
    """Render the configured projects, the directories tasks may work in."""
    return json.dumps(
        {"projects": [project.to_dict() for project in extensions.config.projects]},
        indent=2,
    )


def _catalog_agents() -> str:
    """Render supported agent integrations and the custom-agent escape hatch."""
    return json.dumps(
        {
            "agents": [
                {"name": name, "directory": directory}
                for name, directory in AGENT_DIRECTORIES.items()
            ],
            "custom": {
                "prefix": CUSTOM_AGENT_PREFIX,
                "description": "Use custom:<name> for an arbitrary agent name.",
            },
        },
        indent=2,
    )
