# SPDX-License-Identifier: GPL-3.0-or-later
"""The names and locations of ww's configuration files.

``ww-agentic-workflows.yaml`` describes what workflows do and
``ww-agentic-workflows.json`` how the tools around them behave. Each comes in
three levels, applied top to bottom so a lower level wins:

1. machine: ``ww-agentic-workflows.machine.{yaml,json}`` in the user's
   configuration directory, shared by every project on the machine;
2. repo: ``ww-agentic-workflows.{yaml,json}`` in the project root;
3. local: ``ww-agentic-workflows.local.{yaml,json}`` next to the repo files,
   kept out of version control.

Earlier ww versions called the repo files ``workflows.yaml`` and
``agentic-workflows.json``; ww no longer reads those names, stops when it finds
one, and ``init`` renames them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ww.errors import ConfigurationError

FILE_STEM = "ww-agentic-workflows"
WORKFLOWS_FILE = f"{FILE_STEM}.yaml"
SETTINGS_FILE = f"{FILE_STEM}.json"
LOCAL_WORKFLOWS_FILE = f"{FILE_STEM}.local.yaml"
LOCAL_SETTINGS_FILE = f"{FILE_STEM}.local.json"
MACHINE_WORKFLOWS_FILE = f"{FILE_STEM}.machine.yaml"
MACHINE_SETTINGS_FILE = f"{FILE_STEM}.machine.json"
# The .gitignore patterns ``init`` adds; they also cover local files a local
# configuration imports, such as ``git.ww-agentic-workflows.local.yaml``.
LOCAL_IGNORE_PATTERNS = (f"*{LOCAL_WORKFLOWS_FILE}", f"*{LOCAL_SETTINGS_FILE}")
# Where the machine level lives instead of the user's configuration directory.
MACHINE_DIR_VARIABLE = "WW_MACHINE_CONFIG_DIR"


@dataclass(frozen=True)
class ConfigurationLevel:
    """Where one level keeps one kind of configuration file."""

    name: str
    path: Path


def machine_directory() -> Path:
    """The machine level's directory, following XDG when it is configured."""
    configured = os.environ.get(MACHINE_DIR_VARIABLE)
    if configured:
        return Path(configured)
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / FILE_STEM


def display_path(file: Path, base: Path) -> str:
    """How messages name ``file``: from the repo root, from home, or in full."""
    for root, prefix in ((base, ""), (Path.home(), "~/")):
        try:
            return prefix + str(file.relative_to(root))
        except ValueError:
            continue
    return str(file)


def workflow_levels(repo_file: Path) -> tuple[ConfigurationLevel, ...]:
    """The YAML levels around ``repo_file``, from machine to local."""
    return (
        ConfigurationLevel("machine", machine_directory() / MACHINE_WORKFLOWS_FILE),
        ConfigurationLevel("repo", repo_file),
        ConfigurationLevel("local", repo_file.with_name(LOCAL_WORKFLOWS_FILE)),
    )


def settings_levels(repo_file: Path) -> tuple[ConfigurationLevel, ...]:
    """The JSON levels around ``repo_file``, from machine to local."""
    return (
        ConfigurationLevel("machine", machine_directory() / MACHINE_SETTINGS_FILE),
        ConfigurationLevel("repo", repo_file),
        ConfigurationLevel("local", repo_file.with_name(LOCAL_SETTINGS_FILE)),
    )


def project_settings_levels(directory: Path) -> tuple[ConfigurationLevel, ...]:
    """The JSON levels a configured project carries in its own directory.

    A project has a repo and a local level, like the root; the machine level
    is shared by every project on the machine and is read once, at the root.
    """
    return (
        ConfigurationLevel("repo", directory / SETTINGS_FILE),
        ConfigurationLevel("local", directory / LOCAL_SETTINGS_FILE),
    )

def task_format_moved(label: str) -> str:
    """The error for a ``task_format`` key still written in a YAML file."""
    return (
        f"task_format in {label} now lives in {SETTINGS_FILE} (or its machine or "
        "local file); move it there and remove it from the YAML"
    )


# Each former name and the name that replaced it.
LEGACY_FILES = {
    "workflows.yaml": WORKFLOWS_FILE,
    "agentic-workflows.json": SETTINGS_FILE,
}


def check_legacy_files(root: Path) -> None:
    """Stop when ``root`` still holds a configuration file under a former name."""
    for legacy, current in LEGACY_FILES.items():
        if not (root / legacy).is_file():
            continue
        if (root / current).exists():
            raise ConfigurationError(
                f"found {legacy} next to {current}; ww reads only {current}, "
                f"so remove {legacy}"
            )
        raise ConfigurationError(
            f"found {legacy}; rename it to {current}, or run init to rename it"
        )


def rename_legacy_files(root: Path) -> tuple[tuple[str, str], ...]:
    """Rename former configuration files whose new name is still free."""
    renamed = []
    for legacy, current in LEGACY_FILES.items():
        if (root / legacy).is_file() and not (root / current).exists():
            (root / legacy).rename(root / current)
            renamed.append((legacy, current))
    return tuple(renamed)
