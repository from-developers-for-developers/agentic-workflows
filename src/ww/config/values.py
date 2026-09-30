# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared YAML value validation for workflow configuration parsing."""

from __future__ import annotations

from typing import Any, cast, get_args

from ww.contracts import StepRole
from ww.errors import ConfigurationError
from ww.extensions import is_extension_reference
from ww.validation import (
    NAME_PATTERN as _NAME,
)
from ww.validation import (
    expect_mapping,
    expect_nonempty_string,
    expect_normalized_name,
    reject_unknown_keys,
)
from ww.workflow_config import ALL, ALL_NAMES, NameFilter


def _mapping(value: Any, path: str) -> dict[str, Any]:
    return expect_mapping(value, path, error=ConfigurationError)


def _named_entry(
    mapping: dict[str, Any],
    path: str,
    *,
    allowed: set[str] | None = None,
    ignored: set[str] | None = None,
) -> dict[str, Any]:
    """Expand ``name: description`` shorthand into the shared action shape."""
    if "name" in mapping:
        return mapping
    candidates = [key for key in mapping if ignored is None or key not in ignored]
    if not candidates:
        return mapping
    shorthand_name = candidates[0]
    if allowed is not None and shorthand_name in allowed:
        return mapping
    shorthand_description = mapping[shorthand_name]
    if not isinstance(shorthand_name, str) or not shorthand_name.strip():
        raise ConfigurationError(f"{path} shorthand name must be a non-empty string")
    if shorthand_description is not None and not isinstance(shorthand_description, str):
        raise ConfigurationError(
            f"{path} shorthand description must be a string or null"
        )
    if "description" in mapping:
        raise ConfigurationError(
            f"{path} cannot combine shorthand and explicit descriptions"
        )
    normalized = dict(mapping)
    del normalized[shorthand_name]
    normalized["name"] = shorthand_name
    if shorthand_description is not None:
        normalized["description"] = shorthand_description
    return normalized


def _only(mapping: dict[str, Any], allowed: set[str], path: str) -> None:
    reject_unknown_keys(mapping, allowed, path, error=ConfigurationError)


def _name(mapping: dict[str, Any], path: str) -> str:
    return _required_string(mapping, "name", path)


def _required_string(mapping: dict[str, Any], key: str, path: str) -> str:
    return expect_normalized_name(
        mapping.get(key), f"{path}.{key}", error=ConfigurationError
    )


def _nonempty_string(mapping: dict[str, Any], key: str, path: str) -> str:
    return expect_nonempty_string(
        mapping.get(key), f"{path}.{key}", error=ConfigurationError
    )


def _optional_string(mapping: dict[str, Any], key: str, path: str) -> str | None:
    if key not in mapping:
        return None
    return _nonempty_string(mapping, key, path)


def _optional_bool(mapping: dict[str, Any], key: str, path: str) -> bool | None:
    if key not in mapping:
        return None
    value = mapping[key]
    if not isinstance(value, bool):
        raise ConfigurationError(f"{path}.{key} must be true or false")
    return value


def _optional_agent(mapping: dict[str, Any], key: str, path: str) -> str | None:
    value = _optional_string(mapping, key, path)
    if value == "auto":
        raise ConfigurationError(f"{path}.{key} must not be 'auto'")
    return value


def _role(mapping: dict[str, Any], path: str) -> StepRole | None:
    """The declared ``role``, or ``None`` when the step inherits one."""
    if "role" not in mapping:
        return None
    value = mapping["role"]
    if value not in get_args(StepRole):
        raise ConfigurationError(f"{path}.role must be manager or worker")
    return cast(StepRole, value)


def _subagents(mapping: dict[str, Any], path: str) -> bool | None:
    """The declared ``subagents``, or ``None`` when the step inherits it.

    ``false`` means whoever performs the step, manager or worker, spawns no
    subagents for anything; who performs it is ``role``'s business.
    """
    if "subagents" not in mapping:
        return None
    value = mapping["subagents"]
    if not isinstance(value, bool):
        raise ConfigurationError(f"{path}.subagents must be true or false")
    return value


def _optional_name(mapping: dict[str, Any], key: str, path: str) -> str | None:
    if key not in mapping:
        return None
    return _required_string(mapping, key, path)


def _profile(mapping: dict[str, Any], path: str) -> dict[str, str | None]:
    value = mapping.get("profile")
    if value is None:
        return {"profile": None, "profile_description": None}
    if isinstance(value, str):
        if not value.strip() or not _NAME.fullmatch(value):
            raise ConfigurationError(f"{path}.profile must be a normalized name")
        return {"profile": value, "profile_description": None}
    profile = _mapping(value, f"{path}.profile")
    _only(profile, {"name", "description"}, f"{path}.profile")
    name = _optional_name(profile, "name", f"{path}.profile")
    description = _optional_string(profile, "description", f"{path}.profile")
    if name is None and description is None:
        raise ConfigurationError(f"{path}.profile requires name or description")
    return {"profile": name, "profile_description": description}


def _description(value: Any, path: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigurationError(f"{path}.description must be a string")
    return value


def _description_items(value: Any, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ConfigurationError(
            f"{path}.description must be a string or list of strings"
        )
    return tuple(value)


def _required_list(mapping: dict[str, Any], key: str, path: str) -> list[Any]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise ConfigurationError(f"{path}.{key} must be a list")
    return value


def _reference_or_name(value: str) -> bool:
    return bool(_NAME.fullmatch(value)) or is_extension_reference(value)


def _string_list(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and _reference_or_name(item) for item in value
    ):
        raise ConfigurationError(f"{path} must be a list of normalized names")
    return tuple(value)


def _name_filter(value: Any, path: str, *, empty: NameFilter) -> NameFilter:
    """Parse a ``workflows`` or ``steps`` filter: ``"*"`` or a list of names.

    A step name may be a ``/``-separated path. ``empty`` is what ``[]``
    means where the filter is written: every name for a hook, none for a
    rule group.
    """
    if value == ALL_NAMES:
        return ALL
    if not isinstance(value, list):
        raise ConfigurationError(
            f'{path} must be "*" or a list of names'
            + (f" (write [{value}] for one name)" if isinstance(value, str) else "")
        )
    if ALL_NAMES in value:
        raise ConfigurationError(
            f'{path} cannot mix "*" with names; write "*" alone for all'
        )
    if not all(
        isinstance(item, str)
        and item
        and all(_NAME.fullmatch(segment) for segment in item.split("/"))
        for item in value
    ):
        raise ConfigurationError(f'{path} must be "*" or a list of normalized names')
    return NameFilter.of(value) if value else empty


def _unique(values: Any, label: str) -> None:
    items = tuple(values)
    if len(set(items)) != len(items):
        raise ConfigurationError(f"duplicate {label} name")


