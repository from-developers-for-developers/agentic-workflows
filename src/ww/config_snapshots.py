# SPDX-License-Identifier: GPL-3.0-or-later
"""Read ww's configuration from a commit, never from whatever is checked out.

In a Git project the configuration tasks read is the one committed on a ref,
``config_ref`` in ``ww.json``, by default the ww/git default base branch. ww
copies it out of that commit, without a checkout, into
``<root>/.ww/config/<sha>/``: ``ww.yaml``, ``ww.json``, the rule-automation
store, and every committed file their values name, such as an import, a rule
file or directory, or a check's script reached as ``{{ww.root}}/<path>``. The
copy is written to a temporary directory and renamed into place, so a reader
never sees half of one, and made read-only: a commit's copy never changes.
One missing, as after ``git clean``, is written again on the next read.

Every run records the commit it started with and reads that copy until it
ends, whatever the operator checks out meanwhile or commits on the ref;
``ww config refresh`` moves a task to the ref's current commit. Commands
outside a task read the ref's current commit. Commands that edit the
configuration (``init``, ``lint``, ``setup``, ``onboarding``, rule writes)
read the checkout, where the edit is made and committed.

Without Git, with ``config_ref: false``, or when the ref's commit holds no
``ww.yaml`` yet, the configuration is read from the checkout as before. What
ww writes while running, task state, locks, logs, rule counters, disputes, and
the untracked ``ww.local.*`` files stay in the checkout.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ww.config_files import (
    RULE_AUTOMATION_FILE,
    SETTINGS_FILE,
    SNAPSHOT_DIRECTORY,
    WORKFLOWS_FILE,
)
from ww.errors import ConfigurationError, StateError
from ww.project_config import load_project_config

GIT_EXTENSION = "ww/git"
# The files a snapshot starts from; everything else is found through them.
_ENTRY_FILES = (WORKFLOWS_FILE, SETTINGS_FILE, RULE_AUTOMATION_FILE)
# A configured path reached from the root in a check's command.
_ROOT_REFERENCE = re.compile(
    r"(?:\{\{\s*ww\.root\s*\}\}|\$\{?WW_ROOT\}?)/([^\s\"'`;|&()<>]+)"
)
_STRUCTURED = (".yaml", ".yml", ".json")


@dataclass(frozen=True)
class ConfigSource:
    """Where one invocation reads ww's configuration.

    ``directory`` is the checkout itself, or the snapshot of ``commit`` on
    ``ref``.
    """

    directory: Path
    ref: str | None = None
    commit: str | None = None

    @property
    def pinned(self) -> bool:
        return self.commit is not None


@dataclass(frozen=True)
class _Tree:
    """The files of one commit under the project root, by relative path."""

    blobs: dict[str, tuple[str, str]]

    @property
    def directories(self) -> set[str]:
        return {
            posixpath.dirname(path) for path in self.blobs if posixpath.dirname(path)
        }


class ConfigSnapshots:
    """The configuration snapshots of the project at ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.directory = self.root / SNAPSHOT_DIRECTORY
        self._prefix: str | None | bool = False

    # ----- where configuration comes from -------------------------------

    def ref(self) -> str | None:
        """The ref tasks read configuration from, or ``None`` for the checkout."""
        if self._repository_prefix() is None:
            return None
        config = load_project_config(self.root / SETTINGS_FILE)
        if config.config_ref is False:
            return None
        if config.config_ref is not None:
            return config.config_ref
        bases = config.settings_for(GIT_EXTENSION).get("base_branches")
        default = bases.get("default") if isinstance(bases, dict) else None
        return default if isinstance(default, str) and default else None

    def resolve(self, ref: str) -> str | None:
        """The commit ``ref`` names, or ``None`` when it names none."""
        result = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return result.strip() if result is not None and result.strip() else None

    def current(self) -> ConfigSource:
        """The configuration the ref's current commit holds.

        A configured ``config_ref`` that names no commit is an error; the
        default base branch that names none, or a commit without
        ``ww.yaml``, as before a project is set up there, reads the checkout.
        """
        ref = self.ref()
        if ref is None:
            return self.checkout()
        commit = self.resolve(ref)
        if commit is None:
            if load_project_config(self.root / SETTINGS_FILE).config_ref:
                raise ConfigurationError(
                    f"config_ref {ref!r} in {SETTINGS_FILE} names no commit; "
                    "set it to a branch that exists, or false to read the "
                    "configuration from the checkout"
                )
            return self.checkout()
        if WORKFLOWS_FILE not in self._tree(commit).blobs:
            return self.checkout()
        return ConfigSource(self.materialize(commit), ref, commit)

    def pinned(self, commit: str, ref: str | None) -> ConfigSource:
        """The configuration a run recorded, written again when it is gone."""
        if self._repository_prefix() is None:
            raise StateError(
                f"the task reads the configuration of commit {commit}, but "
                f"{self.root} is no longer a Git repository"
            )
        return ConfigSource(self.materialize(commit), ref, commit)

    def checkout(self) -> ConfigSource:
        """The configuration as the checkout holds it, edits included."""
        return ConfigSource(self.root)

    def ahead(self, commit: str, ref: str) -> int | None:
        """How many commits ``ref`` has beyond ``commit``, when both resolve."""
        result = self._git("rev-list", "--count", f"{commit}..{ref}")
        try:
            return int(result.strip()) if result is not None else None
        except ValueError:
            return None

    # ----- writing snapshots ------------------------------------------

    def materialize(self, commit: str) -> Path:
        """The read-only copy of ``commit``'s configuration, written once."""
        target = self.directory / commit
        if target.is_dir():
            return target
        tree = self._tree(commit)
        self.directory.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{commit}.", dir=self.directory))
        try:
            self._write(tree, staging, commit)
            _make_read_only(staging)
            try:
                staging.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
        finally:
            if staging.exists():
                _remove(staging)
        return target

    def snapshots(self) -> tuple[str, ...]:
        """The commits with a snapshot, sorted."""
        if not self.directory.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in self.directory.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            )
        )

    def prune(self, keep: Iterable[str]) -> tuple[str, ...]:
        """Remove every snapshot but ``keep``'s, and leftovers of crashed writes.

        Returns the commits removed.
        """
        if not self.directory.is_dir():
            return ()
        kept = set(keep)
        removed = []
        for entry in sorted(self.directory.iterdir()):
            if not entry.is_dir() or entry.name in kept:
                continue
            _remove(entry)
            if not entry.name.startswith("."):
                removed.append(entry.name)
        return tuple(removed)

    def _write(self, tree: _Tree, staging: Path, commit: str) -> None:
        """Write the entry files and every committed file they name."""
        pending = [path for path in _ENTRY_FILES if path in tree.blobs]
        written: set[str] = set()
        while pending:
            path = pending.pop(0)
            if path in written:
                continue
            written.add(path)
            mode, blob = tree.blobs[path]
            destination = staging / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = self._blob(blob, commit)
            destination.write_bytes(content)
            if mode == "100755":
                destination.chmod(0o755)
            pending.extend(
                found
                for found in _named_files(path, content, tree)
                if found not in written
            )

    # ----- Git ---------------------------------------------------------

    def _repository_prefix(self) -> str | None:
        """The root's path inside its repository, ``""`` at its top, else ``None``."""
        if self._prefix is False:
            result = self._git("rev-parse", "--show-prefix")
            self._prefix = result.strip() if result is not None else None
        return self._prefix if isinstance(self._prefix, str) else None

    def _tree(self, commit: str) -> _Tree:
        prefix = self._repository_prefix() or ""
        listing = self._git("ls-tree", "-r", "-z", f"{commit}:{prefix}")
        if listing is None:
            raise StateError(f"cannot read the files of commit {commit}")
        blobs: dict[str, tuple[str, str]] = {}
        for record in listing.split("\0"):
            if not record:
                continue
            meta, _, path = record.partition("\t")
            mode, kind, blob = meta.split()
            if kind == "blob" and mode in {"100644", "100755"}:
                blobs[path] = (mode, blob)
        return _Tree(blobs)

    def _blob(self, blob: str, commit: str) -> bytes:
        try:
            result = subprocess.run(
                ["git", "cat-file", "blob", blob],
                cwd=self.root,
                capture_output=True,
                check=False,
            )
        except OSError as error:
            raise StateError(f"cannot run git: {error}") from error
        if result.returncode != 0:
            raise StateError(f"cannot read a file of commit {commit}")
        return result.stdout

    def _git(self, *arguments: str) -> str | None:
        """A Git command's output in the root, or ``None`` when it fails."""
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        return result.stdout if result.returncode == 0 else None


