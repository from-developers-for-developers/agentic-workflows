# SPDX-License-Identifier: GPL-3.0-or-later
"""The names of ww's configuration files in a project.

``ww-agentic-workflows.yaml`` describes what workflows do and
``ww-agentic-workflows.json`` how the tools around them behave. Earlier ww
versions called them ``workflows.yaml`` and ``agentic-workflows.json``; ww no
longer reads those names, stops when it finds one, and ``init`` renames them.
"""

from __future__ import annotations

from pathlib import Path

from ww.errors import ConfigurationError

FILE_STEM = "ww-agentic-workflows"
WORKFLOWS_FILE = f"{FILE_STEM}.yaml"
SETTINGS_FILE = f"{FILE_STEM}.json"

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
