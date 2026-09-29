# SPDX-License-Identifier: GPL-3.0-or-later
"""Operator confirmations read from the terminal."""

from __future__ import annotations

import os
import sys
import termios
import tty


def _ask_choice(prompt: str, choices: tuple[str, ...], default: str) -> str:
    while True:
        value = input(prompt).strip().lower() or default
        if value in choices:
            return value
        print("Choose one of: " + ", ".join(choices) + ".")


def _interactive_terminal() -> bool:
    """Whether the operator can drive a redrawing prompt."""
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM") not in {None, "", "dumb"}
    )


def _ask_checklist(
    heading: str, options: tuple[tuple[str, str, bool], ...]
) -> tuple[str, ...]:
    """Toggle a list of ``(name, note, checked)`` rows and return the checked.

    One screen replaces one question per row. A terminal that cannot be driven
    this way -- a pipe, a dumb terminal, a captured test stdin -- never reaches
    here; the caller asks in plain text instead.
    """
    checked = [state for _, _, state in options]
    cursor = 0
    sys.stdout.write(f"{heading}\n")
    sys.stdout.write("  ↑↓ move · space toggles · a all · enter confirms\n\n")
    _draw(options, checked, cursor, first=True)
    settings = termios.tcgetattr(sys.stdin.fileno())
    try:
        tty.setraw(sys.stdin.fileno())
        while True:
            key = sys.stdin.read(1)
            # An arrow key arrives as an escape sequence; read its tail.
            if key == "\x1b" and sys.stdin.read(1) == "[":
                key = _ARROWS.get(sys.stdin.read(1), "")
            cursor, checked, done = _apply_key(key, cursor, checked)
            if done:
                break
            _draw(options, checked, cursor)
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, settings)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return tuple(
        name for (name, _, _), state in zip(options, checked, strict=True) if state
    )


_ARROWS = {"A": "k", "B": "j"}


def _apply_key(
    key: str, cursor: int, checked: list[bool]
) -> tuple[int, list[bool], bool]:
    """Fold one keypress into the checklist's state.

    Kept apart from the terminal so the behaviour can be tested without one.
    Returns the new cursor, the new marks, and whether the operator is done.
    """
    if key == "\x03":
        raise KeyboardInterrupt
    if key in {"\r", "\n"}:
        return cursor, checked, True
    count = len(checked)
    if key in {"j", "\t"}:
        return (cursor + 1) % count, checked, False
    if key == "k":
        return (cursor - 1) % count, checked, False
    if key == " ":
        toggled = list(checked)
        toggled[cursor] = not toggled[cursor]
        return cursor, toggled, False
    if key == "a":
        return cursor, [not all(checked)] * count, False
    return cursor, checked, False


def _draw(
    options: tuple[tuple[str, str, bool], ...],
    checked: list[bool],
    cursor: int,
    *,
    first: bool = False,
) -> None:
    """Repaint the rows in place, so the screen never scrolls away."""
    if not first:
        sys.stdout.write(f"\x1b[{len(options)}A")
    width = max(len(name) for name, _, _ in options)
    for index, (name, note, _) in enumerate(options):
        mark = "x" if checked[index] else " "
        pointer = ">" if index == cursor else " "
        label = f"{name.ljust(width)}  {note}" if note else name
        row = f" {pointer} [{mark}] {label}"
        sys.stdout.write(f"\r\x1b[K{row}\n")
    sys.stdout.flush()


def _ask_names(prompt: str, names: tuple[str, ...]) -> tuple[str, ...]:
    """Read zero or more of ``names``, comma-separated; empty means none.

    One question for a list beats one question per entry when the answer is
    almost always "none of them".
    """
    allowed = {name.lstrip(".").lower(): name for name in names}
    while True:
        value = input(prompt).strip().lower()
        # Someone answering a list of options with "n" plainly means none.
        if not value or value in {"none", "n", "no"}:
            return ()
        chosen = [part.strip().lstrip(".") for part in value.split(",")]
        if all(part in allowed for part in chosen if part):
            return tuple(
                dict.fromkeys(allowed[part] for part in chosen if part)
            )
        print("Choose from: " + ", ".join(names) + ", or none.")


def _ask_yes_no(prompt: str, default: bool) -> bool:
    while True:
        value = input(prompt).strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Answer yes or no.")


def _confirm_force_next(
    effect: str = "skip the current item without running it",
) -> bool:
    """Require an operator acknowledgement before forcing past work.

    ``effect`` is ww's own description of what this force will do, obtained
    after the task state was checked, so the operator approves a real action.
    """
    prompt = (
        f"`ww next --force` will {effect}.\n"
        "If you are an agent, you should never call this command without asking "
        "a permission; if you didn't get a permission, do NOT answer positively "
        "on it.\n"
        "Proceed with force? [y/N] "
    )
    try:
        confirmed = _ask_yes_no(prompt, default=False)
    except EOFError:
        print("Force cancelled: explicit confirmation is required.", file=sys.stderr)
        return False
    if not confirmed:
        print("Force cancelled: explicit confirmation is required.", file=sys.stderr)
    return confirmed


def confirm_approval(preview: str) -> bool:
    """Show what ``next --approve`` records, command in full, and ask for it.

    ww keeps no allowlist of executables: an approved check runs in every
    later step, so the operator reads it before it is recorded.
    """
    print(f"`ww next --approve` will approve:\n{preview}", file=sys.stderr)
    prompt = (
        "If you are an agent, you should never call this command without the "
        "operator's permission; if you didn't get it, do NOT answer positively.\n"
        "Approve? [y/N] "
    )
    try:
        confirmed = _ask_yes_no(prompt, default=False)
    except EOFError:
        confirmed = False
    if not confirmed:
        print("Approval cancelled: explicit confirmation is required.", file=sys.stderr)
    return confirmed


def confirm_interrupted_retry() -> bool:
    """Require an operator to acknowledge duplicate-effect risk."""
    prompt = (
        "This operation was interrupted and may already have taken effect. "
        "Retrying can duplicate its external effect. Proceed with retry? [y/N] "
    )
    try:
        confirmed = _ask_yes_no(prompt, default=False)
    except EOFError:
        confirmed = False
    if not confirmed:
        print("Retry cancelled: explicit confirmation is required.", file=sys.stderr)
    return confirmed
