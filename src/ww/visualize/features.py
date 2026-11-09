# SPDX-License-Identifier: GPL-3.0-or-later
"""The Feature maps tab: every map, as ``ww features`` reads it.

The tab exists only while the repo ``ww.json`` keeps feature maps. Each map
comes with its pointers grouped by field, those that no longer resolve
marked, how far its files moved on since it last changed, and the tasks that
changed it; the problems ``ww features check`` reports come beside them.
"""

from __future__ import annotations

from typing import Any

from ww.config_snapshots import GIT_EXTENSION
from ww.features import (
    FeatureMap,
    FeatureMaps,
    load_feature_maps,
    pointer_problems,
    read_task_list,
    staleness,
)

from .project import Project

_FIELDS = ("parts", "touches", "tests", "docs")


def feature_maps_view(project: Project) -> dict[str, Any]:
    """What the tab shows, or ``{"enabled": false}`` without feature maps."""
    config = project.extensions.config if project.extensions is not None else None
    if config is None or not config.feature_maps:
        return {"enabled": False}
    commit_format = config.settings_for(GIT_EXTENSION).get("commit_format")
    maps = load_feature_maps(project.storage.root)
    return {
        "enabled": True,
        "maps": [
            _view(
                maps,
                feature,
                commit_format if isinstance(commit_format, str) else None,
            )
            for feature in maps.maps
        ],
        "problems": [problem.to_dict() for problem in maps.check()],
    }


def _view(
    maps: FeatureMaps, feature: FeatureMap, commit_format: str | None
) -> dict[str, Any]:
    root = maps.root
    stale = staleness(root, feature, commit_format)

    def pointers(field: str) -> list[dict[str, Any]]:
        return [
            {"pointer": str(pointer), "broken": list(pointer_problems(pointer, root))}
            for pointer in getattr(feature, field)
        ]

    return {
        "name": feature.name,
        "summary": feature.summary,
        "aliases": list(feature.aliases),
        "entry": (
            {
                "pointer": str(feature.entry),
                "broken": list(pointer_problems(feature.entry, root)),
            }
            if feature.entry is not None
            else None
        ),
        "fields": {field: pointers(field) for field in _FIELDS},
        "reads": list(feature.reads),
        "uses": list(feature.uses),
        "staleness": ({**stale.to_dict(), "text": stale.describe()} if stale else None),
        "tasks": [entry.to_dict() for entry in read_task_list(root, feature.name)],
    }
