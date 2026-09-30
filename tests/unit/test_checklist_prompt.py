# SPDX-License-Identifier: GPL-3.0-or-later
"""Key handling for the multi-select checklist, without needing a terminal."""

from __future__ import annotations

import pytest

from ww.cli.prompts import _apply_key


def _press(keys: str, checked: list[bool]) -> tuple[int, list[bool], bool]:
    cursor, done = 0, False
    for key in keys:
        cursor, checked, done = _apply_key(key, cursor, checked)
        if done:
            break
    return cursor, checked, done


def test_space_toggles_the_row_under_the_cursor() -> None:
    _, checked, _ = _press(" ", [False, False, False])

    assert checked == [True, False, False]


def test_moving_down_then_toggling_marks_the_second_row() -> None:
    _, checked, _ = _press("j ", [False, False, False])

    assert checked == [False, True, False]


def test_the_cursor_wraps_at_both_ends() -> None:
    cursor, _, _ = _press("k", [False, False, False])
    assert cursor == 2

    cursor, _, _ = _press("jjj", [False, False, False])
    assert cursor == 0


def test_a_selects_everything_then_clears_everything() -> None:
    _, checked, _ = _press("a", [False, True, False])
    assert checked == [True, True, True]

    _, checked, _ = _press("aa", [False, True, False])
    assert checked == [False, False, False]


def test_a_row_can_be_unchecked_again() -> None:
    _, checked, _ = _press("  ", [False, False])

    assert checked == [False, False]


@pytest.mark.parametrize("key", ["\r", "\n"])
def test_enter_finishes_and_keeps_the_marks(key: str) -> None:
    _, checked, done = _press(f" j {key}", [False, False, False])

    assert done is True
    assert checked == [True, True, False]


def test_an_unknown_key_changes_nothing() -> None:
    cursor, checked, done = _apply_key("z", 1, [False, True])

    assert (cursor, checked, done) == (1, [False, True], False)


def test_interrupt_is_not_swallowed() -> None:
    with pytest.raises(KeyboardInterrupt):
        _apply_key("\x03", 0, [False])


def _notice(columns: int) -> list[str]:
    import os
    import re
    from unittest.mock import patch

    from ww.output_adapters.markdown import _permission_notice

    with patch(
        "shutil.get_terminal_size", return_value=os.terminal_size((columns, 24))
    ):
        lines = _permission_notice()
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


@pytest.mark.parametrize("columns", [44, 60, 80, 120])
def test_the_permission_notice_fits_the_terminal(columns: int) -> None:
    """It is a box, so an overflowing line breaks the drawing itself."""
    lines = _notice(columns)

    assert max(len(line) for line in lines) <= columns
    assert any("Allow ww to run without confirmation" in line for line in lines)


@pytest.mark.parametrize("columns", [44, 60, 80, 120])
def test_the_permission_notice_box_is_closed(columns: int) -> None:
    """An open right edge reads as broken rendering, not as emphasis."""
    rows = [line for line in _notice(columns) if line[:1] in {"┌", "│", "└"}]

    assert rows[0].startswith("┌") and rows[0].endswith("┐")
    assert rows[-1].startswith("└") and rows[-1].endswith("┘")
    for row in rows[1:-1]:
        assert row.startswith("│") and row.endswith("│"), row
    # Every edge lines up only if each row is drawn to the same width.
    assert len({len(row) for row in rows}) == 1
