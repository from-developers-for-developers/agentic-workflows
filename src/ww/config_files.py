# SPDX-License-Identifier: GPL-3.0-or-later
"""The names and locations of ww's configuration files.

``ww-agentic-workflows.yaml`` describes what workflows do and
``ww-agentic-workflows.json`` how the tools around them behave. Each comes in
three levels, applied top to bottom so a lower level wins:

1. user: ``ww-agentic-workflows.{yaml,json}`` in the user's configuration
   directory, shared by every project of the user;
2. repo: ``ww-agentic-workflows.{yaml,json}`` in the project root;
3. local: ``ww-agentic-workflows.local.{yaml,json}`` next to the repo files,
   kept out of version control.

Earlier ww versions called the repo files ``workflows.yaml`` and
``agentic-workflows.json``; ww no longer reads those names, stops when it finds
one, and ``init`` renames them. The user level was once the machine level,
with ``.machine`` in its file names and ``WW_MACHINE_CONFIG_DIR`` naming its
directory; ww stops on either and names the replacement.
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
# The rule-automation store: ww-owned derived knowledge at the project root,
# committed so every checkout shares it, never part of a step's change set.
RULE_AUTOMATION_FILE = "ww-rule-automation.json"
# Rule groups ``ww rules add --group`` and ``ww rules filter`` write, imported
# by the repo file so that file is never rewritten; ww owns this one.
RULES_IMPORT_FILE = "ww-rules.yaml"
# The .gitignore patterns ``init`` adds; they also cover local files a local
# configuration imports, such as ``git.ww-agentic-workflows.local.yaml``.
LOCAL_IGNORE_PATTERNS = (f"*{LOCAL_WORKFLOWS_FILE}", f"*{LOCAL_SETTINGS_FILE}")
# Where the user level lives instead of the user's configuration directory.
USER_DIR_VARIABLE = "WW_USER_CONFIG_DIR"
# The variable earlier ww versions read instead; setting it alone is an error.
FORMER_USER_DIR_VARIABLE = "WW_MACHINE_CONFIG_DIR"
# The user-level files earlier ww versions read, by the name that replaced them.
FORMER_USER_FILES = {
    f"{FILE_STEM}.machine.yaml": WORKFLOWS_FILE,
    f"{FILE_STEM}.machine.json": SETTINGS_FILE,
}


@dataclass(frozen=True)
class ConfigurationLevel:
    """Where one level keeps one kind of configuration file."""

    name: str
    path: Path


def user_directory() -> Path:
    """The user level's directory, following XDG when it is configured.

    Stops when only the former variable is set, or when the directory still
    holds a file under its former name, so an old setup is never silently
    ignored.
    """
    configured = os.environ.get(USER_DIR_VARIABLE)
    if not configured and os.environ.get(FORMER_USER_DIR_VARIABLE):
        raise ConfigurationError(
            f"{FORMER_USER_DIR_VARIABLE} is no longer read; set "
            f"{USER_DIR_VARIABLE} instead"
        )
    if configured:
        directory = Path(configured)
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        directory = (Path(base) if base else Path.home() / ".config") / FILE_STEM
    for former, current in FORMER_USER_FILES.items():
        if (directory / former).is_file():
            raise ConfigurationError(
                f"found {directory / former}; the user level now reads "
                f"{directory / current}, so rename it"
            )
    return directory


def display_path(file: Path, base: Path) -> str:
    """How messages name ``file``: from the repo root, from home, or in full."""
    for root, prefix in ((base, ""), (Path.home(), "~/")):
        try:
            return prefix + str(file.relative_to(root))
        except ValueError:
            continue
    return str(file)


def _levels(
    repo_file: Path, user_name: str, local_name: str
) -> tuple[ConfigurationLevel, ...]:
    """The user, repo, and local levels around ``repo_file``.

    The user file shares the repo file's name, so a user directory that is
    the project root itself contributes no separate level.
    """
    user = user_directory() / user_name
    return (
        *(
            (ConfigurationLevel("user", user),)
            if user.resolve() != repo_file.resolve()
            else ()
        ),
        ConfigurationLevel("repo", repo_file),
        ConfigurationLevel("local", repo_file.with_name(local_name)),
    )


def workflow_levels(repo_file: Path) -> tuple[ConfigurationLevel, ...]:
    """The YAML levels around ``repo_file``, from user to local."""
    return _levels(repo_file, WORKFLOWS_FILE, LOCAL_WORKFLOWS_FILE)


def settings_levels(repo_file: Path) -> tuple[ConfigurationLevel, ...]:
    """The JSON levels around ``repo_file``, from user to local."""
    return _levels(repo_file, SETTINGS_FILE, LOCAL_SETTINGS_FILE)


def project_settings_levels(directory: Path) -> tuple[ConfigurationLevel, ...]:
    """The JSON levels a configured project carries in its own directory.

    A project has a repo and a local level, like the root; the user level is
    shared by every project of the user and is read once, at the root.
    """
    return (
        ConfigurationLevel("repo", directory / SETTINGS_FILE),
        ConfigurationLevel("local", directory / LOCAL_SETTINGS_FILE),
    )

def task_format_moved(label: str) -> str:
    """The error for a ``task_format`` key still written in a YAML file."""
    return (
        f"task_format in {label} now lives in {SETTINGS_FILE} (or its user or "
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
