# SPDX-License-Identifier: GPL-3.0-or-later
"""Announcing a newer ww before running the command the caller asked for."""

from __future__ import annotations

import argparse
import json
import sys

from ww.errors import WwError
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
        notice = pending_notice(installation_checkout())
        if notice is None:
            return
        stream = sys.stderr if to_stderr else sys.stdout
        stream.write(notice.render())
        stream.flush()
        mark_announced(notice)
    except Exception:  # noqa: BLE001 - contain every failure of a convenience
        return


def render_updates(storage: Storage, args: argparse.Namespace) -> str:
    """Render ``ww updates``: what the last check found, or a fresh look."""
    if not update_check_wanted(storage):
        return _rendered(None, args, "The ww update check is turned off.\n")
    checkout = installation_checkout()
    if checkout is None:
        return _rendered(
            None,
            args,
            "This ww was not installed from a Git checkout, so there is "
            "nothing to compare it against.\n",
        )
    pending_notice(checkout, force=bool(args.now))
    notice = last_notice()
    if notice is not None:
        mark_announced(notice)
    return _rendered(notice, args, "ww is up to date.\n")


def update_check_wanted(storage: Storage) -> bool:
    """Whether this project and environment want the check to run."""
    try:
        configured = load_project_config(storage.project_config_path).update_check
    except WwError:
        configured = True
    return check_enabled(configured)


def _rendered(
    notice: UpdateNotice | None, args: argparse.Namespace, otherwise: str
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
