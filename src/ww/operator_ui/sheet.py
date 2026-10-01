# SPDX-License-Identifier: GPL-3.0-or-later
"""The answer sheet: what the operator answered and ww has not applied yet.

Answers are operator input the engine has not acted on, so they stay out of
the task's state.  They live in a file of their own under
``.ww/operator-ui/``, keyed by run and item, written the moment they are
given and removed once applied.  The file remembers when the task was
created; a sheet left behind by a reset task is discarded, not applied to
the task that reuses the ID.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ww.storage import Storage

_FORMAT = "ww.operator-ui"


@dataclass(frozen=True)
class Answer:
    """One pending answer: the pick, when the stage offers choices, a comment,
    and when it was given."""

    choice: str | None
    comment: str
    at: str

    def to_dict(self) -> dict[str, object]:
        return {"choice": self.choice, "comment": self.comment, "at": self.at}

    @classmethod
    def from_dict(cls, data: Any) -> Answer:
        if not isinstance(data, dict):
            raise ValueError("an answer must be a mapping")
        choice = data.get("choice")
        comment = data.get("comment", "")
        at = data.get("at")
        if (
            (choice is not None and not isinstance(choice, str))
            or not isinstance(comment, str)
            or not isinstance(at, str)
        ):
            raise ValueError("an answer has a choice or null, a comment, and a time")
        return cls(choice, comment, at)


class AnswerSheet:
    """The per-task file of answers waiting to be applied."""

    def __init__(self, storage: Storage, task_id: str, task_created_at: str) -> None:
        self.storage = storage
        self.task_id = task_id
        self.task_created_at = task_created_at

    @property
    def path(self) -> Path:
        return self.storage.runtime_path / "operator-ui" / f"{self.task_id}.json"

    def read(self, run_id: str | None) -> dict[str, Answer]:
        """The pending answers of one run, by item ID."""
        run = self._load().get(run_id or "-", {})
        return {item_id: Answer.from_dict(value) for item_id, value in run.items()}

    def record(self, run_id: str | None, item_id: str, answer: Answer) -> None:
        """Record or replace one item's answer in one atomic file replacement."""
        runs = self._load()
        runs.setdefault(run_id or "-", {})[item_id] = answer.to_dict()
        self._save(runs)

    def remove(self, run_id: str | None, item_id: str) -> None:
        runs = self._load()
        runs.get(run_id or "-", {}).pop(item_id, None)
        self._save(runs)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("format") != _FORMAT:
            raise ValueError(f"{self.path} is not an operator page answer sheet")
        if data.get("task_created_at") != self.task_created_at:
            # Left behind by a task that was reset; it belongs to nothing now.
            return {}
        runs = data.get("runs", {})
        if not isinstance(runs, dict) or not all(
            isinstance(run, dict) for run in runs.values()
        ):
            raise ValueError(f"{self.path} has invalid runs")
        return runs

    def _save(self, runs: dict[str, dict[str, Any]]) -> None:
        document = {
            "format": _FORMAT,
            "task_created_at": self.task_created_at,
            "runs": runs,
        }
        self.storage.locks.atomic_write(
            self.path, json.dumps(document, indent=2) + "\n"
        )
