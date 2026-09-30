#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Print the size of ww's instruction pages, one line per page.

Renders the fixed page set of ``tests/page_catalog.py`` (the same pages the
golden page tests snapshot) in a temporary directory and prints each page's
characters and an approximate token count (about four characters per token).
Pass ``--json`` for machine-readable output, ``--baseline <file>`` to compare
with an earlier ``--json`` result, and ``--write-golden`` to refresh the
golden pages under ``tests/golden/pages`` after an intended wording change.

Run it from the repository root with ww importable, for example::

    PYTHONPATH=src:. python scripts/measure_pages.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.page_catalog import (  # noqa: E402
    GOLDEN_DIRECTORY,
    measurements,
    render_pages,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument(
        "--baseline", type=Path, help="an earlier --json result to compare with"
    )
    parser.add_argument(
        "--write-golden", action="store_true", help="refresh the golden pages"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        _isolate(base)
        pages = render_pages(base / "pages")
    if args.write_golden:
        GOLDEN_DIRECTORY.mkdir(parents=True, exist_ok=True)
        for name, page in pages.items():
            (GOLDEN_DIRECTORY / f"{name}.md").write_text(page, encoding="utf-8")
    sizes = measurements(pages)
    if args.json:
        print(json.dumps(sizes, indent=2))
        return 0
    before = (
        json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else {}
    )
    print(_table(sizes, before))
    return 0


def _isolate(base: Path) -> None:
    """Keep the operator's Git, update, and user-level settings out of the pages."""
    git_config = base / "gitconfig"
    git_config.write_text("[commit]\n\tgpgsign = false\n", encoding="utf-8")
    (base / "state").mkdir()
    (base / "user").mkdir()
    os.environ.pop("WW_MACHINE_CONFIG_DIR", None)
    os.environ.update(
        {
            "GIT_CONFIG_GLOBAL": str(git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "WW_UPDATE_CHECK": "0",
            "WW_STATE_HOME": str(base / "state"),
            "WW_USER_CONFIG_DIR": str(base / "user"),
        }
    )


def _table(
    sizes: dict[str, dict[str, int]], before: dict[str, dict[str, int]]
) -> str:
    rows = [("page", "characters", "tokens", "before", "change")]
    total = earlier = 0
    for name, size in sizes.items():
        tokens = size["tokens"]
        total += tokens
        old = before.get(name, {}).get("tokens")
        if old is not None:
            earlier += old
        rows.append(
            (name, str(size["characters"]), str(tokens), *_change(old, tokens))
        )
    rows.append(("total", "", str(total), *_change(earlier or None, total)))
    widths = [max(len(row[column]) for row in rows) for column in range(5)]
    return "\n".join(
        "  ".join(
            cell.ljust(width) if column == 0 else cell.rjust(width)
            for column, (cell, width) in enumerate(zip(row, widths, strict=True))
        ).rstrip()
        for row in rows
    )


def _change(old: int | None, new: int) -> tuple[str, str]:
    if not old:
        return ("", "")
    return (str(old), f"{(new - old) * 100 / old:+.0f}%")


if __name__ == "__main__":
    raise SystemExit(main())
