# SPDX-License-Identifier: GPL-3.0-or-later
"""The numbered examples of documentation/examples.md, as code blocks."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

EXAMPLES = Path(__file__).parents[1] / "documentation/examples.md"
_BLOCK = re.compile(
    r"^## (\d+)\. .*?$|^```(yaml|json|markdown)\n(.*?)^```$", re.S | re.M
)
# A block whose first line is ``# file: <path>`` is one file of a layered
# setup, such as the global, project and local files of example 6.
FILE_MARKER = re.compile(r"# file: (\S+)\n")


def examples() -> list[tuple[str, list[tuple[str, str]]]]:
    """Every example's number and its (kind, text) blocks, in document order."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    for match in _BLOCK.finditer(EXAMPLES.read_text(encoding="utf-8")):
        if match.group(1):
            sections.append((match.group(1), []))
        elif sections:
            sections[-1][1].append((match.group(2), match.group(3)))
    return sections


def example_yaml(number: int) -> str:
    """The one YAML block of a single-file example."""
    blocks = dict(examples())[str(number)]
    (text,) = [text for kind, text in blocks if kind == "yaml"]
    return text


def example_configuration(number: int) -> dict[str, object]:
    loaded = yaml.safe_load(example_yaml(number))
    assert isinstance(loaded, dict)
    return loaded
