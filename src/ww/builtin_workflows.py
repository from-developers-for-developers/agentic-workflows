# SPDX-License-Identifier: GPL-3.0-or-later
"""The workflows ww ships with, composed below every configuration level.

Each file in ``ww/assets/workflows/`` is ordinary ``ww-agentic-workflows.yaml``
notation holding one or more workflows, and optionally the root ``documents``
and ``modes`` that belong to them. Together they form a built-in level beneath
the user, repo and local levels:

- a workflow, document or mode that any configuration level defines under the
  same name replaces the built-in one;
- ``ww-agentic-workflows.json`` switches a built-in workflow off with
  ``"workflows": {"<name>": {"enabled": false}}``; a file whose workflows are
  all switched off contributes nothing, its documents and modes included.

The built-in level is added after composition and parsing, when the
configuration is validated, so a level's ``extends: false`` never removes it
and the project's own files are parsed exactly as they are written.

``catchall`` records a change no configured workflow covers. It exists so that
every change goes through ww, including the small ones an agent would
otherwise just make, without adding any process to them: the agent works
exactly as it would on a plain prompt and ww keeps the record.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ww.errors import ConfigurationError
from ww.workflow_config import (
    DocumentDefinition,
    ModeDefinition,
    WorkflowConfiguration,
    WorkflowDefinition,
)

if TYPE_CHECKING:
    from importlib.abc import Traversable

    from ww.project_config import ProjectConfig

CATCHALL = "catchall"
# The root keys a built-in file may declare.
BUILTIN_KEYS = frozenset({"workflows", "documents", "modes"})
# Where the built-in files live; tests point it at a directory of their own.
BUILTIN_DIRECTORY: Traversable | Path = files("ww.assets").joinpath("workflows")


@dataclass(frozen=True)
class BuiltinFile:
    """One built-in YAML file: its workflows and what belongs to them."""

    name: str
    workflows: tuple[WorkflowDefinition, ...]
    documents: tuple[DocumentDefinition, ...] = ()
    modes: tuple[ModeDefinition, ...] = ()


_parsed: dict[str, tuple[BuiltinFile, ...]] = {}


def builtin_files() -> tuple[BuiltinFile, ...]:
    """Every built-in file, parsed once per directory, in file-name order."""
    directory = BUILTIN_DIRECTORY
    key = str(directory)
    if key not in _parsed:
        _parsed[key] = tuple(
            _parse_file(entry)
            for entry in sorted(directory.iterdir(), key=lambda item: item.name)
            if entry.name.endswith(".yaml") and entry.is_file()
        )
    return _parsed[key]


def builtin_workflows() -> tuple[WorkflowDefinition, ...]:
    return tuple(
        workflow for builtin in builtin_files() for workflow in builtin.workflows
    )


def builtin_workflow_names() -> frozenset[str]:
    return frozenset(workflow.name for workflow in builtin_workflows())


def builtin_workflow(name: str) -> WorkflowDefinition:
    """The built-in workflow ``name``, as ww ships it."""
    for workflow in builtin_workflows():
        if workflow.name == name:
            return workflow
    raise KeyError(name)


def is_builtin(workflow: WorkflowDefinition) -> bool:
    """Whether ``workflow`` is a built-in one, not a configured replacement."""
    return any(workflow == builtin for builtin in builtin_workflows())


def with_builtin_workflows(
    configuration: WorkflowConfiguration, project_config: ProjectConfig
) -> WorkflowConfiguration:
    """Add each enabled built-in the configuration does not define itself.

    The built-in workflows follow the configured ones. A built-in file's
    documents and modes come along while any of its workflows is enabled,
    unless the configuration declares one of the same name.
    """
    workflows = {workflow.name for workflow in configuration.workflows}
    documents = {document.name for document in configuration.documents}
    modes = {mode.name for mode in configuration.modes}
    added_workflows: list[WorkflowDefinition] = []
    added_documents: list[DocumentDefinition] = []
    added_modes: list[ModeDefinition] = []
    for builtin in builtin_files():
        enabled = [
            workflow
            for workflow in builtin.workflows
            if project_config.workflow_enabled(workflow.name)
        ]
        if not enabled:
            continue
        added_workflows.extend(
            workflow for workflow in enabled if workflow.name not in workflows
        )
        for document in builtin.documents:
            if document.name not in documents:
                documents.add(document.name)
                added_documents.append(document)
        for mode in builtin.modes:
            if mode.name not in modes:
                modes.add(mode.name)
                added_modes.append(mode)
    if not (added_workflows or added_documents or added_modes):
        return configuration
    return replace(
        configuration,
        workflows=(*configuration.workflows, *added_workflows),
        documents=(*configuration.documents, *added_documents),
        modes=(*configuration.modes, *added_modes),
    )


def _parse_file(entry: Traversable | Path) -> BuiltinFile:
    # The notation frontend validates through this module, so it is imported
    # only once a built-in file is first read.
    from ww.config import parse_yaml_text

    label = f"built-in {entry.name}"
    text = entry.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{label} must contain a mapping")
    unknown = set(raw) - BUILTIN_KEYS
    if unknown:
        raise ConfigurationError(
            f"{label} declares {', '.join(sorted(unknown))}; a built-in file "
            "holds only " + ", ".join(sorted(BUILTIN_KEYS))
        )
    parsed = parse_yaml_text(text, label)
    return BuiltinFile(
        entry.name.removesuffix(".yaml"),
        parsed.workflows,
        parsed.documents,
        parsed.modes,
    )
