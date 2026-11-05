# SPDX-License-Identifier: GPL-3.0-or-later
"""A task's short description: one line the agent gives when it starts a task.

Each run keeps the description given at its start; the task's own is the
first one recorded, so a later run, such as a merge, does not rename it.
"""

from __future__ import annotations

from collections.abc import Iterable

from ww.errors import StateError

# Long enough for a sentence, short enough for one row of ``ww tasks``.
DESCRIPTION_LIMIT = 120


def validate_description(text: str) -> str:
    """The description as stored, or a ``StateError`` saying what is wrong."""
    description = text.strip()
    if not description:
        raise StateError("start requires a non-empty --description")
    if "\n" in description or "\r" in description:
        raise StateError("start --description must be a single line")
    if len(description) > DESCRIPTION_LIMIT:
        raise StateError(
            f"start --description must be at most {DESCRIPTION_LIMIT} characters, "
            f"not {len(description)}; keep the detail in --requirements"
        )
    return description


def description_from(text: str) -> str | None:
    """A description made from free text, such as a child's: its first line."""
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not line:
        return None
    if len(line) > DESCRIPTION_LIMIT:
        return line[: DESCRIPTION_LIMIT - 1].rstrip() + "…"
    return line


def task_description(descriptions: Iterable[str | None]) -> str | None:
    """The task's description: the first its runs recorded, oldest first."""
    return next((text for text in descriptions if text), None)
