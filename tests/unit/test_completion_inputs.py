# SPDX-License-Identifier: GPL-3.0-or-later
"""Checks on the values an agent passes when it completes a step."""

import pytest

from ww.completion_inputs import validate_requested_values, validate_values
from ww.errors import StateError
from ww.workflow_config import ProvidedVariable


def test_duplicate_values_keep_the_existing_completion_error() -> None:
    with pytest.raises(
        StateError, match="a completion variable can only be supplied once"
    ):
        validate_values((("task_id", "ONE"), ("task_id", "TWO")))


def test_unexpected_values_are_reported_before_missing_values() -> None:
    with pytest.raises(
        StateError, match="unexpected completion variable\\(s\\): extra"
    ):
        validate_requested_values({"extra": "value"}, (ProvidedVariable("task_id"),))
