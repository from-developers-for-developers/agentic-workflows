# SPDX-License-Identifier: GPL-3.0-or-later
"""Interpolation analysis for normalized workflow definitions."""

from __future__ import annotations

import re
from collections.abc import Mapping

from ww.errors import ConfigurationError

_TOKEN = re.compile(
    # Matches {{name}} and dotted references such as {{ww.metadata.github.owner}}.
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)\s*\}\}"
)
_BRACES = re.compile(r"\{\{.*?\}\}")


def dependencies(value: str) -> tuple[str, ...]:
    """Return ordered interpolation names and reject malformed tokens."""
    names = tuple(match.group(1) for match in _TOKEN.finditer(value))
    normalized = _TOKEN.sub("", value)
    if "{{" in normalized or "}}" in normalized or _BRACES.search(normalized):
        raise ConfigurationError(f"invalid interpolation in {value!r}")
    return names


def interpolate(value: str, variables: Mapping[str, str]) -> str:
    """Bind values that are available, preserving declared unresolved tokens."""
    dependencies(value)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return variables.get(name, match.group(0))

    return _TOKEN.sub(replace, value)
