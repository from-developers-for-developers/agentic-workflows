# SPDX-License-Identifier: GPL-3.0-or-later
"""Amendments to a task's recorded requirements.

The requirements ``init`` saved are never rewritten; a later clarification is
appended as an amendment with its time and who recorded it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ww.validation import expect_nonempty_string, expect_string


@dataclass(frozen=True)
class Amendment:
    """One short addition to a task's requirements."""

    at: str
    role: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"at": self.at, "role": self.role, "text": self.text}

    @classmethod
    def from_dict(cls, data: object) -> Amendment:
        if not isinstance(data, dict):
            raise ValueError("amendment must be a mapping")
        return cls(
            at=expect_nonempty_string(data.get("at"), "amendment time"),
            role=expect_string(data.get("role"), "amendment role"),
            text=expect_nonempty_string(data.get("text"), "amendment text"),
        )


# Amendments are clarifications, not a second requirements document.
MAX_AMENDMENT_LENGTH = 1000


@dataclass(frozen=True)
class TaskRequirements:
    """A task's recorded requirements and the amendments appended to them."""

    task_id: str
    text: str | None
    amendments: tuple[Amendment, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "requirements": self.text,
            "amendments": [entry.to_dict() for entry in self.amendments],
        }
