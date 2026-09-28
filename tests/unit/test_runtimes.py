# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for runtime-specific orchestration guidance."""

import pytest

from ww.errors import ConfigurationError
from ww.runtimes import RUNTIME_INSTRUCTIONS, runtime_instruction


def test_exactly_two_runtimes_are_supported() -> None:
    assert sorted(RUNTIME_INSTRUCTIONS) == ["auto", "single"]


@pytest.mark.parametrize("runtime", sorted(RUNTIME_INSTRUCTIONS))
def test_every_runtime_defines_responsibilities_and_the_ww_boundary(
    runtime: str,
) -> None:
    guidance = "\n".join(runtime_instruction(runtime))

    assert "manager" in guidance
    assert "worker" in guidance
    assert "only ./ww start, next, and complete operate this flow" in guidance
    assert "Do not mimic or bypass" in guidance
    assert "ww-agentic-workflows.yaml" in guidance


def test_single_is_both_manager_and_worker() -> None:
    guidance = "\n".join(runtime_instruction("single"))

    assert "manager and worker" in guidance
    assert "Do not spawn subagents" in guidance


def test_auto_dispatches_assignments_and_workers_submit_results() -> None:
    guidance = "\n".join(runtime_instruction("auto"))

    assert "manager dispatches each assignment" in guidance
    assert "worker submits its own results" in guidance
    assert "--role worker" in guidance


def test_role_specific_guidance_does_not_assign_worker_manager_commands() -> None:
    worker = "\n".join(runtime_instruction("auto", "worker"))
    manager = "\n".join(runtime_instruction("auto", "manager"))

    assert "Worker responsibility" in worker
    assert "Manager responsibility" not in worker
    assert "Manager responsibility" in manager


def test_auto_offers_both_worker_shapes() -> None:
    guidance = "\n".join(runtime_instruction("auto"))

    assert "one worker for every step" in guidance
    assert "a fresh worker per step" in guidance


def test_auto_covers_execution_settings_this_session_cannot_change() -> None:
    guidance = "\n".join(runtime_instruction("auto"))

    assert "execution settings this session cannot change" in guidance


def test_a_retired_runtime_names_the_supported_ones() -> None:
    with pytest.raises(ConfigurationError) as error:
        runtime_instruction("delegate")

    assert "auto, single" in str(error.value)
