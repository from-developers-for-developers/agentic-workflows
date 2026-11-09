# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww features``: the index of a project's feature maps, a lookup, one map,
the maps a change touches, the maps behind their code, a check of them all,
and the template a new map starts from."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ww.errors import StateError
from ww.features import (
    FEATURES_DIRECTORY,
    TEMPLATE,
    FeatureMap,
    FeatureMaps,
    Problem,
    Staleness,
    changed_paths,
    load_feature_maps,
    read_task_list,
    staleness,
)

OFF = (
    "Feature maps are off in this project: set `feature_maps` to true in the "
    "repo ww.json to keep them.\n"
)


@dataclass(frozen=True)
class FeaturesRequest:
    """What ``ww features`` was asked, and what it reads to answer.

    ``run_workspace`` gives a task's working directory and the branch its run
    forks from; ``commit_format`` is the ww/git one, naming a commit's task.
    """

    root: Path
    enabled: bool
    action: str | None
    json_output: bool
    run_workspace: Callable[[str], tuple[Path, str | None]]
    name: str | None = None
    query: tuple[str, ...] = ()
    task: str | None = None
    commit_format: str | None = None


def features(request: FeaturesRequest) -> tuple[str, int]:
    """The text ``ww features <action>`` prints, and its exit code."""
    if request.action == "template":
        return TEMPLATE, 0
    if not request.enabled:
        return (_json({"enabled": False}) if request.json_output else OFF), 0
    if request.action == "changed":
        return _changed(request), 0
    maps = load_feature_maps(request.root)
    if request.action == "show":
        assert request.name is not None
        return _show(request, maps, request.name), 0
    if request.action == "lookup":
        return _lookup(request, maps), 0
    if request.action == "stale":
        return _stale(request, maps), 0
    if request.action == "check":
        problems = maps.check()
        return _check(maps, problems, request.json_output), 1 if problems else 0
    return _index(maps, request.json_output), 0


def _index(maps: FeatureMaps, json_output: bool) -> str:
    if json_output:
        return _json(
            {
                "enabled": True,
                "maps": [_brief(feature) for feature in maps.maps],
                "problems": [problem.to_dict() for problem in maps.problems],
            }
        )
    if not maps.maps and not maps.problems:
        return (
            f"No feature maps yet. Add one as {FEATURES_DIRECTORY.as_posix()}/"
            "<name>.yaml; `ww features template` prints a starting point.\n"
        )
    lines = [f"Feature maps ({FEATURES_DIRECTORY.as_posix()}):", ""]
    lines += [_line(feature) for feature in maps.maps]
    if maps.problems:
        lines += [
            "",
            f"{len(maps.problems)} map problem(s); `ww features check` lists "
            "them with the pointers that no longer resolve.",
        ]
    return "\n".join(lines) + "\n"


def _lookup(request: FeaturesRequest, maps: FeatureMaps) -> str:
    """The maps a query finds, each with what no longer holds about it."""
    query = " ".join(request.query)
    found = maps.lookup(query)
    reports = [(feature, *_health(request, maps, feature)) for feature in found]
    if request.json_output:
        return _json(
            {
                "enabled": True,
                "query": query,
                "maps": [
                    {
                        **feature.to_dict(),
                        "broken": [problem.message for problem in broken],
                        "staleness": stale.to_dict() if stale else None,
                    }
                    for feature, broken, stale in reports
                ],
            }
        )
    if not found:
        return (
            f"No feature map matches {query!r}. `ww features` lists them all; "
            "search the code instead.\n"
        )
    lines = [f"Feature maps for {query!r}:", ""]
    for feature, broken, stale in reports:
        lines.append(_line(feature))
        lines += [f"  - no longer resolves: {problem.message}" for problem in broken]
        if stale is not None and (stale.stale or not stale.committed):
            lines.append(f"  - {stale.describe()}")
    lines += [
        "",
        "Read one with `ww features show <name>`. A map lists where code lives "
        "and does not explain it; confirm what you need in the code.",
    ]
    return "\n".join(lines) + "\n"


def _show(request: FeaturesRequest, maps: FeatureMaps, name: str) -> str:
    feature = maps.get(name)
    if feature is None:
        known = ", ".join(found.name for found in maps.maps) or "none"
        raise StateError(f"no feature map {name!r}; maps: {known}")
    broken, stale = _health(request, maps, feature)
    tasks = read_task_list(request.root, feature.name)
    if request.json_output:
        return _json(
            {
                **feature.to_dict(),
                "broken": [problem.message for problem in broken],
                "staleness": stale.to_dict() if stale else None,
                "tasks": [entry.to_dict() for entry in tasks],
            }
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
    if stale is not None:
        lines += ["", f"History: {stale.describe()}."]
    if tasks:
        lines += [
            "",
            "Tasks that changed it:",
            *(f"- {entry.line()}" for entry in tasks),
        ]
    return "\n".join(lines) + "\n"


def _changed(request: FeaturesRequest) -> str:
    """The maps the changes of a task's run, or of the checkout, touch."""
    if request.task is not None:
        directory, base = request.run_workspace(request.task)
        subject = f"{request.task}'s run"
    else:
        directory, base = request.root, None
        subject = "the uncommitted changes"
    paths = changed_paths(directory, base)
    if paths is None:
        raise StateError(f"{directory} is not a Git checkout: no change set to map")
    coverage = load_feature_maps(directory).coverage(paths)
    if request.json_output:
        return _json(
            {
                "enabled": True,
                "directory": str(directory),
                "affected": [feature.name for feature in coverage.affected],
                "unmapped": list(coverage.unmapped),
            }
        )
    if not paths:
        return f"Nothing changed in {subject}.\n"
    lines = [f"Feature maps {subject} touch:", ""]
    lines += [_line(feature) for feature in coverage.affected] or ["- none"]
    if coverage.unmapped:
        lines += [
            "",
            "Changed files no map covers:",
            *(f"- {path}" for path in coverage.unmapped),
        ]
    return "\n".join(lines) + "\n"


def _stale(request: FeaturesRequest, maps: FeatureMaps) -> str:
    """The maps whose files changed since they did."""
    reports = [
        (feature, stale)
        for feature in maps.maps
        if (stale := staleness(request.root, feature, request.commit_format))
        is not None
        and (stale.stale or not stale.committed)
    ]
    if request.json_output:
        return _json(
            {
                "enabled": True,
                "maps": [
                    {"name": feature.name, **stale.to_dict()}
                    for feature, stale in reports
                ],
            }
        )
    if not reports:
        return f"{len(maps.maps)} feature map(s), none behind its files.\n"
    return "Feature maps behind their files, or not committed yet:\n\n" + "".join(
        f"- `{feature.name}`: {stale.describe()}\n" for feature, stale in reports
    )


def _health(
    request: FeaturesRequest, maps: FeatureMaps, feature: FeatureMap
) -> tuple[tuple[Problem, ...], Staleness | None]:
    return (
        maps.pointer_problems(feature),
        staleness(request.root, feature, request.commit_format),
    )


def _brief(feature: FeatureMap) -> dict[str, object]:
    return {
        "name": feature.name,
        "summary": feature.summary,
        "aliases": list(feature.aliases),
    }


def _line(feature: FeatureMap) -> str:
    aliases = f" (also: {', '.join(feature.aliases)})" if feature.aliases else ""
    return f"- `{feature.name}` — {feature.summary}{aliases}"


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
