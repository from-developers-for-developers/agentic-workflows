# SPDX-License-Identifier: GPL-3.0-or-later
"""Feature maps: where each of a project's features lives, one line per place.

A feature map is a map, not documentation: ``.ww/features/<name>.yaml`` points
from one feature to the files, names, tests and document sections it spreads
over, so an agent finds them without searching. The reasons behind the code
stay in the project's documentation.

ww never parses a programming language. It checks a map's shape and that
each pointer still resolves: a path exists, a glob matches, a ``path#Name``
appears in the file as a whole word, and a ``path#Heading`` on a Markdown
file names one of its headings. Everything here is inert unless the repo
``ww.json`` sets ``feature_maps`` to true.
"""

from __future__ import annotations

from ww.config_files import FEATURES_DIRECTORY

from .due import MapsDue, maps_due
from .history import Staleness, changed_paths, staleness
from .maps import (
    MAX_ENTRIES,
    TEMPLATE,
    Coverage,
    FeatureMap,
    FeatureMaps,
    Problem,
    load_feature_maps,
)
from .pointers import Pointer, pointer_problems
from .reports import (
    FEATURE_MAPS_HEADING,
    MapsReport,
    read_maps_answers,
    read_maps_report,
)
from .task_lists import TaskEntry, read_task_list, record_task, task_list_path

__all__ = [
    "FEATURE_MAPS_HEADING",
    "FEATURES_DIRECTORY",
    "MAX_ENTRIES",
    "TEMPLATE",
    "Coverage",
    "FeatureMap",
    "FeatureMaps",
    "MapsDue",
    "MapsReport",
    "Pointer",
    "Problem",
    "Staleness",
    "TaskEntry",
    "changed_paths",
    "load_feature_maps",
    "maps_due",
    "pointer_problems",
    "read_maps_answers",
    "read_maps_report",
    "read_task_list",
    "record_task",
    "staleness",
    "task_list_path",
]
