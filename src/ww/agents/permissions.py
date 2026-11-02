# SPDX-License-Identifier: GPL-3.0-or-later
"""Allow rules that let an agent run ww's commands without asking.

An agent whose permission format ww knows has a :class:`Permissions`: the
shared settings file the ``init`` summary tells the operator to merge entries
into, and the file kept out of Git that ``init`` may write the role
commands to itself.  Role rules name the project's wrapper by absolute path,
never a bare ``ww``: only the project's own launcher is allowed.  Merging
adds the missing rules and keeps every other key and rule as it was.
"""

from __future__ import annotations

import json
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ww.errors import StateError

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
    "lookup *",
    "record *",
    "discover*",
    "requirements *",
)


class Permissions(ABC):
    """One agent's permission format."""

    # The project settings file shared through Git.
    shared_file: str
    # The project settings file kept out of Git that ``init`` writes the role
    # rules to; ``None`` when ww does not write the agent's permissions.
    local_file: str | None = None
    # ``.gitignore`` lines besides ``local_file`` that already ignore it.
    ignore_patterns: tuple[str, ...] = ()

    @abstractmethod
    def rule(self, command: str) -> str:
        """The allow rule for a shell command pattern, such as ``./ww *``."""

    @abstractmethod
    def merged(
        self, document: dict[str, Any], rules: tuple[str, ...]
    ) -> dict[str, Any]:
        """``document`` with ``rules`` added, every other entry kept."""

    def allow(self, commands: tuple[str, ...]) -> dict[str, Any]:
        """The settings entries that allow ``commands`` with any arguments."""
        return self.merged({}, tuple(self.rule(f"{command} *") for command in commands))

    def role_rules(self, wrapper: Path) -> tuple[str, ...]:
        """The rules for ww's role commands through the wrapper at ``wrapper``."""
        return tuple(self.rule(f"{wrapper} {command}") for command in ROLE_COMMANDS)


def install_rules(root: Path, permissions: Permissions, rules: tuple[str, ...]) -> bool:
    """Merge ``rules`` into the agent's local settings; ``True`` if it changed.

    A file that is not a JSON object is never overwritten: the error names it.
    """
    relative = _local_file(permissions)
    path = root / relative
    document: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"cannot read {relative}: {error}") from error
        if not isinstance(loaded, dict):
            raise StateError(f"{relative} must hold a JSON object")
        document = loaded
    updated = permissions.merged(document, rules)
    if updated == document:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    return True


def ensure_ignored(root: Path, permissions: Permissions) -> bool:
    """Keep the local settings out of Git; ``True`` if ``.gitignore`` changed."""
    relative = _local_file(permissions)
    if _ignored(root, relative, permissions.ignore_patterns):
        return False
    gitignore = root / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    separator = "\n" if text and not text.endswith("\n") else ""
    gitignore.write_text(f"{text}{separator}{relative}\n", encoding="utf-8")
    return True


def _local_file(permissions: Permissions) -> str:
    if permissions.local_file is None:
        raise StateError("this agent has no local settings file ww writes")
    return permissions.local_file


def _ignored(root: Path, relative: str, patterns: tuple[str, ...]) -> bool:
    if (root / ".git").exists():
        try:
            checked = subprocess.run(
                ["git", "check-ignore", "-q", relative],
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
    return bool(lines & {relative, f"/{relative}", *patterns})
