# SPDX-License-Identifier: GPL-3.0-or-later
"""Bash allow rules for ww's role commands, in Claude Code's local settings.

``init`` offers to write them so an agent running a task is not asked about
every ``ww`` call.  The rules name the project's wrapper by absolute path,
never a bare ``ww``: only the project's own launcher is allowed.  Merging adds
the missing rules and keeps every other key and rule as it was.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ww.errors import StateError

SETTINGS_FILE = ".claude/settings.local.json"
# The role commands an agent runs; ``discover`` and its variants take no space.
ROLE_COMMANDS = (
    "instruction *",
    "next *",
    "complete *",
    "fail *",
    "dispute *",
    "check *",
    "status *",
    "artifacts *",
    "items *",
    "item *",
    "add-item *",
    "update-item *",
    "interact *",
    "loop *",
    "lookup *",
    "discover*",
    "requirements *",
)


def role_rules(wrapper: Path) -> tuple[str, ...]:
    """The allow rules for the project wrapper at the absolute path ``wrapper``."""
    return tuple(f"Bash({wrapper} {command})" for command in ROLE_COMMANDS)


def merged(document: dict[str, Any], rules: tuple[str, ...]) -> dict[str, Any]:
    """``document`` with ``rules`` appended to ``permissions.allow``, none dropped."""
    permissions = document.get("permissions", {})
    if not isinstance(permissions, dict):
        raise StateError("permissions in the settings file must be an object")
    allowed = permissions.get("allow", [])
    if not isinstance(allowed, list):
        raise StateError("permissions.allow in the settings file must be a list")
    missing = [rule for rule in rules if rule not in allowed]
    return {
        **document,
        "permissions": {**permissions, "allow": [*allowed, *missing]},
    }


def install_rules(root: Path, rules: tuple[str, ...]) -> bool:
    """Merge ``rules`` into the project's local settings; ``True`` if it changed.

    A file that is not a JSON object is never overwritten: the error names it.
    """
    path = root / SETTINGS_FILE
    document: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"cannot read {SETTINGS_FILE}: {error}") from error
        if not isinstance(loaded, dict):
            raise StateError(f"{SETTINGS_FILE} must hold a JSON object")
        document = loaded
    updated = merged(document, rules)
    if updated == document:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    return True


def ensure_ignored(root: Path) -> bool:
    """Keep the local settings file out of Git; ``True`` if ``.gitignore`` changed."""
    if _ignored(root):
        return False
    gitignore = root / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    separator = "\n" if text and not text.endswith("\n") else ""
    gitignore.write_text(f"{text}{separator}{SETTINGS_FILE}\n", encoding="utf-8")
    return True


def _ignored(root: Path) -> bool:
    if (root / ".git").exists():
        try:
            checked = subprocess.run(
                ["git", "check-ignore", "-q", SETTINGS_FILE],
                cwd=root,
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
        else:
            return checked.returncode == 0
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return False
    lines = {
        line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()
    }
    return bool(lines & {SETTINGS_FILE, f"/{SETTINGS_FILE}", ".claude/*.local.json"})
