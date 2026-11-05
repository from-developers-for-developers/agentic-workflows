# SPDX-License-Identifier: GPL-3.0-or-later
"""A task's one-line description: validated at start, derived for children."""

from __future__ import annotations

import pytest

from ww.errors import StateError
from ww.task_descriptions import (
    DESCRIPTION_LIMIT,
    description_from,
    task_description,
    validate_description,
)


def test_a_description_is_stored_trimmed() -> None:
    assert validate_description("  Add a flag \t") == "Add a flag"
    assert validate_description("x" * DESCRIPTION_LIMIT) == "x" * DESCRIPTION_LIMIT


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "non-empty"),
        (" \n ", "non-empty"),
        ("one\ntwo", "single line"),
        ("one\rtwo", "single line"),
        ("x" * (DESCRIPTION_LIMIT + 1), "keep the detail in --requirements"),
    ],
)
def test_a_blank_multi_line_or_long_description_is_refused(
    text: str, message: str
) -> None:
    with pytest.raises(StateError, match=message):
        validate_description(text)


def test_a_child_s_description_is_the_first_line_of_its_own() -> None:
    assert description_from("\n  Split the parser  \nThen the rest.") == (
        "Split the parser"
    )
    assert description_from(" \n ") is None
    shortened = description_from("y" * (DESCRIPTION_LIMIT + 10))
    assert shortened == "y" * (DESCRIPTION_LIMIT - 1) + "…"
    assert shortened is not None and len(shortened) == DESCRIPTION_LIMIT


def test_the_task_s_description_is_the_first_a_run_recorded() -> None:
    assert task_description([None, "First", "Second"]) == "First"
    assert task_description([None, ""]) is None
    assert task_description([]) is None
