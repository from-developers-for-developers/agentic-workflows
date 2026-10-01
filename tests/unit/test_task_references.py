# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolving a task reference to an existing or a new task ID."""

import pytest

from ww.errors import StateError
from ww.task_references import TaskResolution, resolve_task_reference

KNOWN = ("FOOBAR-12345", "FOOBAR-2", "OPS-7", "OTHER-7")


@pytest.mark.parametrize(
    ("reference", "task_format", "expected"),
    [
        # The format's slot takes the number people quote...
        ("12345", "FOOBAR-{{digit}}", TaskResolution("12345", ("FOOBAR-12345",), None)),
        # ...in any letter case, and the exact ID still wins.
        ("foobar-12345", "FOOBAR-{{digit}}", ("FOOBAR-12345",)),
        ("FOOBAR-2", "FOOBAR-{{digit}}", ("FOOBAR-2",)),
        # Tracker keys: the one task ending in the number after a separator.
        ("12345", "explicit", ("FOOBAR-12345",)),
        ("7", "explicit", ("OPS-7", "OTHER-7")),
        ("2345", "explicit", ()),
    ],
)
def test_references_resolve_to_existing_tasks(
    reference: str, task_format: str, expected: object
) -> None:
    resolution = resolve_task_reference(reference, task_format, KNOWN)

    if isinstance(expected, TaskResolution):
        assert resolution == expected
    else:
        assert resolution.matches == expected


@pytest.mark.parametrize(
    ("reference", "task_format", "proposed"),
    [
        ("99", "FOOBAR-{{digit}}", "FOOBAR-99"),
        ("foobar-99", "FOOBAR-{{digit}}", "FOOBAR-99"),
        # A value the slot cannot hold is proposed as written.
        ("abc", "FOOBAR-{{digit}}", "abc"),
        ("FOOBAR-9", "explicit", "FOOBAR-9"),
        ("20260928120000", None, "TASK-20260928120000"),
        ("not a task id", "FOOBAR-{{digit}}", None),
    ],
)
def test_an_unknown_reference_proposes_the_id_a_new_task_would_get(
    reference: str, task_format: str | None, proposed: str | None
) -> None:
    resolution = resolve_task_reference(reference, task_format, KNOWN)

    assert resolution.matches == ()
    assert resolution.resolved is None
    assert resolution.proposed == proposed


def test_an_empty_reference_is_refused() -> None:
    with pytest.raises(StateError, match="must not be empty"):
        resolve_task_reference("  ", "FOOBAR-{{digit}}", KNOWN)
