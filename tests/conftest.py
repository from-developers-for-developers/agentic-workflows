# SPDX-License-Identifier: GPL-3.0-or-later
"""Test-suite isolation from machine-level configuration."""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import patch

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_git_configuration(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Keep test Git processes independent from the operator's configuration."""
    config = tmp_path_factory.mktemp("git-config") / "global"
    config.write_text("[commit]\n\tgpgsign = false\n", encoding="utf-8")
    environment = {
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    with patch.dict(os.environ, environment):
        yield


@pytest.fixture(scope="session", autouse=True)
def isolated_update_check(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Keep the update notice out of the suite, and out of the real home.

    Every ww command announces a pending update, which would otherwise fetch
    this checkout's remote and write to the operator's own state file. Tests
    that exercise the notice turn it back on for themselves.
    """
    environment = {
        "WW_UPDATE_CHECK": "0",
        "WW_STATE_HOME": str(tmp_path_factory.mktemp("ww-state")),
    }
    with patch.dict(os.environ, environment):
        yield


@pytest.fixture(scope="session", autouse=True)
def isolated_machine_configuration(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Keep the operator's machine-level configuration out of every test."""
    environment = {
        "WW_MACHINE_CONFIG_DIR": str(tmp_path_factory.mktemp("ww-machine")),
    }
    with patch.dict(os.environ, environment):
        yield
