# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in project defaults created by ``ww-agentic-workflows init``."""

import json
from copy import deepcopy
from importlib.resources import files
from typing import Any

from ww.config_files import (
    FILE_STEM,
    LOCAL_SETTINGS_FILE,
    SETTINGS_FILE,
    USER_DIR_VARIABLE,
)
from ww.executable import DEFAULT_EXECUTABLE
from ww.project_config import (
    BUILTIN_DEFAULTS,
    AgentHooks,
    Limits,
)
from ww.runtimes import DEFAULT_RUNTIME

DEFAULT_WORKFLOWS_YAML = """modes: []
handlers: []
hooks: {}
workflows: []
"""


def default_settings() -> dict[str, Any]:
    """Every root-level setting with its default, in the order init writes them.

    The settings file init creates holds all of them, so each option can be
    found and changed in place.
    """
    return {
        "enabled": True,
        "runtime": DEFAULT_RUNTIME,
        "update_check": True,
        "executable": DEFAULT_EXECUTABLE,
        "task_format": "TASK-{{uuid}}",
        "limits": Limits().to_dict(),
        "agent_hooks": AgentHooks().to_dict(),
        "rules": {"scripting": True},
        "builtins": deepcopy(BUILTIN_DEFAULTS),
        "workflows": {},
        "projects": [],
        "extensions": {},
    }


DEFAULT_PROJECT_CONFIG_JSON = json.dumps(default_settings(), indent=2) + "\n"


# ``./ww`` runs the binary the settings levels name in ``executable``, read on
# every run so a project switches installs by editing one line; the local file
# wins over the repo one, which wins over the user's. Without python3 or the
# key it runs the standard name.
PROJECT_LAUNCHER = f"""#!/bin/sh
set -eu
project_root=$(CDPATH= cd "$(dirname "$0")" && pwd)
cd "$project_root"
executable={DEFAULT_EXECUTABLE}
if command -v python3 >/dev/null 2>&1; then
  configured=$(python3 -c '
import json, os
user = os.environ.get("{USER_DIR_VARIABLE}") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "{FILE_STEM}",
)
value = None
for path in (
    os.path.join(user, "{SETTINGS_FILE}"),
    "{SETTINGS_FILE}",
    "{LOCAL_SETTINGS_FILE}",
):
    try:
        found = json.load(open(path)).get("executable")
    except (OSError, ValueError, AttributeError):
        continue
    if isinstance(found, str) and found.strip():
        value = found.strip()
print(value or "")
' 2>/dev/null || true)
  if [ -n "$configured" ]; then
    executable=$configured
  fi
fi
exec "$executable" "$@"
"""


AGENT_INSTRUCTIONS = (
    files("ww.assets").joinpath("agent_instructions.md").read_text(encoding="utf-8")
)
# The skills ``init`` offers to install into each agent directory, by name:
# ``ww`` to work through ww, ``noww`` for the operator to opt out of it,
# ``ww-rule`` to write rules for ww's steps from the operator's words, and
# ``ww-setup`` with the skills it guides through, each starting one of ww's
# learning and setup workflows (``ww-refresh`` reruns the learning ones;
# ``ww-setup`` itself starts ``ww-express``, which has no skill of its own).
WW_SKILL_NAME = "ww"
SKILLS = {
    name: files("ww.assets").joinpath(f"{name}_skill.md").read_text(encoding="utf-8")
    for name in (
        WW_SKILL_NAME,
        "noww",
        "ww-rule",
        "ww-setup",
        "ww-learn",
        "ww-learn-project",
        "ww-suggest",
        "ww-refresh",
        "ww-solve",
        "ww-rules-from-artifacts",
        "ww-automate",
    )
}


def skill_location(directory: str, name: str) -> str:
    """Where a skill lives inside an agent directory."""
    return f"{directory}/skills/{name}/SKILL.md"
