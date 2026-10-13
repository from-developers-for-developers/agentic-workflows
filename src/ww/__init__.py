# SPDX-License-Identifier: GPL-3.0-or-later
"""ww: local-first agentic workflows."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ww-agentic-workflows")
except PackageNotFoundError:
    # Only a source tree that was never installed; releases are numbered by
    # CI from PyPI (scripts/release_version.py), never here.
    __version__ = "0+unknown"
# Shown by ``--version`` and ``init``: syntax, state, and commands may still
# change between pulls, as documentation/limitations.md describes.
STAGE = "beta"
BETA_NOTICE = (
    "ww is in beta: ww.yaml syntax, task state, and "
    "commands may still change. What is not promised yet: "
    "https://github.com/from-developers-for-developers/agentic-workflows/blob/"
    "main/documentation/limitations.md"
)
