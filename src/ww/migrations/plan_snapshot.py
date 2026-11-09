# SPDX-License-Identifier: GPL-3.0-or-later
"""Migrations of the plan snapshot nested in each run document."""

from __future__ import annotations

from typing import Any


def artifact_dependency_lists(snapshot: dict[str, Any]) -> dict[str, Any]:
    """1 -> 2: a plan item's ``artifact_dependency`` became the list
    ``artifact_dependencies``, since ``artifact_from`` may name several steps.

    Plan items sit in ``plan`` and ``template_plan``, and inside them in each
    item's verification plan, so every item mapping found below the snapshot
    is rewritten; ``None`` becomes an empty list.
    """
    upgraded: dict[str, Any] = _rewritten(snapshot)
    return upgraded


def _rewritten(value: Any) -> Any:
    if isinstance(value, list):
        return [_rewritten(entry) for entry in value]
    if not isinstance(value, dict):
        return value
    upgraded = {
        key: _rewritten(entry)
        for key, entry in value.items()
        if key != "artifact_dependency"
    }
    if "artifact_dependency" in value:
        dependency = value["artifact_dependency"]
        upgraded["artifact_dependencies"] = [] if dependency is None else [dependency]
    return upgraded
