# SPDX-License-Identifier: GPL-3.0-or-later
"""Decoding helpers specific to persisted execution records.

Primitive checks live in :mod:`ww.validation`; this module keeps only the
shapes that exist solely in saved task state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from ww.validation import is_positive_int

_T = TypeVar("_T")
_PAIR = 2


def _from_path(function: Callable[[Any], _T], value: Any, path: str) -> _T:
    try:
        return function(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{path}: {error}") from error


def _variables(value: Any, context: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(item, str) for name, item in value.items()
    ):
        raise ValueError(f"{context} must be a mapping of strings")
    return tuple(value.items())


def _positive_int_mapping(value: Any, context: str) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and is_positive_int(item) for name, item in value.items()
    ):
        raise ValueError(f"{context} must be a mapping of positive integers")
    return tuple(value.items())


def _assignment_log(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or not all(
        isinstance(entry, list)
        and len(entry) == _PAIR
        and all(isinstance(part, str) for part in entry)
        for entry in value
    ):
        raise ValueError("execution state.assignment_log must be a list of pairs")
    return tuple((entry[0], entry[1]) for entry in value)
