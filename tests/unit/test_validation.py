# SPDX-License-Identifier: GPL-3.0-or-later
"""Primitive checks shared by YAML parsing and record decoding."""

from __future__ import annotations

from typing import Literal

import pytest

from ww.errors import ConfigurationError
from ww.validation import (
    expect_keys,
    expect_literal,
    expect_positive_int,
    is_positive_int,
    is_strict_int,
    reject_unknown_keys,
    require_keys,
)


def test_strict_int_excludes_booleans() -> None:
    assert is_strict_int(3) and not is_strict_int(True) and not is_strict_int("3")
    assert is_positive_int(1) and not is_positive_int(0) and not is_positive_int(True)


def test_helpers_raise_the_requested_error_type() -> None:
    with pytest.raises(ValueError, match="count must be a positive integer"):
        expect_positive_int(True, "count")
    with pytest.raises(ConfigurationError, match="steps\\[0\\].limit"):
        expect_positive_int(0, "steps[0].limit", error=ConfigurationError)


def test_key_checks_report_exactly_what_is_wrong() -> None:
    with pytest.raises(ValueError, match="record missing field\\(s\\): b"):
        require_keys({"a": 1}, {"a", "b"}, "record")
    with pytest.raises(ValueError, match="record has unknown key\\(s\\): c"):
        reject_unknown_keys({"a": 1, "c": 2}, {"a", "b"}, "record")
    with pytest.raises(ValueError, match="record has invalid fields"):
        expect_keys({"a": 1}, {"a", "b"}, "record")
    expect_keys({"a": 1, "b": 2}, {"a", "b"}, "record")
    # Stored data may carry fields ww no longer knows; they are left alone.
    expect_keys({"a": 1, "b": 2, "gone": 3}, {"a", "b"}, "record")


def test_literal_membership_uses_the_alias_members() -> None:
    mode = Literal["fast", "slow"]
    assert expect_literal("fast", mode, "mode") == "fast"
    with pytest.raises(ValueError, match="mode must be one of 'fast', 'slow'"):
        expect_literal("medium", mode, "mode")
