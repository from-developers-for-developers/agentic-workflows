# SPDX-License-Identifier: GPL-3.0-or-later
"""Token usage of the agents that work on ww's tasks.

ww never sees a model's tokens itself: it reads the agents' own session
logs (see :mod:`ww.agents.usage`) and, for agents without such logs, what
their hooks report.  :mod:`.collector` places the tokens on tasks, steps
and roles, :mod:`.store` keeps them, and :mod:`.report` sums them up.
"""

from .collector import Collection, Collector, Invocation, project_collector
from .report import (
    TaskUsage,
    UnreadableSession,
    UsageOverview,
    UsageRow,
    format_tokens,
    render_overview,
    render_task_usage,
    task_total,
    task_usage,
    usage_overview,
)
from .runs import map_counts, note_run
from .stats import usage_stats
from .store import HOOK, TRANSCRIPT, FileState, RunFacts, Stretch, UsageStore

__all__ = [
    "HOOK",
    "TRANSCRIPT",
    "Collection",
    "Collector",
    "FileState",
    "Invocation",
    "RunFacts",
    "Stretch",
    "TaskUsage",
    "UnreadableSession",
    "UsageOverview",
    "UsageRow",
    "UsageStore",
    "format_tokens",
    "map_counts",
    "note_run",
    "project_collector",
    "render_overview",
    "render_task_usage",
    "task_total",
    "task_usage",
    "usage_overview",
    "usage_stats",
]
