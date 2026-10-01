# SPDX-License-Identifier: GPL-3.0-or-later
"""The agent, model, and reasoning a completion selects for the next step."""

from ww.service import _normalize_completion_selection


def test_auto_selection_clears_model_and_reasoning() -> None:
    selection = _normalize_completion_selection(
        "codex", "auto", "auto", "codex", "gpt-5"
    )

    assert selection.selected_model is None
    assert selection.selected_reasoning is None
    assert selection.clear_selected_model
    assert selection.clear_selected_reasoning


def test_agent_change_clears_inherited_model_and_reasoning() -> None:
    selection = _normalize_completion_selection("other", None, None, "codex", "gpt-5")

    assert selection.clear_selected_model
    assert selection.clear_selected_reasoning


def test_model_change_clears_only_inherited_reasoning() -> None:
    selection = _normalize_completion_selection(
        "codex", "gpt-5.1", None, "codex", "gpt-5"
    )

    assert selection.selected_model == "gpt-5.1"
    assert not selection.clear_selected_model
    assert selection.clear_selected_reasoning


def test_explicit_reasoning_is_retained_when_model_changes() -> None:
    selection = _normalize_completion_selection(
        "codex", "gpt-5.1", "high", "codex", "gpt-5"
    )

    assert selection.selected_reasoning == "high"
    assert not selection.clear_selected_reasoning
