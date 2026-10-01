# SPDX-License-Identifier: GPL-3.0-or-later
"""Map what a person calls a task onto the task IDs this project uses.

People rarely type an ID the way ww stores it. With ``task_format:
FOOBAR-{{digit}}`` they say "12345" or "foobar-12345" and mean ``FOOBAR-12345``;
with tracker keys (``task_format: explicit``) "12345" usually means the one
existing task whose key ends in it. Resolution is deterministic and read-only:
it proposes, and the caller decides what to do with the answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from ww.errors import StateError
from ww.task_ids import (
    EXPLICIT_TASK_FORMAT,
    GENERATED_TASK_PREFIX,
    validate_task_id,
)

# A task format placeholder, e.g. "{{digit}}" in "TASK-{{digit}}".
_PLACEHOLDER = re.compile(r"\{\{(?:digit|timestamp|uuid)\}\}")
# The values each placeholder generates; a reference fills a slot only with one.
_SLOT_VALUES = {
    # A counter, e.g. "42".
    "{{digit}}": re.compile(r"\d+"),
    # A 14-digit timestamp with an optional "-N" suffix, e.g. "20261001120000-2".
    "{{timestamp}}": re.compile(r"\d{14}(?:-\d+)?"),
    # A UUID with an optional "-N" suffix, e.g.
    # "123e4567-e89b-12d3-a456-426614174000".
    "{{uuid}}": re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:-\d+)?",
        re.IGNORECASE,
    ),
}
# What separates a tracker's project key from the number people quote.
_SEPARATORS = ("-", "_", "/")


@dataclass(frozen=True)
class TaskResolution:
    """The tasks a reference may mean.

    ``matches`` are existing tasks, best first. ``proposed`` is the ID a new
    task would get for this reference, when the reference can name one.
    """

    reference: str
    matches: tuple[str, ...]
    proposed: str | None

    @property
    def resolved(self) -> str | None:
        """The one existing task the reference means, if it is unambiguous."""
        return self.matches[0] if len(self.matches) == 1 else None


def resolve_task_reference(
    reference: str, task_format: str | None, known: Iterable[str]
) -> TaskResolution:
    """Resolve ``reference`` against ``task_format`` and the ``known`` task IDs.

    An exact ID, the same ID in other letter case, and the ID the task format
    builds from the reference are strong matches, and the first found wins.
    Only without one do existing IDs ending in the reference after a
    separator count, so "12345" finds ``FOOBAR-12345`` but not ``FOOBAR-112345``.
    """
    text = reference.strip()
    if not text:
        raise StateError("a task reference must not be empty")
    ids = tuple(known)
    by_folded = {task_id.casefold(): task_id for task_id in ids}
    formatted = _formatted(text, task_format)
    strong = [
        found
        for candidate in (text, formatted)
        if candidate is not None
        and (found := by_folded.get(candidate.casefold())) is not None
    ]
    if strong:
        return TaskResolution(text, (strong[0],), None)
    folded = text.casefold()
    weak = tuple(
        task_id
        for task_id in ids
        if any(
            task_id.casefold().endswith(separator + folded) for separator in _SEPARATORS
        )
    )
    return TaskResolution(text, weak, _valid(formatted or text))


def _formatted(text: str, task_format: str | None) -> str | None:
    """The ID ``task_format`` gives the reference, when it has one slot.

    A reference already carrying the format's prefix keeps its own value and
    takes the format's spelling of the prefix.
    """
    if task_format == EXPLICIT_TASK_FORMAT:
        return None
    template = task_format or GENERATED_TASK_PREFIX + "{{timestamp}}"
    slots = _PLACEHOLDER.findall(template)
    if len(slots) != 1:
        return None
    prefix, suffix = _PLACEHOLDER.split(template)
    value = text
    if prefix and value.casefold().startswith(prefix.casefold()):
        value = value[len(prefix) :]
    if suffix and value.casefold().endswith(suffix.casefold()):
        value = value[: -len(suffix)]
    if not _SLOT_VALUES[slots[0]].fullmatch(value):
        return None
    return f"{prefix}{value}{suffix}"


def _valid(task_id: str) -> str | None:
    try:
        validate_task_id(task_id)
    except StateError:
        return None
    return task_id
