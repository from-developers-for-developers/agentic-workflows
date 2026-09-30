# SPDX-License-Identifier: GPL-3.0-or-later
"""Every page of the catalog matches its golden copy, word for word.

A wording change to a page is then a visible diff under
``tests/golden/pages``. After an intended change, refresh the copies with
``PYTHONPATH=src:. python scripts/measure_pages.py --write-golden`` and review
the diff.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.page_catalog import GOLDEN_DIRECTORY, SCENARIOS, render_page


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_the_page_matches_its_golden_copy(tmp_path: Path, name: str) -> None:
    golden = GOLDEN_DIRECTORY / f"{name}.md"

    page = render_page(name, tmp_path / name)

    assert golden.exists(), f"no golden copy for {name}; run --write-golden"
    assert page == golden.read_text(encoding="utf-8")


def test_every_golden_copy_belongs_to_a_page() -> None:
    names = {path.stem for path in GOLDEN_DIRECTORY.glob("*.md")}

    assert names == set(SCENARIOS)
