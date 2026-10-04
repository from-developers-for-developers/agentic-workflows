# SPDX-License-Identifier: GPL-3.0-or-later
"""The three design documents, readable from a checkout or an installation.

The canonical files live in ``documentation/``. A built distribution carries
byte-for-byte copies under ``ww/assets/docs`` (added by the build, never
committed), so ``ww docs`` shows the documents of the installed version.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from .errors import StateError

DESIGN_DOCUMENTS = ("specification", "features", "examples")
PACKAGED_DIRECTORY = "docs"
_CHECKOUT = Path(__file__).resolve().parents[2] / "documentation"


def read_design_document(name: str) -> str:
    """Return the same-version text of one design document."""
    if name not in DESIGN_DOCUMENTS:
        known = ", ".join(DESIGN_DOCUMENTS)
        raise StateError(f"unknown document {name!r}; choose one of: {known}")
    packaged = files("ww.assets").joinpath(PACKAGED_DIRECTORY).joinpath(f"{name}.md")
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    checkout = _CHECKOUT / f"{name}.md"
    if checkout.is_file():
        return checkout.read_text(encoding="utf-8")
    raise StateError(f"the {name} document is not available in this installation")
