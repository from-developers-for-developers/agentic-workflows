# SPDX-License-Identifier: GPL-3.0-or-later
"""What a step that writes the feature maps must answer for.

Everything follows from the files the run changed: the maps those changes
touch, the changed files no map covers, the pointers of the touched maps
that no longer resolve, and the gaps the run's reading steps reported. The
step answers for each touched map and each uncovered file in its artifact;
ww checks that each has an answer, and that every pointer resolves, not
whether an answer is right.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .maps import FeatureMap, Problem, load_feature_maps


@dataclass(frozen=True)
class MapsDue:
    """The maps and files a writing step answers for, read in ``directory``."""

    directory: Path
    affected: tuple[FeatureMap, ...] = ()
    unmapped: tuple[str, ...] = ()
    broken: tuple[Problem, ...] = ()
    gaps: tuple[str, ...] = ()

    @property
    def subjects(self) -> tuple[str, ...]:
        """What the artifact must answer for: each map's name, each file."""
        return (*(feature.name for feature in self.affected), *self.unmapped)

    @property
    def empty(self) -> bool:
        return not (self.affected or self.unmapped or self.broken)

    def unanswered(self, answers: Mapping[str, str] | None) -> tuple[str, ...]:
        """The subjects ``answers`` leaves without an answer."""
        given = answers or {}
        return tuple(subject for subject in self.subjects if subject not in given)


def maps_due(
    directory: Path, changed: Iterable[str], gaps: Iterable[str] = ()
) -> MapsDue:
    """What a writing step answers for, given the run's ``changed`` files and
    the ``gaps`` its reading steps reported, with the maps in ``directory``."""
    maps = load_feature_maps(directory)
    coverage = maps.coverage(changed)
    return MapsDue(
        directory,
        coverage.affected,
        coverage.unmapped,
        tuple(
            problem
            for feature in coverage.affected
            for problem in maps.pointer_problems(feature)
        ),
        tuple(gaps),
    )
