# SPDX-License-Identifier: GPL-3.0-or-later
"""Token usage summed for people: per task by step, role and model."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ww.agents import TokenCounts
from ww.project_config import UsagePrice

from .store import Stretch, UsageStore

_COLUMNS = ("input", "cache_write", "cache_read", "output", "total")
_HEADINGS = ("input", "cache write", "cache read", "output", "total")
_KILO = 1_000
_MEGA = 1_000_000


@dataclass(frozen=True)
class UsageRow:
    """Tokens summed over one group of stretches."""

    labels: dict[str, str | None]
    counts: TokenCounts
    cost: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.labels,
            **self.counts.to_dict(),
            "total": self.counts.total,
            "cost": None if self.cost is None else round(self.cost, 4),
        }


@dataclass(frozen=True)
class UnreadableSession:
    session: str
    host: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"session": self.session, "host": self.host}


@dataclass(frozen=True)
class TaskUsage:
    """One task's usage, and the sessions ww could not read here."""

    task_id: str
    rows: tuple[UsageRow, ...]
    total: UsageRow
    unreadable: tuple[UnreadableSession, ...]
    priced: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_id,
            "rows": [row.to_dict() for row in self.rows],
            "total": self.total.to_dict(),
            "unreadable": [session.to_dict() for session in self.unreadable],
        }


@dataclass(frozen=True)
class UsageOverview:
    """Every task's total, the tokens outside tasks and those still waiting."""

    tasks: tuple[UsageRow, ...]
    outside: UsageRow
    waiting: TokenCounts
    priced: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": [row.to_dict() for row in self.tasks],
            "outside": self.outside.to_dict(),
            "waiting": {**self.waiting.to_dict(), "total": self.waiting.total},
        }


def task_usage(
    store: UsageStore,
    task_id: str,
    prices: Mapping[str, UsagePrice],
    sessions: Mapping[str, str | None],
) -> TaskUsage:
    """``task_id``'s usage by run, step, role and model.

    ``sessions`` are the sessions that ran ww commands for the task, with
    the host they ran on; those whose logs were not found are listed.
    """
    stretches = store.task_records(task_id)
    rows = _grouped(stretches, ("run", "step", "role", "model"), prices)
    state = store.load()
    unreadable = tuple(
        UnreadableSession(session, host)
        for session, host in sorted(sessions.items())
        if (known := state.sessions.get(session)) is not None
        and known.readable is False
    )
    return TaskUsage(
        task_id,
        rows,
        _row({}, stretches, prices),
        unreadable,
        bool(prices),
    )


def usage_overview(
    store: UsageStore, task_ids: Iterable[str], prices: Mapping[str, UsagePrice]
) -> UsageOverview:
    rows = []
    for task_id in task_ids:
        stretches = store.task_records(task_id)
        if stretches:
            rows.append(_row({"task": task_id}, stretches, prices))
    waiting = TokenCounts()
    for file in store.load().files.values():
        for figures in file.pending.values():
            waiting = waiting + TokenCounts.from_dict(figures)
    return UsageOverview(
        tuple(rows),
        _row({"task": None}, store.outside_records(), prices),
        waiting,
        bool(prices),
    )


def task_total(store: UsageStore, task_id: str) -> int:
    """All tokens recorded for ``task_id``."""
    return sum(stretch.counts.total for stretch in store.task_records(task_id))


def render_task_usage(usage: TaskUsage) -> str:
    lines = [f"# {usage.task_id} token usage", ""]
    if not usage.rows:
        lines.append("No token usage recorded for this task yet.")
    else:
        lines.extend(
            _table(
                ("run", "step", "role", "model"),
                [*usage.rows, _labelled(usage.total, "total")],
                usage.priced,
            )
        )
    if usage.unreadable:
        lines.extend(["", "Sessions whose logs are not on this machine:"])
        lines.extend(
            f"- {session.session}"
            + (f" (ran on {session.host})" if session.host else "")
            for session in usage.unreadable
        )
    return "\n".join(lines) + "\n"


def render_overview(overview: UsageOverview) -> str:
    lines = ["# Token usage", ""]
    rows = [
        *overview.tasks,
        _labelled(overview.outside, "outside any task"),
    ]
    lines.extend(_table(("task",), rows, overview.priced))
    if overview.waiting.total:
        lines.extend(
            [
                "",
                f"{_number(overview.waiting.total)} tokens wait for their "
                "session's next ww command to place them.",
            ]
        )
    return "\n".join(lines) + "\n"


def format_tokens(total: int) -> str:
    """A compact token count for a column: 950, 12.3k, 4.1M."""
    if total < _KILO:
        return str(total)
    if total < _MEGA:
        return f"{total / _KILO:.1f}k"
    return f"{total / _MEGA:.1f}M"


def _grouped(
    stretches: list[Stretch],
    keys: tuple[str, ...],
    prices: Mapping[str, UsagePrice],
) -> tuple[UsageRow, ...]:
    groups: dict[tuple[str | None, ...], list[Stretch]] = {}
    for stretch in sorted(stretches, key=lambda stretch: stretch.first):
        groups.setdefault(tuple(getattr(stretch, key) for key in keys), []).append(
            stretch
        )
    return tuple(
        _row(dict(zip(keys, labels, strict=True)), members, prices)
        for labels, members in groups.items()
    )


def _row(
    labels: dict[str, str | None],
    stretches: list[Stretch],
    prices: Mapping[str, UsagePrice],
) -> UsageRow:
    counts = TokenCounts()
    cost: float | None = 0.0 if prices else None
    for stretch in stretches:
        counts = counts + stretch.counts
        price = prices.get(stretch.model or "")
        if cost is not None:
            cost = None if price is None else cost + _cost(stretch.counts, price)
    return UsageRow(labels, counts, cost)


def _cost(counts: TokenCounts, price: UsagePrice) -> float:
    return (
        counts.input * price.input
        + counts.cache_write * price.cache_write
        + counts.cache_read * price.cache_read
        + counts.output * price.output
    ) / 1_000_000


def _labelled(row: UsageRow, label: str) -> UsageRow:
    return UsageRow({"_label": label}, row.counts, row.cost)


def _table(keys: tuple[str, ...], rows: list[UsageRow], priced: bool) -> list[str]:
    headings = [*keys, *_HEADINGS, *(("cost",) if priced else ())]
    body = []
    for row in rows:
        label = row.labels.get("_label")
        names = (
            [label or "", *([""] * (len(keys) - 1))]
            if "_label" in row.labels
            else [row.labels.get(key) or "—" for key in keys]
        )
        figures = [
            _number(
                row.counts.total if column == "total" else getattr(row.counts, column)
            )
            for column in _COLUMNS
        ]
        cost = [("—" if row.cost is None else f"${row.cost:,.2f}")] if priced else []
        body.append([*names, *figures, *cost])
    widths = [
        max(len(heading), *(len(line[index]) for line in body))
        for index, heading in enumerate(headings)
    ]
    labels = len(keys)

    def line(cells: list[str]) -> str:
        return "  ".join(
            cell.ljust(width) if index < labels else cell.rjust(width)
            for index, (cell, width) in enumerate(zip(cells, widths, strict=True))
        ).rstrip()

    return [line(headings), *(line(cells) for cells in body)]


def _number(value: int) -> str:
    return f"{value:,}"
