# SPDX-License-Identifier: GPL-3.0-or-later
"""Filesystem persistence, using locked atomic state replacement."""

from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from ww.config.composition import compose_configuration
from ww.config_files import (
    LOCAL_IGNORE_PATTERNS,
    RUNTIME_IGNORE_LINES,
    SETTINGS_FILE,
    WHOLE_RUNTIME_IGNORE_LINES,
    WORKFLOWS_FILE,
    display_path,
    newline_of,
    user_directory,
    workflow_levels,
)
from ww.errors import ConfigurationError, StateError
from ww.locking import FileLocks
from ww.results import NO_WORKFLOWS_ACTION, InitializationResult
from ww.storage_adapters import (
    FileProjectMetadataStorageAdapter,
    FileTaskStorageAdapter,
    ProjectMetadataStorage,
    TaskStorageAdapter,
)

EXECUTION_LOG_MAX_BYTES = 1_000_000
EXECUTION_LOG_RETENTION = 5


class Storage:
    """Path conventions and filesystem operations for one project root."""

    def __init__(
        self,
        root: Path,
        task_persistence: TaskStorageAdapter | None = None,
        project_metadata: ProjectMetadataStorage | None = None,
    ) -> None:
        self.root = root
        self.locks = FileLocks(root)
        self.task_persistence = task_persistence or FileTaskStorageAdapter(root)
        self.project_metadata = project_metadata or FileProjectMetadataStorageAdapter(
            root
        )

    def lock_project(self) -> AbstractContextManager[None]:
        """Serialize project-wide file changes against other ww processes."""
        return self.locks.lock(self.root, purpose="project files")

    @property
    def config_path(self) -> Path:
        return self.root / WORKFLOWS_FILE

    @property
    def runtime_path(self) -> Path:
        return self.root / ".ww"

    @property
    def project_config_path(self) -> Path:
        return self.root / SETTINGS_FILE

    def initialize_project(
        self,
        workflows: str,
        project_config: str,
        launcher: str,
        agent_instructions: str,
        *,
        ignore_runtime: bool = False,
        skills: tuple[tuple[str, str], ...] = (),
    ) -> InitializationResult:
        """Add any missing project files without replacing existing ones.

        ``skills`` pairs a project-relative ``SKILL.md`` location with the
        content it receives unless a file already exists there.
        """
        with self.lock_project():
            return self._initialize_project(
                workflows,
                project_config,
                launcher,
                agent_instructions,
                ignore_runtime=ignore_runtime,
                skills=skills,
            )

    def _initialize_project(
        self,
        workflows: str,
        project_config: str,
        launcher: str,
        agent_instructions: str,
        *,
        ignore_runtime: bool,
        skills: tuple[tuple[str, str], ...],
    ) -> InitializationResult:
        launcher_path = self.root / "ww"
        instructions_path = self.root / "WW_AGENT_INSTRUCTIONS.md"
        created: list[str] = []
        preserved: list[str] = []
        self.root.mkdir(parents=True, exist_ok=True)

        if self.config_path.exists():
            if self._add_missing_workflow_defaults(workflows):
                created.append(f"{WORKFLOWS_FILE} (added missing defaults)")
            else:
                preserved.append(WORKFLOWS_FILE)
        else:
            self._write_starter_workflows(workflows)
            created.append(WORKFLOWS_FILE)

        if self.project_config_path.exists():
            if self._add_missing_project_settings(project_config):
                created.append(f"{SETTINGS_FILE} (added missing settings)")
            else:
                preserved.append(SETTINGS_FILE)
        else:
            self.locks.atomic_write(self.project_config_path, project_config)
            created.append(SETTINGS_FILE)

        # The launcher is ww-owned: one an older ww wrote reads old config
        # names and can run the wrong binary, so a differing one is replaced.
        if launcher_path.exists():
            if launcher_path.read_text(encoding="utf-8") != launcher:
                self.locks.atomic_write(launcher_path, launcher)
                created.append("ww (updated to the current launcher)")
            else:
                preserved.append("ww")
        else:
            self.locks.atomic_write(launcher_path, launcher)
            created.append("ww")

        for path, content in (
            (instructions_path, agent_instructions),
            *((self.root / relative, content) for relative, content in skills),
        ):
            relative = str(path.relative_to(self.root))
            if path.exists():
                preserved.append(relative)
                continue
            self.locks.atomic_write(path, content)
            created.append(relative)

        if launcher_path.is_file():
            launcher_path.chmod(launcher_path.stat().st_mode | 0o111)
        # Nothing else creates the user level's directory, and a user file is
        # easier to add once the place for it exists.
        user = user_directory()
        if not user.is_dir():
            user.mkdir(parents=True)
            created.append(
                f"{display_path(user, self.root)} (user configuration directory)"
            )
        self.runtime_path.mkdir(parents=True, exist_ok=True)
        tasks = self.runtime_path / "tasks"
        if tasks.exists():
            preserved.append(".ww/tasks")
        else:
            tasks.mkdir(parents=True)
            created.append(".ww/tasks")

        if ignore_runtime:
            change = self._ignore_runtime_directory()
            if change:
                created.append(change)
            else:
                preserved.append(".gitignore entries for .ww")
        # Local configuration belongs to one checkout, so it is always kept
        # out of version control.
        added = self._ignore_local_configuration()
        if added:
            created.append(".gitignore entries: " + ", ".join(added))
        elif (self.root / ".gitignore").exists():
            preserved.append(".gitignore entries: " + ", ".join(LOCAL_IGNORE_PATTERNS))

        actions = self._initialization_actions()
        if (
            instructions_path.exists()
            and instructions_path.read_text(encoding="utf-8") != agent_instructions
        ):
            actions.append(
                "WW_AGENT_INSTRUCTIONS.md was preserved; review it before use. "
                "Current start commands require --requirements."
            )
        return InitializationResult(
            str(self.root), tuple(created), tuple(preserved), tuple(actions)
        )

    def _write_starter_workflows(self, defaults: str) -> None:
        """Write the starter YAML without the keys another level provides."""
        if not any(
            level.path.is_file()
            for level in workflow_levels(self.config_path)
            if level.name != "repo"
        ):
            self.locks.atomic_write(self.config_path, defaults)
            return
        import yaml

        # Compose the other levels around an empty repo file first.
        self.locks.atomic_write(self.config_path, "{}\n")
        try:
            defined = compose_configuration(self.config_path).raw
        except BaseException:
            self.config_path.unlink(missing_ok=True)
            raise
        desired = yaml.safe_load(defaults)
        missing = {key: value for key, value in desired.items() if key not in defined}
        if missing == desired:
            self.locks.atomic_write(self.config_path, defaults)
        elif missing:
            self.locks.atomic_write(
                self.config_path, yaml.safe_dump(missing, sort_keys=False)
            )

    def _add_missing_workflow_defaults(self, defaults: str) -> bool:
        """Append absent root keys while leaving existing YAML formatting intact."""
        import yaml

        try:
            existing = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
            desired = yaml.safe_load(defaults)
        except (OSError, yaml.YAMLError) as error:
            raise ConfigurationError(f"invalid {self.config_path}: {error}") from error
        if not isinstance(existing, dict) or not isinstance(desired, dict):
            raise ConfigurationError(f"{self.config_path} must contain a mapping")
        # A key an imported file defines is present too.
        defined = compose_configuration(self.config_path).raw
        missing = {key: value for key, value in desired.items() if key not in defined}
        if not missing:
            return False
        current = self.config_path.read_text(encoding="utf-8")
        separator = "" if not current or current.endswith("\n") else "\n"
        addition = yaml.safe_dump(missing, sort_keys=False)
        self.locks.atomic_write(self.config_path, f"{current}{separator}{addition}")
        return True

    def _add_missing_project_settings(self, defaults: str) -> bool:
        try:
            existing = json.loads(self.project_config_path.read_text(encoding="utf-8"))
            desired = json.loads(defaults)
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigurationError(
                f"invalid {self.project_config_path}: {error}"
            ) from error
        if not isinstance(existing, dict) or not isinstance(desired, dict):
            raise ConfigurationError(
                f"{self.project_config_path} must contain a JSON object"
            )

        def merge(target: dict[str, Any], source: dict[str, Any]) -> bool:
            changed = False
            for key, value in source.items():
                if key not in target:
                    target[key] = value
                    changed = True
                elif isinstance(target[key], dict) and isinstance(value, dict):
                    changed = merge(target[key], value) or changed
            return changed

        if not merge(existing, desired):
            return False
        self.locks.atomic_write(
            self.project_config_path, json.dumps(existing, indent=2) + "\n"
        )
        return True

    def _ignore_runtime_directory(self) -> str | None:
        """Keep ``.ww`` out of Git but for the files a team shares.

        Every line that ignores ``.ww`` whole (:data:`WHOLE_RUNTIME_IGNORE_LINES`)
        gives way to :data:`RUNTIME_IGNORE_LINES`, written once where the first
        stood; a ``.ww/*`` line gains the re-inclusions it lacks after it, and
        every other line is left alone. Returns what changed, or ``None``.
        """
        # Reads and rewrites .gitignore; the project scope held by
        # initialize_project keeps that pair together.
        path = self.root / ".gitignore"
        existing = _read_gitignore(path)
        updated, added, replaced = with_runtime_ignored(existing)
        if updated == existing:
            return None
        self.locks.atomic_write(path, updated)
        if not added:
            return f".gitignore: removed {', '.join(replaced)}, which hid .ww/*"
        change = ".gitignore entries: " + ", ".join(added)
        if replaced:
            change += f" (replacing {', '.join(replaced)})"
        return change

    def _ignore_local_configuration(self) -> tuple[str, ...]:
        """Add the local-file patterns missing from .gitignore.

        An existing .gitignore gains them; one is created only in a Git
        checkout, where it has an effect.
        """
        path = self.root / ".gitignore"
        if not path.exists() and not (self.root / ".git").exists():
            return ()
        existing = _read_gitignore(path)
        entries = {line.strip() for line in existing.splitlines()}
        missing = tuple(
            pattern for pattern in LOCAL_IGNORE_PATTERNS if pattern not in entries
        )
        if missing:
            self.locks.atomic_write(path, _appended(existing, missing))
        return missing

    def _initialization_actions(self) -> list[str]:
        actions: list[str] = []
        reference = "@WW_AGENT_INSTRUCTIONS.md"
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = self.root / name
            if not path.exists():
                if name == "AGENTS.md":
                    actions.append(f"Create {name} and add {reference}.")
            elif reference not in path.read_text(encoding="utf-8"):
                actions.append(f"Add {reference} to {name}.")
        try:
            raw = compose_configuration(self.config_path).raw
        except (OSError, ConfigurationError):
            raw = {}
        if not raw.get("workflows"):
            actions.append(NO_WORKFLOWS_ACTION)
        return actions

    def append_log(self, record: dict[str, Any]) -> None:
        """Append a non-sensitive audit record with owner-only permissions."""
        path = self.runtime_path / "executions.jsonl"
        line = json.dumps(record, sort_keys=True) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.locks.lock(path, purpose="execution log"):
            size = path.stat().st_size if path.exists() else 0
            if size and size + len(line.encode("utf-8")) > EXECUTION_LOG_MAX_BYTES:
                self._rotate_execution_logs(path)
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            # Existing logs might have been created by an older release with
            # the user's umask. Repair them on every append as part of the
            # audit log's confidentiality contract.
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    @staticmethod
    def _rotate_execution_logs(path: Path) -> None:
        """Retain a bounded number of complete invocation logs."""
        oldest = path.with_name(f"{path.name}.{EXECUTION_LOG_RETENTION}")
        oldest.unlink(missing_ok=True)
        for index in range(EXECUTION_LOG_RETENTION - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                source.replace(path.with_name(f"{path.name}.{index + 1}"))
        if path.exists():
            path.replace(path.with_name(f"{path.name}.1"))

    def cleanup_locks(self) -> int:
        """Remove inactive ww lock sidecars while excluding active commands."""
        return self.locks.cleanup()

    def read_bootstrap(self, request_id: str) -> dict[str, Any] | None:
        """Read an unresolved external-task identity request."""
        path = self.runtime_path / "bootstrap" / f"{request_id}.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"invalid bootstrap request {path}: {error}") from error
        if not isinstance(value, dict):
            raise StateError(f"invalid bootstrap request {path}: expected a mapping")
        return value

    def write_bootstrap(self, request_id: str, value: dict[str, Any]) -> None:
        """Persist one unresolved external-task identity request."""
        self.locks.atomic_write(
            self.runtime_path / "bootstrap" / f"{request_id}.json",
            json.dumps(value, indent=2) + "\n",
        )


def with_runtime_ignored(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """``text`` keeping ``.ww`` out of Git but for the shared files.

    Returns the new text, the lines it adds, and the whole-directory lines it
    replaces.
    A re-inclusion counts only after the last ``.ww/*`` line, since a later
    ``.ww/*`` would ignore the file again. The file's line ending is kept.
    """
    newline = newline_of(text)
    star, *shared = RUNTIME_IGNORE_LINES
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline
    whole = [
        line.strip() for line in lines if line.strip() in WHOLE_RUNTIME_IGNORE_LINES
    ]
    added: list[str] = []
    if whole:
        has_star = star in (line.strip() for line in lines)
        kept: list[str] = []
        for line in lines:
            if line.strip() not in WHOLE_RUNTIME_IGNORE_LINES:
                kept.append(line)
            elif not has_star:
                kept.extend(f"{entry}{newline}" for entry in RUNTIME_IGNORE_LINES)
                added.extend(RUNTIME_IGNORE_LINES)
                has_star = True
        lines = kept
    entries = [line.strip() for line in lines]
    if star not in entries:
        lines.extend(f"{entry}{newline}" for entry in RUNTIME_IGNORE_LINES)
        added.extend(RUNTIME_IGNORE_LINES)
    else:
        last = len(entries) - 1 - entries[::-1].index(star)
        missing = [entry for entry in shared if entry not in entries[last + 1 :]]
        # After the re-inclusions that already follow it, keeping them together.
        position = last + 1
        while position < len(entries) and entries[position].startswith("!.ww/"):
            position += 1
        lines[position:position] = [f"{entry}{newline}" for entry in missing]
        added.extend(entry for entry in missing if entry not in added)
    updated = "".join(lines)
    if not whole and not added:
        return text, (), ()
    return updated, tuple(added), tuple(dict.fromkeys(whole))


def _read_gitignore(path: Path) -> str:
    """A .gitignore's text with its own line endings, or ``""`` when absent."""
    return path.read_bytes().decode("utf-8") if path.exists() else ""


def _appended(text: str, entries: tuple[str, ...]) -> str:
    """``text`` with ``entries`` appended as lines, in its own line ending."""
    newline = newline_of(text)
    separator = "" if not text or text.endswith("\n") else newline
    return text + separator + "".join(f"{entry}{newline}" for entry in entries)
