# SPDX-License-Identifier: GPL-3.0-or-later
"""Every dispute a step's worker raised against a check, across tasks.

A worker whose completion a check rejected may dispute the check instead of
fixing its work (``ww dispute``); the operator then lets the check stand or
waives it for that step. The task keeps the open dispute on the step's record;
this log keeps every dispute ever raised in the project, so ``ww lint`` can
point at the rules and checks that keep being argued with without reading
every task's state.

The log is local history at ``.ww/rule-disputes.json``, beside the task
states it summarises. It is deliberately not part of the rule-automation
store: the store holds derived knowledge about rules, a dispute is an event.
A dispute is appended before the task state records it, so an interruption
between the two can leave one entry too many, never one too few.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ww.errors import StateError
from ww.locking import FileLocks
from ww.validation import expect_optional_string, expect_positive_int, expect_string

DISPUTES_FILE = ".ww/rule-disputes.json"
DISPUTES_SCHEMA_VERSION = 1
_ENTRY_KEYS = {
    "check",
    "text_hash",
    "task_id",
    "run_id",
    "step",
    "reason",
    "attempt",
    "disputed_at",
}


@dataclass(frozen=True)
class DisputeEntry:
    """One dispute: which check, of which step, why, and when.

    ``text_hash`` is the disputed rule's wording hash when the check is a
    rule's, so a dispute follows the wording like the store does.
    """

    check: str
    task_id: str
    step: str
    reason: str
    attempt: int
    disputed_at: str
    text_hash: str | None = None
    run_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "check": self.check,
            "text_hash": self.text_hash,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "step": self.step,
            "reason": self.reason,
            "attempt": self.attempt,
            "disputed_at": self.disputed_at,
        }

    @classmethod
    def from_dict(cls, data: Any, path: str) -> DisputeEntry:
        if not isinstance(data, dict):
            raise ValueError(f"{path} must be an object")
        unknown = set(data) - _ENTRY_KEYS
        if unknown:
            raise ValueError(f"{path} has unknown keys: " + ", ".join(sorted(unknown)))
        return cls(
            check=expect_string(data.get("check"), f"{path}.check"),
            text_hash=expect_optional_string(
                data.get("text_hash"), f"{path}.text_hash"
            ),
            task_id=expect_string(data.get("task_id"), f"{path}.task_id"),
            run_id=expect_optional_string(data.get("run_id"), f"{path}.run_id"),
            step=expect_string(data.get("step"), f"{path}.step"),
            reason=expect_string(data.get("reason"), f"{path}.reason"),
            attempt=expect_positive_int(data.get("attempt"), f"{path}.attempt"),
            disputed_at=expect_string(data.get("disputed_at"), f"{path}.disputed_at"),
        )


class DisputeLog:
    """Read and append ``.ww/rule-disputes.json``.

    A missing file is an empty log; a malformed one is an error, never an
    empty log. Appends are read-modify-writes under the file's own lock,
    taken inside the task lock of the disputing command.
    """

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / DISPUTES_FILE
        self.locks = FileLocks(Path(root))

    def load(self) -> tuple[DisputeEntry, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return _entries(raw)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise StateError(
                f"invalid rule dispute log {self.path}: {error}"
            ) from error

    def append(self, entry: DisputeEntry) -> None:
        with self.locks.lock(self.path, purpose="rule dispute log"):
            entries = (*self.load(), entry)
            self.locks.atomic_write(
                self.path,
                json.dumps(
                    {
                        "schema_version": DISPUTES_SCHEMA_VERSION,
                        "disputes": [item.to_dict() for item in entries],
                    },
                    indent=2,
                )
                + "\n",
            )


def _entries(raw: Any) -> tuple[DisputeEntry, ...]:
    if not isinstance(raw, dict):
        raise ValueError("the dispute log must be an object")
    if raw.get("schema_version") != DISPUTES_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported dispute log schema: {raw.get('schema_version')!r}"
        )
    unknown = set(raw) - {"schema_version", "disputes"}
    if unknown:
        raise ValueError("unknown dispute log keys: " + ", ".join(sorted(unknown)))
    disputes = raw.get("disputes", [])
    if not isinstance(disputes, list):
        raise ValueError("the dispute log's disputes must be a list")
    return tuple(
        DisputeEntry.from_dict(item, f"disputes[{index}]")
        for index, item in enumerate(disputes)
    )
