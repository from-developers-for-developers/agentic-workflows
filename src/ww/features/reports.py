# SPDX-License-Identifier: GPL-3.0-or-later
"""The ``## Feature maps`` section a step that reads the maps ends its
artifact with: the maps it used, and their gaps.

``Used:`` lists map names, comma-separated. ``Gaps:`` says what a map got
wrong or left out, on its line or the lines below it. ``none`` means none.
The artifact stays the one record; ww reads the section from it when needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FEATURE_MAPS_HEADING = "## Feature maps"
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FIELD = re.compile(r"^(used|gaps)\s*:\s*(.*)$", re.IGNORECASE)
_NONE = frozenset({"", "none", "-", "n/a"})


@dataclass(frozen=True)
class MapsReport:
    """What a step reported about the feature maps it read."""

    used: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {"used": list(self.used), "gaps": list(self.gaps)}


def read_maps_report(artifact: str) -> MapsReport | None:
    """The artifact's Feature maps section, or ``None`` when it has none."""
    lines = _section(artifact)
    if lines is None:
        return None
    fields: dict[str, list[str]] = {"used": [], "gaps": []}
    current: str | None = None
    for raw in lines:
        line = raw.strip()
        found = _FIELD.match(line)
        if found:
            current = found.group(1).lower()
            line = found.group(2).strip()
        if current is None or line.lower() in _NONE:
            continue
        fields[current].append(re.sub(r"^[-*]\s+", "", line))
    used = tuple(
        dict.fromkeys(
            name.strip().strip("`")
            for entry in fields["used"]
            for name in entry.split(",")
            if name.strip().strip("`").lower() not in _NONE
        )
    )
    return MapsReport(used, tuple(fields["gaps"]))


def _section(artifact: str) -> list[str] | None:
    """The lines under the ``Feature maps`` heading, to the next heading of
    the same level or above."""
    lines = artifact.splitlines()
    for index, line in enumerate(lines):
        heading = _HEADING.match(line)
        if heading and heading.group(2).lower() == "feature maps":
            level = len(heading.group(1))
            body: list[str] = []
            for following in lines[index + 1 :]:
                other = _HEADING.match(following)
                if other and len(other.group(1)) <= level:
                    break
                body.append(following)
            return body
    return None
