# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit upgrades through the installer that owns this ww installation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from ww import __version__
from ww.errors import StateError
from ww.open_work import open_work
from ww.package_updates import PACKAGE_NAME, allows_prereleases
from ww.storage import Storage
from ww.updates import installation_checkout


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


def _refuse_open_tasks(storage: Storage) -> None:
    work = open_work(storage.task_persistence, storage.root)
    if work.tasks or work.unreadable:
        ids = [task.task_id for task in work.tasks]
        ids.extend(task.task_id for task in work.unreadable)
        raise StateError(
            "Finish open tasks or resolve unreadable task records before upgrading: "
            + ", ".join(ids)
        )


def _editable_install() -> bool:
    try:
        direct = distribution(PACKAGE_NAME).read_text("direct_url.json")
        data = json.loads(direct) if direct else {}
    except (PackageNotFoundError, ValueError):
        return False
    directory = data.get("dir_info") if isinstance(data, dict) else None
    return isinstance(directory, dict) and directory.get("editable") is True


def upgrade(storage: Storage, *, pre: bool = False) -> str:
    """Upgrade only when this project's saved tasks are safe to leave behind."""
    _refuse_open_tasks(storage)
    checkout = installation_checkout()
    if checkout is not None:
        if checkout != storage.root:
            _refuse_open_tasks(Storage(checkout))
        if _run(["git", "status", "--porcelain"], cwd=checkout):
            raise StateError(
                "The ww checkout has local changes; commit or stash them first"
            )
        # Require the actual tracking branch: never pull a default into a feature.
        _run(["git", "symbolic-ref", "--quiet", "HEAD"], cwd=checkout)
        _run(["git", "rev-parse", "--verify", "@{upstream}"], cwd=checkout)
        output = _run(["git", "pull", "--ff-only"], cwd=checkout)
        return f"Updated the ww checkout at {checkout}.\n{output}\n"
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
    output = _run(command, env=environment_variables)
    return f"ww upgrade finished.\n{output}\n"
