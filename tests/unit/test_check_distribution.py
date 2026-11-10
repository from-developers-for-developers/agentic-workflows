# SPDX-License-Identifier: GPL-3.0-or-later
"""The distribution check installs the wheel the way a user would."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_distribution.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_distribution", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wheel_is_installed_with_its_declared_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The smoke test imports modules that need PyYAML, a declared dependency.

    Installing with ``--no-deps`` leaves it out of the fresh venv, so the
    import smoke test fails with ``No module named 'yaml'``.
    """
    script = _load_script()
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "ww_agentic_workflows-1.0.0-py3-none-any.whl"
    wheel.touch()
    (dist / "ww_agentic_workflows-1.0.0.tar.gz").touch()
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(script, "_run", lambda *command, cwd: commands.append(command))
    for check in (
        "_require_members",
        "_require_license",
        "_require_document_parity",
        "_require_exact_skills",
    ):
        monkeypatch.setattr(script, check, lambda *arguments: None)
    monkeypatch.setattr(sys, "argv", ["check_distribution.py", "--dist-dir", str(dist)])

    assert script.main() == 0

    (install,) = [
        command for command in commands if command[1:4] == ("-m", "pip", "install")
    ]
    assert str(wheel.resolve()) in install
    assert "--no-deps" not in install
