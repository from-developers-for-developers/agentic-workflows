# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for operator confirmations read from the terminal."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ww.cli.prompts import (
    _confirm_force_next,
    confirm_interrupted_retry,
)


def _closed_stdin(_prompt: str) -> str:
    raise EOFError


def _unread(_prompt: str) -> str:
    raise AssertionError("ww must not read a confirmation without a terminal")


CONFIRMATIONS = [
    (confirm_interrupted_retry, "Retry"),
    (_confirm_force_next, "Force"),
]


@pytest.mark.parametrize(("confirm", "name"), CONFIRMATIONS)
def test_without_a_terminal_ww_refuses_at_once_and_names_yes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    confirm: Callable[..., bool],
    name: str,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", _unread)

    assert confirm() is False
    err = capsys.readouterr().err
    assert f"ww error: {name.lower()} needs the operator's confirmation" in err
    assert "rerun with --yes only once they have agreed" in err


@pytest.mark.parametrize(("confirm", "name"), CONFIRMATIONS)
def test_yes_confirms_without_asking(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    confirm: Callable[..., bool],
    name: str,
) -> None:
    monkeypatch.setattr("builtins.input", _unread)

    assert confirm(assume_yes=True) is True
    assert "--yes" in capsys.readouterr().err


@pytest.mark.parametrize(("confirm", "name"), CONFIRMATIONS)
def test_at_a_terminal_a_closed_stdin_declines_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    confirm: Callable[..., bool],
    name: str,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _closed_stdin)

    assert confirm() is False
    assert f"{name} cancelled: explicit confirmation is required." in (
        capsys.readouterr().err
    )
