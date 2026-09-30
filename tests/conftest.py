# SPDX-License-Identifier: GPL-3.0-or-later
"""Test-suite isolation from the operator's own configuration."""

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
def isolated_user_configuration(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Keep the operator's user-level configuration out of every test."""
    environment = {
        "WW_USER_CONFIG_DIR": str(tmp_path_factory.mktemp("ww-user")),
    }
    with patch.dict(os.environ, environment):
        # The former variable is an error, so an operator who still sets it
        # must not fail the suite.
        os.environ.pop("WW_MACHINE_CONFIG_DIR", None)
        yield


# The built-in files every test sees unless it asks for all of them.
DEFAULT_TEST_BUILTINS = ("catchall.yaml",)


@pytest.fixture(scope="session", autouse=True)
def catchall_only_builtins(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Compose only the catch-all below the tests' configurations.

    ww's learning workflows are built-ins too; most tests describe a
    project's own workflows and would otherwise list them everywhere. Tests
    of the shipped set use the ``shipped_builtins`` fixture.
    """
    from ww import builtin_workflows

    directory = tmp_path_factory.mktemp("ww-builtins")
    shipped = builtin_workflows.BUILTIN_DIRECTORY
    for name in DEFAULT_TEST_BUILTINS:
        (directory / name).write_text(
            shipped.joinpath(name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    with patch.object(builtin_workflows, "BUILTIN_DIRECTORY", directory):
        yield


@pytest.fixture
def shipped_builtins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every built-in file ww ships, as a project outside the suite sees them."""
    from importlib.resources import files

    from ww import builtin_workflows

    monkeypatch.setattr(
        builtin_workflows,
        "BUILTIN_DIRECTORY",
        files("ww.assets").joinpath("workflows"),
    )
