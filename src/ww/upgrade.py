# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit upgrades through the installer that owns this ww installation.

Before it installs a new major version, ``upgrade`` checks the project it
runs in against that version (:mod:`ww.upgrade_checks`) and refuses, unless
forced, while the project holds configuration or open runs the new version
no longer reads. It has to know the version first: a package installation
asks PyPI for the release pip or pipx is about to install, and a Git
checkout reads ``pyproject.toml``'s release series at its upstream commit
after fetching it. When neither answers, the upgrade goes ahead and says the
check could not run. After a successful upgrade, the new version checks the
project's configuration in a process of its own and offers ``ww migrate``;
nothing is migrated without the operator.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import TYPE_CHECKING

from ww import __version__
from ww.errors import StateError
from ww.executable import ww_command
from ww.package_updates import (
    PACKAGE_NAME,
    allows_prereleases,
    fetch_releases,
    latest_compatible,
)
from ww.updates import installation_checkout
from ww.upgrade_checks import crosses_major, readiness

if TYPE_CHECKING:
    from ww.storage import Storage

# The version ``pyproject.toml`` names: the release series at that commit.
_PROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
# The new version's own check of the configuration, which may take a while
# on a large project but must never hold the upgrade's report forever.
CHECK_TIMEOUT_SECONDS = 120.0


def _run(
    command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> str:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, check=False
        )
    except OSError as error:
        raise StateError(f"Cannot run {command[0]}: {error}") from error
    if result.returncode:
        raise StateError(
            result.stderr.strip() or result.stdout.strip() or "Upgrade failed"
        )
    return result.stdout.strip()


def _editable_install() -> bool:
    try:
        direct = distribution(PACKAGE_NAME).read_text("direct_url.json")
        data = json.loads(direct) if direct else {}
    except (PackageNotFoundError, ValueError):
        return False
    directory = data.get("dir_info") if isinstance(data, dict) else None
    return isinstance(directory, dict) and directory.get("editable") is True


def upgrade(
    *, pre: bool = False, force: bool = False, storage: Storage | None = None
) -> str:
    """Upgrade through the installer or Git checkout that owns this installation.

    With ``storage``, a move to a new major version is refused while that
    project holds what the new version removes, unless ``force``; after the
    upgrade, the new version checks the project's configuration.
    """
    # Only a ww project has anything to check.
    project = storage if storage and storage.config_path.is_file() else None
    checkout = installation_checkout()
    if checkout is not None:
        if _run(["git", "status", "--porcelain"], cwd=checkout):
            raise StateError(
                "The ww checkout has local changes; commit or stash them first"
            )
        # Require the actual tracking branch: never pull a default into a feature.
        _run(["git", "symbolic-ref", "--quiet", "HEAD"], cwd=checkout)
        _run(["git", "rev-parse", "--verify", "@{upstream}"], cwd=checkout)
        _run(["git", "fetch", "--quiet"], cwd=checkout)
        target = _checkout_version(checkout, "@{upstream}")
        current = _checkout_version(checkout, "HEAD") or __version__
        notes = _gate(project, current, target, force)
        # What was checked is what is installed: the fetched upstream commit.
        output = _run(["git", "merge", "--ff-only", "@{upstream}"], cwd=checkout)
        return (
            f"Updated the ww checkout at {checkout}.\n{output}\n"
            f"{notes}{_after(project)}"
        )
    if _editable_install():
        raise StateError("This editable ww installation has no Git checkout to update")
    include_pre = allows_prereleases(__version__, pre=pre)
    metadata = Path(sys.prefix) / "pipx_metadata.json"
    if metadata.is_file():
        try:
            data = json.loads(metadata.read_text())
            package = data["main_package"]["package"]
            environment = data.get("environment") or Path(sys.prefix).name
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise StateError(
                f"Cannot read pipx installation metadata: {error}"
            ) from error
        if package != PACKAGE_NAME:
            raise StateError(
                "ww is injected into another pipx environment; upgrade it with pipx"
            )
        if not isinstance(environment, str) or not environment:
            raise StateError("pipx installation metadata has no environment name")
        pipx = shutil.which("pipx")
        if pipx is None:
            raise StateError("This ww is managed by pipx, but pipx is not on PATH")
        command = [pipx, "upgrade", environment]
        if include_pre:
            command.append("--pip-args=--pre")
    else:
        command = [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE_NAME]
        if include_pre:
            command.append("--pre")
    environment_variables = None
    if metadata.is_file():
        environment_variables = {
            **os.environ,
            "PIPX_HOME": str(Path(sys.prefix).parent.parent),
        }
    target = _package_target(include_pre) if project else None
    notes = _gate(project, __version__, target, force)
    output = _run(command, env=environment_variables)
    return f"ww upgrade finished.\n{output}\n{notes}{_after(project)}"


def _package_target(include_pre: bool) -> str | None:
    """The release pip or pipx is about to install, or ``None`` offline."""
    releases = fetch_releases()
    if releases is None:
        return None
    return latest_compatible(releases, __version__, pre=include_pre)


def _checkout_version(checkout: Path, ref: str) -> str | None:
    """The release series ``pyproject.toml`` names at ``ref``, if it reads."""
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:pyproject.toml"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode:
        return None
    match = _PROJECT_VERSION.search(result.stdout)
    return match.group(1) if match else None


def _gate(
    storage: Storage | None, current: str, target: str | None, force: bool
) -> str:
    """Refuse a new major version the project is not ready for, unless forced.

    Returns what the upgrade's report adds: what ``force`` overrode, or that
    the version to install, and so the check, was unknown.
    """
    if storage is None:
        return ""
    if target is None:
        return (
            "ww could not tell which version this installs, so it did not check "
            "this project against a new major version.\n"
        )
    if not crosses_major(current, target):
        return ""
    ready = readiness(storage, target)
    if not ready.blocked:
        return ""
    if force:
        return f"Upgraded with --force although ww {target} refuses:\n{ready.render()}"
    raise StateError(
        f"ww {target} is a new major version, and this project holds what it "
        f"no longer reads; nothing was upgraded.\n{ready.render()}"
        f"Then run `{ww_command()} upgrade` again, or upgrade anyway with "
        f"`{ww_command()} upgrade --force`."
    )


def _after(storage: Storage | None) -> str:
    """The new version's check of the project's configuration, and the offer.

    It runs in a new process, so it is the upgraded ww that judges. It only
    reports: ``ww migrate`` changes nothing until the operator runs it.
    """
    if storage is None:
        return ""
    command = [
        sys.executable,
        "-m",
        "ww.cli",
        "--root",
        str(storage.root),
        "migrate",
        "--check",
        "--deprecations",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=CHECK_TIMEOUT_SECONDS,
            env={**os.environ, "WW_UPDATE_CHECK": "0"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        return (
            f"The upgraded ww could not check this project ({error}); run "
            f"`{ww_command()} migrate --check --deprecations`.\n"
        )
    if result.returncode == 0:
        return "The upgraded ww found nothing to migrate in this project.\n"
    found = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    return (
        "The upgraded ww found configuration to migrate in this project:\n"
        f"{found}\n"
        f"Run `{ww_command()} migrate` to see the changes, and "
        f"`{ww_command()} migrate --apply` to make them, or start the "
        "ww-migrate workflow to go through them with an agent. Nothing was "
        "changed.\n"
    )
