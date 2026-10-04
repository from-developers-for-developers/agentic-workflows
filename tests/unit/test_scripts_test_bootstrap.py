# SPDX-License-Identifier: GPL-3.0-or-later
"""The test runner selects supported interpreters and repairs partial venvs."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "test"


def _runner_project(tmp_path: Path, *, supported: bool = True) -> tuple[Path, Path]:
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / "test")
    (project / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.10"\n', encoding="utf-8"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    interpreter = fake_bin / "python3.12"
    interpreter.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        + ("  exit 0\n" if supported else "  exit 1\n")
        + "fi\n"
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
        '  mkdir -p "$3/bin"\n'
        "  cat > \"$3/bin/python\" <<'PYTHON'\n"
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        '  case "$2" in *find_spec*) '
        '[ -e "$(dirname "$0")/tools-ready" ] ;; *) exit 0 ;; esac\n'
        "  exit $?\n"
        'elif [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n'
        '  [ -z "$FAKE_PIP_FAILS" ] || exit 1\n'
        '  printf \'%s\\n\' "$*" >> "$(dirname "$0")/pip.log"\n'
        '  case "$*" in *\' -e .[dev]\'*) touch "$(dirname "$0")/tools-ready" ;; esac\n'
        'elif [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then\n'
        '  printf \'%s\\n\' "$*" >> "$(dirname "$0")/pytest.log"\n'
        "fi\n"
        "exit 0\n"
        "PYTHON\n"
        '  chmod +x "$3/bin/python"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    return project, fake_bin


def _run(
    project: Path,
    fake_bin: Path,
    *,
    override: str = "",
    installer_fails: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "WW_PYTHON": override,
        "FAKE_PIP_FAILS": "1" if installer_fails else "",
    }
    return subprocess.run(
        ["/bin/sh", str(project / "scripts" / "test"), "tests/unit"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_fresh_test_environment_uses_a_supported_python_without_network(
    tmp_path: Path,
) -> None:
    project, fake_bin = _runner_project(tmp_path)

    result = _run(project, fake_bin)

    assert result.returncode == 0, result.stderr
    assert (project / ".venv/bin/tools-ready").exists()
    pip_calls = (project / ".venv/bin/pip.log").read_text(encoding="utf-8")
    assert "pip install -q --upgrade pip" in pip_calls
    assert "pip install -q -e .[dev]" in pip_calls
    assert "tests/unit" in (project / ".venv/bin/pytest.log").read_text(
        encoding="utf-8"
    )

    result = _run(project, fake_bin)

    assert result.returncode == 0, result.stderr
    assert (
        len((project / ".venv/bin/pip.log").read_text(encoding="utf-8").splitlines())
        == 2
    )


def test_fresh_test_environment_accepts_an_explicit_python_override(
    tmp_path: Path,
) -> None:
    project, fake_bin = _runner_project(tmp_path)

    result = _run(project, fake_bin, override=str(fake_bin / "python3.12"))

    assert result.returncode == 0, result.stderr
    assert (project / ".venv/bin/tools-ready").exists()


def test_partial_supported_environment_is_repaired_in_place(tmp_path: Path) -> None:
    project, fake_bin = _runner_project(tmp_path)
    venv_bin = project / ".venv/bin"
    venv_bin.mkdir(parents=True)
    helper_project, helper_bin = _runner_project(tmp_path / "helper")
    result = subprocess.run(
        ["/bin/sh", str(helper_project / "scripts/test"), "tests/unit"],
        cwd=helper_project,
        env={**os.environ, "PATH": f"{helper_bin}:/usr/bin:/bin"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    shutil.copy2(helper_project / ".venv/bin/python", venv_bin / "python")

    result = _run(project, fake_bin)

    assert result.returncode == 0, result.stderr
    assert (venv_bin / "tools-ready").exists()
    assert "pip install -q -e .[dev]" in (venv_bin / "pip.log").read_text(
        encoding="utf-8"
    )


def test_missing_supported_python_has_a_clear_diagnostic(tmp_path: Path) -> None:
    project, fake_bin = _runner_project(tmp_path, supported=False)
    old_python = fake_bin / "python3"
    old_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    old_python.chmod(0o755)

    result = _run(project, fake_bin)

    assert result.returncode != 0
    assert "Python >= 3.10 is required" in result.stderr
    assert not (project / ".venv").exists()


def test_failed_install_removes_the_environment_it_just_created(
    tmp_path: Path,
) -> None:
    project, fake_bin = _runner_project(tmp_path)

    result = _run(project, fake_bin, installer_fails=True)

    assert result.returncode != 0
    assert not (project / ".venv").exists()
    assert not (project / ".venv/bin/pytest.log").exists()


def test_failed_install_preserves_an_existing_partial_environment(
    tmp_path: Path,
) -> None:
    project, fake_bin = _runner_project(tmp_path)
    helper_project, helper_bin = _runner_project(tmp_path / "helper")
    setup = subprocess.run(
        ["/bin/sh", str(helper_project / "scripts/test"), "tests/unit"],
        cwd=helper_project,
        env={**os.environ, "PATH": f"{helper_bin}:/usr/bin:/bin"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert setup.returncode == 0, setup.stderr
    venv_bin = project / ".venv/bin"
    venv_bin.mkdir(parents=True)
    shutil.copy2(helper_project / ".venv/bin/python", venv_bin / "python")
    (venv_bin / "marker").write_text("keep", encoding="utf-8")

    result = _run(project, fake_bin, installer_fails=True)

    assert result.returncode != 0
    assert (venv_bin / "python").exists()
    assert (venv_bin / "marker").read_text(encoding="utf-8") == "keep"
    assert not (venv_bin / "tools-ready").exists()
    assert not (venv_bin / "pytest.log").exists()
