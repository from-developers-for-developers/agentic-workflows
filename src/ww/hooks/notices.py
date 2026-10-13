# SPDX-License-Identifier: GPL-3.0-or-later
"""The few lines the session-start hook shows.

Whatever a session-start hook prints stays in the agent's context for the
rest of the session, so the text is short and plain: no headings and no
task lists.
"""

from __future__ import annotations

from ww.executable import ww_command


def session_context(*, compacted: bool, on_request: bool = False) -> str:
    """What a session learns about ww when it starts, resumes, or compacts.

    One short line (two after a compaction): that ww coordinates file
    changes here and when to run ``discover``. No task is listed.
    """
    ww = ww_command()
    lines = []
    if compacted:
        lines.append("Context was compacted; ww's task state is authoritative.")
    lines.append(
        "This project has ww available on request only: use it only when the "
        "user explicitly asks for ww; otherwise work without it and do not "
        f"ask. `{ww} discover` lists its workflows."
        if on_request
        else "This project coordinates file changes through ww: before "
        f"changing files, run `{ww} discover` once."
    )
    return "\n".join(lines) + "\n"
