# SPDX-License-Identifier: GPL-3.0-or-later
"""Announcing a newer ww before running the command the caller asked for."""

from __future__ import annotations

import argparse
import json
import sys

from ww.errors import WwError
from ww.executable import DEFAULT_EXECUTABLE, printed_executable, project_command
from ww.package_updates import (
    PackageUpdateNotice,
    last_package_notice,
    mark_package_announced,
    pending_package_notice,
)
from ww.project_config import load_project_config
from ww.storage import Storage
from ww.updates import (
    UpdateNotice,
    check_enabled,
    installation_checkout,
    last_notice,
    mark_announced,
    pending_notice,
)


def announce(storage: Storage, *, to_stderr: bool = False) -> None:
    """Write a pending update notice ahead of the command's own output.

    A notice is a convenience and may never disturb the command: unreadable
    state, a remote that will not answer, or a read-only home leaves the
    invocation exactly as it would otherwise have been. ``to_stderr`` keeps it
    out of stdout that something else is going to parse.
    """
    try:
        if not update_check_wanted(storage):
            return
        checkout = installation_checkout()
        notice = (
            pending_notice(checkout)
            if checkout is not None
            else pending_package_notice()
        )
        if notice is None:
            return
        stream = sys.stderr if to_stderr else sys.stdout
        configured = load_project_config(storage.project_config_path).executable
        # Without a launcher to run, the notice names the binary itself.
        executable = (
            project_command(configured, storage.root)
            if configured or (storage.root / "ww").is_file()
            else DEFAULT_EXECUTABLE
        )
        with printed_executable(executable):
            stream.write(notice.render())
        stream.flush()
        _mark(notice)
    except Exception:  # noqa: BLE001 - contain every failure of a convenience
        return


def render_updates(storage: Storage, args: argparse.Namespace) -> str:
    """Render ``ww updates``: what the last check found, or a fresh look."""
    if not update_check_wanted(storage):
        return _rendered(None, args, "The ww update check is turned off.\n")
    checkout = installation_checkout()
    notice: UpdateNotice | PackageUpdateNotice | None
    if checkout is None:
        pending_package_notice(force=bool(args.now))
        notice = last_package_notice()
    else:
        pending_notice(checkout, force=bool(args.now))
        notice = last_notice()
    if notice is not None:
        _mark(notice)
    return _rendered(
        notice,
        args,
        "ww is up to date.\n"
        if checkout is not None
        else "No package update notice is available.\n",
    )


def update_check_wanted(storage: Storage) -> bool:
    """Whether this project and environment want the check to run."""
    try:
        configured = load_project_config(storage.project_config_path).update_check
    except WwError:
        configured = True
    return check_enabled(configured)


def _rendered(
    notice: UpdateNotice | PackageUpdateNotice | None,
    args: argparse.Namespace,
    otherwise: str,
) -> str:
    if args.json_output:
        return (
            json.dumps(
                {
                    "update_available": notice is not None,
                    "notice": notice.to_dict() if notice else None,
                },
                indent=2,
            )
            + "\n"
        )
    return notice.render() if notice else otherwise


def _mark(notice: UpdateNotice | PackageUpdateNotice) -> None:
    if isinstance(notice, PackageUpdateNotice):
        mark_package_announced(notice)
    else:
        mark_announced(notice)
