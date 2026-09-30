# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from ww.errors import StateError
from ww.task_references import TaskResolution, resolve_task_reference

KNOWN = ("FORMS-12345", "FORMS-2", "OPS-7", "OTHER-7")


@pytest.mark.parametrize(
    ("reference", "task_format", "expected"),
    [
        # The format's slot takes the number people quote...
        ("12345", "FORMS-{{digit}}", TaskResolution("12345", ("FORMS-12345",), None)),
        # ...in any letter case, and the exact ID still wins.
        ("forms-12345", "FORMS-{{digit}}", ("FORMS-12345",)),
        ("FORMS-2", "FORMS-{{digit}}", ("FORMS-2",)),
        # Tracker keys: the one task ending in the number after a separator.
        ("12345", "explicit", ("FORMS-12345",)),
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
        ("99", "FORMS-{{digit}}", "FORMS-99"),
        ("forms-99", "FORMS-{{digit}}", "FORMS-99"),
        # A value the slot cannot hold is proposed as written.
        ("abc", "FORMS-{{digit}}", "abc"),
        ("FORMS-9", "explicit", "FORMS-9"),
        ("20260928120000", None, "TASK-20260928120000"),
        ("not a task id", "FORMS-{{digit}}", None),
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
        resolve_task_reference("  ", "FORMS-{{digit}}", KNOWN)
