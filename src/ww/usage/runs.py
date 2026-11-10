# SPDX-License-Identifier: GPL-3.0-or-later
"""What was true when each task run started, for comparing runs' tokens.

The first ww command that sees a run (``start``, ``next``, ``start-child``)
records its workflow, start time and ww version, and how many feature maps
the project had then and how many of its files they cover. Usage records
carry the version of each command; this adds what the records cannot know.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ww import __version__
from ww.changes import project_files
from ww.features.maps import FEATURES_DIRECTORY, load_feature_maps
from ww.storage import Storage

from .store import RunFacts, UsageStore


def map_counts(root: Path) -> tuple[int, int]:
    """How many feature maps ``root`` has, and how many of its files they cover."""
    if not (root / FEATURES_DIRECTORY).is_dir():
        return 0, 0
    maps = load_feature_maps(root)
    if not maps.maps:
        return 0, 0
    files = [path for path in project_files(root) if not path.startswith(".ww/")]
    return len(maps.maps), len(files) - len(maps.coverage(files).unmapped)


def note_run(
    storage: Storage,
    task: str,
    run: str,
    workflow: str,
    started: str,
    *,
    counts: Callable[[Path], tuple[int, int]] = map_counts,
) -> bool:
    """Record the run's facts the first time ww sees it; whether it did."""
    store = UsageStore(storage)
    if (task, run) in store.run_facts():
        return False
    maps, files = counts(storage.root)
    return store.note_run(
        RunFacts(task, run, workflow, started, __version__, maps, files)
    )
