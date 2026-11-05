# SPDX-License-Identifier: GPL-3.0-or-later
"""The design documents, readable from a checkout or an installation.

The canonical files live in ``documentation/``. A built distribution carries
byte-for-byte copies under ``ww/assets/docs`` (added by the build, never
committed), so ``ww docs`` shows the documents of the installed version.
The upgrade guide is generated from ww's configuration migrations instead
(:mod:`ww.migrations.config.guide`); ``documentation/deprecations.md`` holds
the same text.

``<document>#<section>`` reads one section: the one under an explicit
``<a id="<section>"></a>`` anchor, or whose heading has that GitHub-style
slug, up to the next heading of the same or a higher level.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from .errors import StateError
from .migrations.config import render_upgrade_guide

DESIGN_DOCUMENTS = ("specification", "features", "examples", "deprecations")
# Documents ww renders from its own declarations rather than reads.
GENERATED_DOCUMENTS: dict[str, Callable[[], str]] = {"upgrade": render_upgrade_guide}
DOCUMENTS = (*DESIGN_DOCUMENTS, *GENERATED_DOCUMENTS)
PACKAGED_DIRECTORY = "docs"
_CHECKOUT = Path(__file__).resolve().parents[2] / "documentation"
_HEADING = re.compile(r"(#{1,6})\s+(.*\S)\s*$")
_ANCHOR = re.compile(r'<a id="([^"]+)"></a>')


def read_design_document(name: str) -> str:
    """Return the same-version text of one document, or of one of its sections."""
    name, _, section = name.partition("#")
    if name not in DOCUMENTS:
        known = ", ".join(DOCUMENTS)
        raise StateError(f"unknown document {name!r}; choose one of: {known}")
    text = _document(name)
    return _section(text, name, section) if section else text


def _document(name: str) -> str:
    generated = GENERATED_DOCUMENTS.get(name)
    if generated is not None:
        return generated()
    packaged = files("ww.assets").joinpath(PACKAGED_DIRECTORY).joinpath(f"{name}.md")
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    checkout = _CHECKOUT / f"{name}.md"
    if checkout.is_file():
        return checkout.read_text(encoding="utf-8")
    raise StateError(f"the {name} document is not available in this installation")


def _section(text: str, name: str, section: str) -> str:
    """The part of ``text`` under ``section``, through to the next heading."""
    lines = text.splitlines(keepends=True)
    headings = _headings(lines)
    start: int | None = None
    for index, line in enumerate(lines):
        anchor = _ANCHOR.fullmatch(line.strip())
        if anchor is not None and anchor[1] == section:
            start = next((at for at in headings if at > index), None)
            break
        if index in headings and _slug(headings[index][1]) == section:
            start = index
            break
    if start is None:
        raise StateError(f"the {name} document has no section {section!r}")
    level = headings[start][0]
    end = next(
        (at for at in headings if at > start and headings[at][0] <= level),
        len(lines),
    )
    body = lines[start:end]
    # The next section's anchor sits just above its heading.
    while body and (not body[-1].strip() or _ANCHOR.fullmatch(body[-1].strip())):
        body.pop()
    return "".join(body)


def _headings(lines: list[str]) -> dict[int, tuple[int, str]]:
    """Each heading's line index, level and text, outside fenced code."""
    found: dict[int, tuple[int, str]] = {}
    fenced = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            fenced = not fenced
            continue
        heading = None if fenced else _HEADING.match(line)
        if heading is not None:
            found[index] = (len(heading[1]), heading[2])
    return found


def _slug(heading: str) -> str:
    """A heading's anchor as GitHub derives it."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
