# SPDX-License-Identifier: GPL-3.0-or-later
"""The worker's first page says it is addressed to the worker."""

from __future__ import annotations

from pathlib import Path

from tests.page_catalog import render_page


def test_the_worker_page_states_who_it_is_for_and_what_it_may_change(
    tmp_path: Path,
) -> None:
    page = " ".join(render_page("step-auto-worker", tmp_path).split())
    assert "This assignment is addressed to you, the worker." in page
    assert "Running the commands this page displays is expected" in page
    assert "even where they name the parent task" in page
    assert "Change only your own branch and worktree" in page


def test_the_manager_page_does_not_carry_the_worker_statement(tmp_path: Path) -> None:
    page = render_page("worker-bootstrap", tmp_path)
    assert "addressed to you, the worker" not in page
