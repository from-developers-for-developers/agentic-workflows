#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write documentation/deprecations.md from ww's configuration migrations.

The upgrade guide is generated from ``ww.migrations.config.CONFIG_MIGRATIONS``
(``ww docs upgrade`` renders the same text). Run this after adding or
changing a migration; ``--check`` exits 1 instead of writing when the file
is out of date, and a test checks the same.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "documentation" / "deprecations.md"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 when the file differs from the generated guide; write nothing.",
    )
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    from ww.migrations.config import render_upgrade_guide

    guide = render_upgrade_guide()
    current = GUIDE.read_text(encoding="utf-8") if GUIDE.is_file() else None
    if args.check:
        if current != guide:
            print(
                f"{GUIDE.relative_to(ROOT)} is out of date: run "
                "scripts/generate_upgrade_guide.py",
                file=sys.stderr,
            )
            return 1
        return 0
    if current != guide:
        GUIDE.write_text(guide, encoding="utf-8")
        print(f"wrote {GUIDE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
