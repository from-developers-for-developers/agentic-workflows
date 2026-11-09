# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww features``: the index of a project's feature maps, one map, a check
of them all, and the template a new map starts from."""

from __future__ import annotations

import json
from pathlib import Path

from ww.errors import StateError
from ww.features import (
    FEATURES_DIRECTORY,
    TEMPLATE,
    FeatureMap,
    FeatureMaps,
    Problem,
    load_feature_maps,
)

OFF = (
    "Feature maps are off in this project: set `feature_maps` to true in the "
    "repo ww.json to keep them.\n"
)


def features(
    root: Path, enabled: bool, action: str | None, name: str | None, json_output: bool
) -> tuple[str, int]:
    """The text ``ww features <action>`` prints, and its exit code."""
    if action == "template":
        return TEMPLATE, 0
    if not enabled:
        return (_json({"enabled": False}) if json_output else OFF), 0
    maps = load_feature_maps(root)
    if action == "show":
        assert name is not None
        return _show(maps, name, json_output), 0
    if action == "check":
        problems = maps.check()
        return _check(maps, problems, json_output), 1 if problems else 0
    return _index(maps, json_output), 0


def _index(maps: FeatureMaps, json_output: bool) -> str:
    if json_output:
        return _json(
            {
                "enabled": True,
                "maps": [
                    {
                        "name": feature.name,
                        "summary": feature.summary,
                        "aliases": list(feature.aliases),
                    }
                    for feature in maps.maps
                ],
                "problems": [problem.to_dict() for problem in maps.problems],
            }
        )
    if not maps.maps and not maps.problems:
        return (
            f"No feature maps yet. Add one as {FEATURES_DIRECTORY.as_posix()}/"
            "<name>.yaml; `ww features template` prints a starting point.\n"
        )
    lines = [f"Feature maps ({FEATURES_DIRECTORY.as_posix()}):", ""]
    for feature in maps.maps:
        aliases = f" (also: {', '.join(feature.aliases)})" if feature.aliases else ""
        lines.append(f"- `{feature.name}` — {feature.summary}{aliases}")
    if maps.problems:
        lines += [
            "",
            f"{len(maps.problems)} map problem(s); `ww features check` lists "
            "them with the pointers that no longer resolve.",
        ]
    return "\n".join(lines) + "\n"


def _show(maps: FeatureMaps, name: str, json_output: bool) -> str:
    feature = maps.get(name)
    if feature is None:
        known = ", ".join(found.name for found in maps.maps) or "none"
        raise StateError(f"no feature map {name!r}; maps: {known}")
    broken = maps.pointer_problems(feature)
    if json_output:
        return _json(
            {**feature.to_dict(), "broken": [problem.message for problem in broken]}
        )
    lines = [f"# {feature.name}", "", feature.summary]
    if feature.aliases:
        lines += ["", "Also: " + ", ".join(feature.aliases)]
    if feature.entry is not None:
        lines += ["", f"Entry: `{feature.entry}`"]
    for title, values in _sections(feature):
        if values:
            lines += ["", f"{title}:", *(f"- `{value}`" for value in values)]
    if broken:
        lines += [
            "",
            "No longer resolving; confirm these in the code:",
            *(f"- {problem.message}" for problem in broken),
        ]
    return "\n".join(lines) + "\n"


def _sections(feature: FeatureMap) -> tuple[tuple[str, tuple[object, ...]], ...]:
    return (
        ("Parts", feature.parts),
        ("Touches", feature.touches),
        ("Reads", feature.reads),
        ("Uses", feature.uses),
        ("Tests", feature.tests),
        ("Docs", feature.docs),
    )


def _check(maps: FeatureMaps, problems: tuple[Problem, ...], json_output: bool) -> str:
    if json_output:
        return _json(
            {
                "enabled": True,
                "maps": len(maps.maps),
                "problems": [problem.to_dict() for problem in problems],
            }
        )
    if not problems:
        return f"{len(maps.maps)} feature map(s), every pointer resolves.\n"
    return (
        f"{len(problems)} problem(s) in {len(maps.maps)} feature map(s):\n"
        + "".join(f"- {problem}\n" for problem in problems)
    )


def _json(value: object) -> str:
    return json.dumps(value, indent=2) + "\n"