def _named_files(path: str, content: bytes, tree: _Tree) -> Iterator[str]:
    """The committed files a configuration file's values name.

    A value names a file relative to the root or to the file's own directory,
    or, in a command, as ``{{ww.root}}/<path>`` or ``$WW_ROOT/<path>``; a
    directory stands for the ``*.md`` files directly inside it, as a rule
    directory does. Values naming nothing committed are ignored.
    """
    if not path.endswith(_STRUCTURED):
        return
    try:
        text = content.decode("utf-8")
        data = json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
    except (UnicodeDecodeError, ValueError, yaml.YAMLError):
        return
    base = posixpath.dirname(path)
    directories = tree.directories
    for value in _strings(data):
        candidates = [value, *_ROOT_REFERENCE.findall(value)]
        for candidate in candidates:
            for relative in {candidate, posixpath.join(base, candidate)}:
                normalized = _normalized(relative)
                if normalized is None:
                    continue
                if normalized in tree.blobs:
                    yield normalized
                elif normalized in directories:
                    yield from (
                        file
                        for file in tree.blobs
                        if posixpath.dirname(file) == normalized
                        and file.endswith(".md")
                    )


def _normalized(path: str) -> str | None:
    """``path`` inside the root, normalised, or ``None`` when it leaves it."""
    stripped = path.strip()
    if not stripped or stripped.startswith("/") or "\n" in stripped:
        return None
    normalized = posixpath.normpath(stripped)
    if normalized in {".", ""} or normalized.split("/")[0] == "..":
        return None
    return normalized


def _strings(data: Any) -> Iterator[str]:
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str):
                yield key
            yield from _strings(value)
    elif isinstance(data, list):
        for value in data:
            yield from _strings(value)


def _make_read_only(directory: Path) -> None:
    for current, folders, files in os.walk(directory, topdown=False):
        for name in files:
            path = Path(current, name)
            mode = path.stat().st_mode
            path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        for name in folders:
            Path(current, name).chmod(0o555)
    directory.chmod(0o555)


def _remove(directory: Path) -> None:
    """Remove a read-only snapshot: its directories are made writable first."""
    for current, folders, _ in os.walk(directory):
        Path(current).chmod(0o755)
        for name in folders:
            Path(current, name).chmod(0o755)
    shutil.rmtree(directory, ignore_errors=True)
