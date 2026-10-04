# SPDX-License-Identifier: GPL-3.0-or-later
"""What ww knows about how far the operator and the project are set up.

Two places hold it, each for what it describes:

- the user level, ``state.json`` in the user configuration directory:
  ``explain``, whether the operator wants the agent to narrate what ww does
  while it learns (absent until they say so);
- the project, in ``.ww/metadata.json`` under ww's own ``ww.`` namespace, which
  no workflow can save into: ``setup.done``, and when ww last learned about
  the project (``learned.project``).

``ww onboarding`` shows both and sets a known key; it records the operator's
stated preference, so it asks for no confirmation. ``discover`` reads it to
tell an agent whether to mention setup on a first use.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ww.config_files import display_path, user_directory
from ww.errors import StateError
from ww.locking import FileLocks
from ww.storage_adapters.base import ProjectMetadata, ProjectMetadataStorage
from ww.workflow_config import WW_METADATA_NAMESPACE

USER_STATE_FILE = "state.json"
EXPLAIN = "explain"
SETUP_DONE = "setup.done"
LEARNED = "learned"
# What ww learns about, and at which level it records when it did.
LEARNED_USER: tuple[str, ...] = ()
LEARNED_PROJECT = ("project",)
USER_KEYS = (EXPLAIN, *(f"{LEARNED}.{name}" for name in LEARNED_USER))
PROJECT_KEYS = (SETUP_DONE, *(f"{LEARNED}.{name}" for name in LEARNED_PROJECT))
KEYS = (*USER_KEYS, *PROJECT_KEYS)
NOW = "now"
_BOOLEANS = {"true": True, "false": False}


@dataclass(frozen=True)
class OnboardingState:
    """Both levels' onboarding keys; ``None`` is a key never set."""

    user_file: Path
    project_file: Path
    explain: bool | None
    setup_done: bool
    # When ww last learned about each subject, as an ISO timestamp.
    learned: dict[str, str | None]

    def to_dict(self) -> dict[str, object]:
        return {
            "user": {
                "file": str(self.user_file),
                EXPLAIN: self.explain,
                **{f"{LEARNED}.{name}": self.learned[name] for name in LEARNED_USER},
            },
            "project": {
                "file": str(self.project_file),
                SETUP_DONE: self.setup_done,
                **{f"{LEARNED}.{name}": self.learned[name] for name in LEARNED_PROJECT},
            },
        }


class Onboarding:
    """Read and set the onboarding keys of one project and its user."""

    def __init__(self, root: Path, project_metadata: ProjectMetadataStorage) -> None:
        self.root = root
        self.project_metadata = project_metadata

    @property
    def user_file(self) -> Path:
        return user_directory() / USER_STATE_FILE

    @property
    def project_file(self) -> Path:
        return self.root / ".ww" / "metadata.json"

    def read(self) -> OnboardingState:
        user = self._read_user()
        project = self._read_project()
        explain = user.get(EXPLAIN)
        learned_user = user.get(LEARNED)
        learned_user = learned_user if isinstance(learned_user, dict) else {}
        return OnboardingState(
            self.user_file,
            self.project_file,
            explain if isinstance(explain, bool) else None,
            project.get(SETUP_DONE) == "true",
            {
                **{name: _text(learned_user.get(name)) for name in LEARNED_USER},
                **{name: project.get(f"{LEARNED}.{name}") for name in LEARNED_PROJECT},
            },
        )

    def set(self, assignments: Sequence[str]) -> OnboardingState:
        """Apply ``KEY=VALUE`` assignments, all checked before any is written."""
        values = dict(parse_assignment(item) for item in assignments)
        user = {key: value for key, value in values.items() if key in USER_KEYS}
        project = {key: value for key, value in values.items() if key in PROJECT_KEYS}
        if user:
            self._write_user(user)
        if project:
            self._write_project(project)
        return self.read()

    def _read_user(self) -> dict[str, object]:
        path = self.user_file
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"invalid onboarding state {path}: {error}") from error
        if not isinstance(data, dict):
            raise StateError(f"invalid onboarding state {path}: not an object")
        return data

    def _write_user(self, values: dict[str, bool | str]) -> None:
        data = self._read_user()
        for key, value in values.items():
            if key == EXPLAIN:
                data[EXPLAIN] = value
                continue
            learned = data.get(LEARNED)
            learned = dict(learned) if isinstance(learned, dict) else {}
            learned[key.removeprefix(f"{LEARNED}.")] = value
            data[LEARNED] = learned
        # The user directory is shared by every project; the write only has
        # to be atomic for a concurrent reader.
        FileLocks(self.root).atomic_write(
            self.user_file, json.dumps(data, indent=2, sort_keys=True) + "\n"
        )

    def _read_project(self) -> dict[str, str]:
        metadata = self.project_metadata.read_project_metadata()
        prefix = f"{WW_METADATA_NAMESPACE}."
        return {
            key.removeprefix(prefix): value
            for key, value in (metadata.values if metadata is not None else ())
            if key.startswith(prefix) and isinstance(value, str)
        }

    def _write_project(self, values: dict[str, bool | str]) -> None:
        with self.project_metadata.lock_project_metadata():
            metadata = self.project_metadata.read_project_metadata()
            current = dict(metadata.values if metadata is not None else ())
            for key, value in values.items():
                current[f"{WW_METADATA_NAMESPACE}.{key}"] = (
                    ("true" if value else "false") if isinstance(value, bool) else value
                )
            try:
                updated = ProjectMetadata(tuple(current.items()))
            except ValueError as error:
                raise StateError(
                    f"cannot record onboarding in {self.project_file}: {error}"
                ) from error
            self.project_metadata.write_project_metadata(updated)


def render_onboarding(state: OnboardingState, root: Path) -> str:
    """The keys of both levels as text, each file named where it lives."""

    def when(name: str) -> str:
        return state.learned[name] or "never"

    explain = {True: "yes", False: "no", None: "not asked yet"}[state.explain]
    return "\n".join(
        [
            "# ww onboarding",
            "",
            f"User ({display_path(state.user_file, root)}):",
            f"- {EXPLAIN}: {explain}",
            *(f"- {LEARNED}.{name}: {when(name)}" for name in LEARNED_USER),
            "",
            f"Project ({display_path(state.project_file, root)}, ww's own keys):",
            f"- {SETUP_DONE}: {'yes' if state.setup_done else 'no'}",
            *(f"- {LEARNED}.{name}: {when(name)}" for name in LEARNED_PROJECT),
            "",
            "Record a key with `ww onboarding --set KEY=VALUE`.",
            "",
        ]
    )


def parse_assignment(text: str) -> tuple[str, bool | str]:
    """One ``KEY=VALUE``: a known key and its value, checked."""
    key, separator, value = text.partition("=")
    key, value = key.strip(), value.strip()
    if not separator:
        raise StateError(f"--set takes KEY=VALUE, not {text!r}")
    if key not in KEYS:
        raise StateError(
            f"unknown onboarding key {key!r}; the keys are " + ", ".join(KEYS)
        )
    if key in {EXPLAIN, SETUP_DONE}:
        if value not in _BOOLEANS:
            raise StateError(f"{key} takes true or false, not {value!r}")
        return key, _BOOLEANS[value]
    if value == NOW:
        return key, _now()
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StateError(
            f"{key} takes `now` or an ISO timestamp, not {value!r}"
        ) from error
    return key, value


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
