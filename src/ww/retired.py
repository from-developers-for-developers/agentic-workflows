# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in workflows ww retired, and what to run instead.

Tasks that already ran one keep their frozen plan and stay readable; only
starting one again is refused, with the replacement named.
"""

from __future__ import annotations

RETIRED_WORKFLOWS = {
    "ww-learn": (
        "ww no longer interviews the operator about themselves, their role, "
        "team or company; run `ww-learn-project` to learn the project, or "
        "`ww-suggest` to design a setup"
    ),
    "ww-express": (
        "express setup is now `ww-learn-project` followed by `ww-suggest` "
        "started with the requirement 'Express setup'"
    ),
}


def workflow_not_found(name: str) -> str:
    """The message for a workflow that does not exist, naming a replacement."""
    message = f"workflow not found: {name}"
    replacement = RETIRED_WORKFLOWS.get(name)
    return f"{message}; it was retired: {replacement}" if replacement else message
