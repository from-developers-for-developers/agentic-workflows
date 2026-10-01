# SPDX-License-Identifier: GPL-3.0-or-later
"""Primitive value checks shared by YAML parsing and persisted-record decoding.

Both layers reject rather than coerce.  They differ only in the exception they
raise: authored YAML reports ``ConfigurationError`` with a document path, saved
records report ``ValueError`` with a field context.  Every helper therefore
takes the exception type as the ``error`` keyword.  This module is a leaf and
imports nothing else from ``ww``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, TypeGuard, get_args

# A normalized name, dots and hyphens allowed, e.g. "code-review" or "ww.git";
# "-review" does not match.
NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


def is_strict_int(value: object) -> TypeGuard[int]:
    """Return whether ``value`` is an ``int`` and not a ``bool``.

    ``bool`` subclasses ``int``, so a plain ``isinstance`` check would accept
    ``True`` where a count or version is expected.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def is_positive_int(value: object) -> TypeGuard[int]:
    return is_strict_int(value) and value > 0


def expect_mapping(
    value: object, context: str, *, error: type[Exception] = ValueError
) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise error(f"{context} must be a mapping")
    return value


def expect_optional_mapping(
    value: object, context: str, *, error: type[Exception] = ValueError
) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise error(f"{context} must be an object or null")
    return dict(value)


def expect_string(
    value: object, context: str, *, error: type[Exception] = ValueError
) -> str:
    if not isinstance(value, str):
        raise error(f"{context} must be a string")
    return value


def expect_optional_string(
    value: object, context: str, *, error: type[Exception] = ValueError
) -> str | None:
    if value is not None and not isinstance(value, str):
        raise error(f"{context} must be a string or null")
    return value


def expect_nonempty_string(
    value: object, context: str, *, error: type[Exception] = ValueError
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{context} must be a non-empty string")
    return value


def expect_normalized_name(
    value: object, context: str, *, error: type[Exception] = ValueError
) -> str:
    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
        raise error(f"{context} must be a non-empty normalized name")
    return value


def expect_bool(
    value: object, context: str, *, error: type[Exception] = ValueError
) -> bool:
    if not isinstance(value, bool):
        raise error(f"{context} must be a boolean")
    return value


def expect_positive_int(
    value: object, context: str, *, error: type[Exception] = ValueError
) -> int:
    if not is_positive_int(value):
        raise error(f"{context} must be a positive integer")
    return value


def expect_nonnegative_int(
    value: object, context: str, *, error: type[Exception] = ValueError
) -> int:
    if not is_strict_int(value) or value < 0:
        raise error(f"{context} must be a non-negative integer")
    return value


def expect_optional_int(
    value: object, context: str, *, error: type[Exception] = ValueError
) -> int | None:
    if value is not None and not is_strict_int(value):
        raise error(f"{context} must be an integer or null")
    return value


def expect_literal(
    value: object, literal: Any, context: str, *, error: type[Exception] = ValueError
) -> Any:
    """Return ``value`` if it is one of the members of a ``Literal`` alias.

    The result is typed ``Any`` because a ``Literal`` alias is not a runtime
    type; callers narrow it with the alias they passed.
    """
    allowed = get_args(literal)
    if value not in allowed:
        raise error(f"{context} must be one of {', '.join(map(repr, allowed))}")
    return value


def require_keys(
    data: Mapping[str, Any],
    required: Iterable[str],
    context: str,
    *,
    error: type[Exception] = ValueError,
) -> None:
    missing = set(required) - data.keys()
    if missing:
        raise error(f"{context} missing field(s): {', '.join(sorted(missing))}")


def reject_unknown_keys(
    data: Mapping[str, Any],
    allowed: Iterable[str],
    context: str,
    *,
    error: type[Exception] = ValueError,
) -> None:
    unknown = data.keys() - set(allowed)
    if unknown:
        raise error(f"{context} has unknown key(s): {', '.join(sorted(unknown))}")


def expect_keys(
    data: Mapping[str, Any],
    expected: Iterable[str],
    context: str,
    *,
    error: type[Exception] = ValueError,
) -> None:
    """Require every ``expected`` key; leave any other key alone.

    Used for stored data: a field ww does not know, such as one a newer
    version wrote, is ignored rather than refused.
    """
    if not set(expected) <= data.keys():
        raise error(f"{context} has invalid fields")
