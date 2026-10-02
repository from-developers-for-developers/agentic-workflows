# SPDX-License-Identifier: GPL-3.0-or-later
"""The YAML ww writes reads like YAML a person would write."""

from __future__ import annotations

from typing import Any

import yaml

from ww.config_writes import dump_yaml


def _setup() -> dict[str, Any]:
    shared = {"argv": ["pytest", "-q"]}
    return {
        "workflows": [
            {
                "name": "bugfix",
                "description": "Find why a bug happens, fix it, and prove it "
                "stays fixed with a test that failed before the fix and passes "
                "after it.",
                "steps": [
                    {
                        "investigate": "Find the cause.",
                        "handlers": [{"run-tests": None}],
                    },
                    {"fix": "Line one.\nLine two.\n", "check": shared},
                ],
            },
            {"name": "review", "steps": [{"read": "Read.", "check": shared}]},
        ],
        "modes": [{"gently": "Say it kindly."}],
        "hooks": {"after_complete": [{"argv": ["true"]}]},
    }


def test_written_yaml_loads_back_to_the_same_data() -> None:
    data = _setup()

    assert yaml.safe_load(dump_yaml(data)) == data


def test_mappings_are_written_in_block_style() -> None:
    text = dump_yaml(_setup())

    assert "{" not in text
    assert "        check:\n          argv: [pytest, -q]\n" in text


def test_a_value_used_twice_is_written_out_twice() -> None:
    text = dump_yaml(_setup())

    assert "&id" not in text
    assert "*id" not in text


def test_a_handler_shorthand_has_an_empty_value() -> None:
    text = dump_yaml(_setup())

    assert "null" not in text
    assert "- run-tests:\n" in text


def test_long_text_is_folded_and_multi_line_text_is_a_literal_block() -> None:
    text = dump_yaml(_setup())

    assert "description: >-\n" in text
    assert "fix: |\n" in text
    assert max(len(line) for line in text.splitlines()) <= 88


def test_sequences_are_indented_under_their_key() -> None:
    text = dump_yaml(_setup())

    assert "workflows:\n  - name: bugfix\n" in text
    assert "\n    steps:\n      - investigate:" in text


def test_top_level_sections_and_list_items_are_separated_by_a_blank_line() -> None:
    text = dump_yaml(_setup())

    assert "\n\n  - name: review\n" in text
    assert "\n\nmodes:\n" in text
    assert "\n\nhooks:\n" in text
    assert not text.startswith("\n")
    assert not text.endswith("\n\n")
