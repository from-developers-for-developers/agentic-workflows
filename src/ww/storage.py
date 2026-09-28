# SPDX-License-Identifier: GPL-3.0-or-later
"""Filesystem persistence, using locked atomic state replacement."""

from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from ww.defaults import GENERATED_LAUNCHERS
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
        self.project_metadata = (
            project_metadata or FileProjectMetadataStorageAdapter(root)
        )

    def lock_project(self) -> AbstractContextManager[None]:
        """Serialize project-wide file changes against other ww processes."""
        return self.locks.lock(self.root, purpose="project files")

    @property
    def config_path(self) -> Path:
        return self.root / "workflows.yaml"

    @property
    def runtime_path(self) -> Path:
        return self.root / ".ww"

    @property
    def project_config_path(self) -> Path:
        return self.root / "agentic-workflows.json"

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
                created.append("workflows.yaml (added missing defaults)")
            else:
                preserved.append("workflows.yaml")
        else:
            self.locks.atomic_write(self.config_path, workflows)
            created.append("workflows.yaml")

        if self.project_config_path.exists():
            if self._add_missing_project_settings(project_config):
                created.append("agentic-workflows.json (added missing settings)")
            else:
                preserved.append("agentic-workflows.json")
        else:
            self.locks.atomic_write(self.project_config_path, project_config)
            created.append("agentic-workflows.json")

        updated_launcher = (
            launcher_path.is_file()
            and launcher_path.read_text(encoding="utf-8") in GENERATED_LAUNCHERS
        )
        if updated_launcher:
            # Written by an earlier ww and never edited: bring it up to date.
            self.locks.atomic_write(launcher_path, launcher)
            created.append("ww (updated launcher)")
        for path, content in (
            (instructions_path, agent_instructions),
            (launcher_path, launcher),
            *((self.root / relative, content) for relative, content in skills),
        ):
            relative = str(path.relative_to(self.root))
            if path == launcher_path and updated_launcher:
                continue
            if path.exists():
                preserved.append(relative)
                continue
            self.locks.atomic_write(path, content)
            created.append(relative)

        if launcher_path.is_file():
            launcher_path.chmod(launcher_path.stat().st_mode | 0o111)
        self.runtime_path.mkdir(parents=True, exist_ok=True)
        tasks = self.runtime_path / "tasks"
        if tasks.exists():
            preserved.append(".ww/tasks")
        else:
            tasks.mkdir(parents=True)
            created.append(".ww/tasks")

        if ignore_runtime:
            if self._ignore_runtime_directory():
                created.append(".gitignore entry: .ww/")
            else:
                preserved.append(".gitignore entry: .ww/")

        actions = self._initialization_actions()
        if (
            instructions_path.exists()
            and instructions_path.read_text(encoding="utf-8") != agent_instructions
        ):
            actions.append(
                "WW_AGENT_INSTRUCTIONS.md was preserved; review it before use. "
                "Current start commands require --init-artifact."
            )
        return InitializationResult(
            str(self.root), tuple(created), tuple(preserved), tuple(actions)
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
        missing = {key: value for key, value in desired.items() if key not in existing}
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
        existing_git = existing.get("extensions", {}).get("ww/git", {})
        desired_git = desired.get("extensions", {}).get("ww/git", {})
        if (
            isinstance(existing_git, dict)
            and isinstance(desired_git, dict)
            and "commit_format" in existing_git
        ):
            desired_git.pop("commit_message", None)

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

    def _ignore_runtime_directory(self) -> bool:
        # Reads and rewrites .gitignore; the project scope held by
        # initialize_project keeps that pair together.
        path = self.root / ".gitignore"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        entries = {line.strip() for line in existing.splitlines()}
        if ".ww/" in entries or ".ww" in entries:
            return False
        separator = "" if not existing or existing.endswith("\n") else "\n"
        self.locks.atomic_write(path, f"{existing}{separator}.ww/\n")
        return True

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
            import yaml

            raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        if not isinstance(raw, dict) or not raw.get("workflows"):
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
            descriptor = os.open(
                path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
            )
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
