# SPDX-License-Identifier: GPL-3.0-or-later
"""Actionable notices for configuration syntax still accepted temporarily."""

from __future__ import annotations

from typing import Any


def assessment_deprecations(raw: dict[str, Any], source: str) -> tuple[str, ...]:
    notices: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            assessment = value.get("assess")
            if isinstance(assessment, dict) and "question" in assessment:
                notices.append(
                    f"Deprecated: {source}:{path}.assess.question; rename question "
                    "to assess (assess: {assess: ...}). See "
                    "documentation/deprecations.md."
                )
            if value.get("name") == "assess" and "question" in value:
                notices.append(
                    f"Deprecated: {source}:{path}.question; use assess instead. "
                    "See documentation/deprecations.md."
                )
            for key, nested in value.items():
                walk(nested, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                walk(nested, f"{path}[{index}]")

    walk(raw, "")
    return tuple(notices)
