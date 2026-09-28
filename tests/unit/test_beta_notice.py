# SPDX-License-Identifier: GPL-3.0-or-later
"""ww says it is in beta wherever someone first meets it."""

from __future__ import annotations

import pytest

from ww import BETA_NOTICE, STAGE, __version__
from ww.cli import main
from ww.output import render_initialization_welcome

LIMITATIONS = (
    "https://github.com/from-developers-for-developers/agentic-workflows/blob/"
    "main/documentation/limitations.md"
)


def test_version_names_the_stage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])

    assert capsys.readouterr().out.strip() == (
        f"ww-agentic-workflows {__version__} ({STAGE})"
    )
    assert STAGE == "beta"


@pytest.mark.parametrize("columns", [60, 200])
def test_the_init_welcome_says_ww_is_in_beta(columns: int) -> None:
    welcome = render_initialization_welcome(False, columns)
    flat = " ".join(welcome.split())

    assert " ".join(BETA_NOTICE.split()) in flat
    # The link stays on one line, even in a narrow terminal.
    assert LIMITATIONS in welcome.splitlines()


def test_json_init_output_carries_no_welcome() -> None:
    assert render_initialization_welcome(True) == ""
