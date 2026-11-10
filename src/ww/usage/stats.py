# SPDX-License-Identifier: GPL-3.0-or-later
"""Token usage over time, per run and per step, for the visualize page.

Each task run becomes one point: its tokens, split by model and by the ww
version that placed them, dated by when the run started, with the feature
maps the project had then. Each step of a run becomes one point the same
way. The page charts and sums them; nothing here reads a log.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ww.agents import TokenCounts

from .store import RunFacts, Stretch, UsageStore

UNKNOWN = "unknown"
# A run's ID names its workflow after its number: ``01-task``.
_RUN_ID = re.compile(r"^\d+-(?P<workflow>.+)$")


@dataclass
class _Point:
    first: str = ""
    counts: TokenCounts = field(default_factory=TokenCounts)
    models: dict[str, TokenCounts] = field(default_factory=dict)
    versions: dict[str, TokenCounts] = field(default_factory=dict)

    def add(self, stretch: Stretch) -> None:
        self.first = min(self.first, stretch.first) if self.first else stretch.first
        self.counts = self.counts + stretch.counts
        model = stretch.model or UNKNOWN
        self.models[model] = self.models.get(model, TokenCounts()) + stretch.counts
        version = stretch.ww_version or UNKNOWN
        self.versions[version] = (
            self.versions.get(version, TokenCounts()) + stretch.counts
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.counts.to_dict(),
            "models": {name: counts.to_dict() for name, counts in self.models.items()},
            "versions": {
                name: counts.to_dict() for name, counts in self.versions.items()
            },
            # The version that placed most of the tokens.
            "ww_version": max(self.versions, key=lambda name: self.versions[name].total)
            if self.versions
            else UNKNOWN,
        }


def workflow_of(run: str, facts: RunFacts | None) -> str:
    """The run's workflow: recorded, else read from its ID."""
    if facts is not None:
        return facts.workflow
    match = _RUN_ID.match(run)
    return match.group("workflow") if match else run


def usage_stats(store: UsageStore) -> dict[str, Any]:
    """Every task run's and step's tokens, with the versions and models seen."""
    facts = store.run_facts()
    runs: dict[tuple[str, str], _Point] = defaultdict(_Point)
    steps: dict[tuple[str, str, str], _Point] = defaultdict(_Point)
    for path in store.usage_files():
        if path == store.outside_path:
            continue
        for stretch in store.records(path):
            if not stretch.task or not stretch.run or not stretch.counts.total:
                continue
            runs[(stretch.task, stretch.run)].add(stretch)
            if stretch.step:
                steps[(stretch.task, stretch.run, stretch.step)].add(stretch)
    run_rows = []
    for (task, run), point in runs.items():
        known = facts.get((task, run))
        run_rows.append(
            {
                "task": task,
                "run": run,
                "workflow": workflow_of(run, known),
                "date": known.started if known else point.first,
                "feature_maps": known.feature_maps if known else None,
                "mapped_files": known.mapped_files if known else None,
                **point.to_dict(),
            }
        )
    step_rows = [
        {
            "task": task,
            "run": run,
            "workflow": workflow_of(run, facts.get((task, run))),
            "step": step,
            "date": point.first,
            **point.to_dict(),
        }
        for (task, run, step), point in steps.items()
    ]
    first_seen: dict[str, str] = {}
    for row in run_rows:
        for version in row["versions"]:
            if version != UNKNOWN and (
                version not in first_seen or row["date"] < first_seen[version]
            ):
                first_seen[version] = row["date"]
    return {
        "runs": sorted(run_rows, key=lambda row: (row["date"], row["task"])),
        "steps": sorted(step_rows, key=lambda row: (row["date"], row["task"])),
        "versions": [
            {"version": version, "first": date}
            for version, date in sorted(first_seen.items(), key=lambda item: item[1])
        ],
        "models": sorted({model for row in run_rows for model in row["models"]}),
    }
