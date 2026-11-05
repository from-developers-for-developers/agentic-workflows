# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan constructs ww removed from its runtime, and the runs that still hold one.

A run executes the plan it saved when it started, so a construct ww stops
running, such as an action or an operation it no longer has, can sit in a
run an older ww started. Such a run cannot go on under this ww, but nothing
else has to stop with it: only that run is refused, with the construct
named and how to finish it, and every other run and task carries on.

Each removal is one :class:`RemovedConstruct`, declared in
:data:`REMOVED_CONSTRUCTS` in the release that deprecates the construct,
with ``removed_in`` set from the start. That is what lets the last release
of one major version know every removal the next makes: ``ww upgrade``
judges the open runs with this build's list against the version it is about
to install (:func:`removed_in_plan` with that version), before a newer ww
refuses them.

A construct's detector reads the plan snapshot as the run document stores
it, expanded but not decoded, because decoding the construct is exactly what
a ww without it cannot do. Old shapes therefore stay here and the decoders
never see them. ww has removed no plan construct yet.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from packaging.version import InvalidVersion, Version

from ww import __version__
from ww.contracts import run_is_open
from ww.errors import StateError
from ww.executable import ww_command

# The distribution ``pipx`` installs; an older release is pinned with it.
_PACKAGE = "ww-agentic-workflows"

# Finds a construct in a plan snapshot mapping, returning where it is, such
# as ``plan.items[3] (review)``, or nothing when the plan does not hold it.
Detector = Callable[[Mapping[str, Any]], tuple[str, ...]]


@dataclass(frozen=True)
class RemovedConstruct:
    """One plan construct ww no longer runs.

    ``what`` names it in messages ("the `legacy` action"); ``last_version``
    is the newest ww that still runs it, the one to finish such a run with.
    """

    id: str
    what: str
    deprecated_in: str
    removed_in: str
    last_version: str
    detect: Detector

    def __post_init__(self) -> None:
        if Version(self.removed_in) <= Version(self.last_version):
            raise ValueError(
                f"removal {self.id!r}: last_version must precede removed_in"
            )
        Version(self.deprecated_in)

    def removed(self, version: str | None = None) -> bool:
        """Whether ``version``, by default ww's own, no longer runs it.

        A prerelease of ``removed_in`` counts as it, since the removal lands
        in its first prerelease. A version that is not a version number,
        such as an uninstalled source tree's, counts as before every removal.
        """
        try:
            current = Version(version if version is not None else __version__)
        except InvalidVersion:
            return False
        return Version(current.base_version) >= Version(self.removed_in)


@dataclass(frozen=True)
class RemovedFinding:
    """A removed construct and where one plan snapshot holds it."""

    construct: RemovedConstruct
    locations: tuple[str, ...]

    def render(self) -> str:
        return (
            f"{self.construct.what} ({', '.join(self.locations)}), removed in "
            f"ww {self.construct.removed_in}"
        )


# Every plan construct ww has removed or scheduled for removal, in the order
# releases added them. An entry stays while a supported ww may meet a run
# that holds the construct.
REMOVED_CONSTRUCTS: tuple[RemovedConstruct, ...] = ()


class RemovedConstructError(StateError):
    """A run whose saved plan holds a construct this ww no longer runs."""

    def __init__(
        self,
        task_id: str,
        run_id: str,
        status: object,
        found: tuple[RemovedFinding, ...],
    ) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.open = not isinstance(status, str) or run_is_open(status)
        self.found = found
        last = min((finding.construct.last_version for finding in found), key=Version)
        uses = "; ".join(finding.render() for finding in found)
        older = f"ww {last} (`pipx install --force {_PACKAGE}=={last}`)"
        reset = f"`{ww_command()} reset {task_id}`"
        if self.open:
            remedy = f"finish it with {older}, or discard the task with {reset}"
        else:
            remedy = (
                f"read the task with {older}, or remove it with {reset}; "
                f"`{ww_command()} visualize` still shows it in a reduced view"
            )
        state = "open" if self.open else "finished"
        super().__init__(
            f"task {task_id!r}: its {state} run {run_id!r} uses {uses}, which "
            f"this ww no longer runs: {remedy}. Other tasks are unaffected."
        )


def plan_items(snapshot: Mapping[str, Any]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Every plan item of ``snapshot`` with its path, for a detector.

    The items of the plan and of its template, each with the verifier and
    repair items nested in its verification plan. Anything that is not a
    mapping is skipped: a damaged document is the decoder's to report.
    """
    for key in ("plan", "template_plan"):
        plan = snapshot.get(key)
        if isinstance(plan, Mapping):
            yield from _items(plan.get("items"), f"{key}.items")


def _items(value: object, path: str) -> Iterator[tuple[str, Mapping[str, Any]]]:
    if not isinstance(value, list):
        return
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            continue
        where = f"{path}[{index}]"
        yield where, item
        nested = item.get("verification_plan")
        if isinstance(nested, Mapping):
            for name in ("verifier", "on_failure"):
                yield from _items(nested.get(name), f"{where}.verification_plan.{name}")


def removed_in_plan(
    snapshot: Mapping[str, Any], version: str | None = None
) -> tuple[RemovedFinding, ...]:
    """The constructs ``version`` (by default ww's own) no longer runs that
    ``snapshot``, a plan snapshot mapping, holds."""
    found: list[RemovedFinding] = []
    for construct in REMOVED_CONSTRUCTS:
        if not construct.removed(version):
            continue
        locations = construct.detect(snapshot)
        if locations:
            found.append(RemovedFinding(construct, tuple(dict.fromkeys(locations))))
    return tuple(found)


def refuse_removed(run: Mapping[str, Any], *, task_id: str, run_id: str) -> None:
    """Refuse the run document ``run`` when its plan holds a removed construct.

    ``run`` is the run as its document stores it, expanded but not decoded.
    """
    snapshot = run.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return
    found = removed_in_plan(snapshot)
    if found:
        state = run.get("state")
        status = state.get("status") if isinstance(state, Mapping) else None
        raise RemovedConstructError(task_id, run_id, status, found)
