# SPDX-License-Identifier: GPL-3.0-or-later
"""The names and locations of ww's configuration files.

``ww.yaml`` describes what workflows do and
``ww.json`` how the tools around them behave. Each comes in
three levels, applied top to bottom so a lower level wins:

1. user: ``ww.{yaml,json}`` in the user's configuration
   directory, shared by every project of the user;
2. repo: ``ww.{yaml,json}`` in the project root;
3. local: ``ww.local.{yaml,json}`` next to the repo files,
   kept out of version control.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

FILE_STEM = "ww"
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
# What ``ww setup apply`` writes: an import file for the repo level, and one
# for the local level, which stays out of version control.
SETUP_IMPORT_FILE = "ww-setup.yaml"
LOCAL_SETUP_IMPORT_FILE = "ww-setup.local.yaml"
# The .gitignore patterns ``init`` adds; they also cover local files a local
# configuration imports, such as ``git.ww.local.yaml``.
LOCAL_IGNORE_PATTERNS = (
    f"*{LOCAL_WORKFLOWS_FILE}",
    f"*{LOCAL_SETTINGS_FILE}",
    LOCAL_SETUP_IMPORT_FILE,
)
# The files under ``.ww`` a team commits: what ww learned about the project.
# Everything else there is one checkout's state. Older versions also shared
# ``team.md`` and ``company.md``; ww no longer writes them, and a line that
# re-includes them stays as the operator has it.
SHARED_RUNTIME_FILES = ("project.md",)
# The .gitignore lines ``init`` writes for ``.ww``. Git cannot re-include a
# file inside an ignored directory, so the directory's contents are ignored
# rather than the directory itself, and each shared file is then re-included.
RUNTIME_IGNORE_LINES = (
    ".ww/*",
    *(f"!.ww/{name}" for name in SHARED_RUNTIME_FILES),
)
# The lines that ignore ``.ww`` whole, as an operator may write them; ``init``
# replaces them with :data:`RUNTIME_IGNORE_LINES`, since a directory ignored
# whole cannot have files re-included.
WHOLE_RUNTIME_IGNORE_LINES = (".ww/", ".ww", "/.ww", "/.ww/")


def runtime_ignored(gitignore: str) -> bool:
    """Whether a .gitignore's text already keeps ``.ww`` out, in any form."""
    return bool(
        {*WHOLE_RUNTIME_IGNORE_LINES, RUNTIME_IGNORE_LINES[0]}.intersection(
            line.strip() for line in gitignore.splitlines()
        )
    )


def newline_of(text: str) -> str:
    """The line ending a text file uses: CRLF when it has one, else LF."""
    return "\r\n" if "\r\n" in text else "\n"


# Where the user level lives instead of the user's configuration directory.
USER_DIR_VARIABLE = "WW_USER_CONFIG_DIR"


@dataclass(frozen=True)
class ConfigurationLevel:
    """Where one level keeps one kind of configuration file."""

    name: str
    path: Path


def user_directory() -> Path:
    """The user level's directory, following XDG when it is configured."""
    configured = os.environ.get(USER_DIR_VARIABLE)
    if configured:
        directory = Path(configured)
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        directory = (Path(base) if base else Path.home() / ".config") / FILE_STEM
    return directory


# Configuration files read as if written, by resolved path; ``None`` reads as
# absent. ``ww setup apply`` validates its plan this way, so checking a change
# never touches the project.
_STAGED: ContextVar[Mapping[Path, str | None] | None] = ContextVar(
    "ww_staged_configuration", default=None
)


@contextmanager
def staged_files(contents: Mapping[Path, str | None]) -> Iterator[None]:
    """Read configuration files as if ``contents`` were written in place.

    Only reads through :func:`read_configuration_file` and
    :func:`configuration_file_exists` see the staged contents; nothing is
    written.
    """
    token = _STAGED.set({path.resolve(): text for path, text in contents.items()})
    try:
        yield
    finally:
        _STAGED.reset(token)


def read_configuration_file(path: Path) -> str:
    """A configuration file's text, staged or on disk."""
    staged = _STAGED.get()
    if staged is not None and path.resolve() in staged:
        text = staged[path.resolve()]
        if text is None:
            raise FileNotFoundError(f"no such file: {path}")
        return text
    return path.read_text(encoding="utf-8")


def configuration_file_exists(path: Path) -> bool:
    """Whether a configuration file is there, staged or on disk."""
    staged = _STAGED.get()
    if staged is not None and path.resolve() in staged:
        return staged[path.resolve()] is not None
    return path.is_file()


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
