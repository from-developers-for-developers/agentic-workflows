# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in project defaults created by ``ww-agentic-workflows init``."""

from importlib.resources import files

from ww.config_files import (
    FILE_STEM,
    LOCAL_SETTINGS_FILE,
    SETTINGS_FILE,
    USER_DIR_VARIABLE,
)
from ww.executable import DEFAULT_EXECUTABLE

DEFAULT_WORKFLOWS_YAML = """modes: []
handlers: []
hooks: {}
workflows: []
"""

DEFAULT_PROJECT_CONFIG_JSON = f"""{{
  "enabled": true,
  "executable": "{DEFAULT_EXECUTABLE}",
  "max_rounds": 3,
  "max_fixes": 3,
  "task_format": "TASK-{{{{uuid}}}}",
  "extensions": {{}}
}}
"""


def _layered_launcher(variable: str, name: str, user_file: str) -> str:
    """A launcher reading ``executable`` from the user, repo and local files.

    ``variable`` names the user directory's override and ``name`` the Python
    variable holding it; both are parameters only so the launcher an earlier
    ww wrote, with the former machine level, is still recognised.
    """
    return f"""#!/bin/sh
set -eu
project_root=$(CDPATH= cd "$(dirname "$0")" && pwd)
cd "$project_root"
executable={DEFAULT_EXECUTABLE}
if command -v python3 >/dev/null 2>&1; then
  configured=$(python3 -c '
import json, os
{name} = os.environ.get("{variable}") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "{FILE_STEM}",
)
value = None
for path in (
    os.path.join({name}, "{user_file}"),
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


# ``./ww`` runs the binary the settings levels name in ``executable``, read on
# every run so a project switches installs by editing one line; the local file
# wins over the repo one, which wins over the user's. Without python3 or the
# key it runs the standard name.
PROJECT_LAUNCHER = _layered_launcher(USER_DIR_VARIABLE, "user", SETTINGS_FILE)


def _single_file_launcher(settings_file: str) -> str:
    """A launcher that read ``executable`` from one settings file only."""
    return f"""#!/bin/sh
set -eu
project_root=$(CDPATH= cd "$(dirname "$0")" && pwd)
cd "$project_root"
executable={DEFAULT_EXECUTABLE}
if [ -f {settings_file} ] && command -v python3 >/dev/null 2>&1; then
  configured=$(python3 -c '
import json
try:
    value = json.load(open("{settings_file}")).get("executable")
except (OSError, ValueError, AttributeError):
    value = None
print(value.strip() if isinstance(value, str) else "")
' 2>/dev/null || true)
  if [ -n "$configured" ]; then
    executable=$configured
  fi
fi
exec "$executable" "$@"
"""


# Launchers earlier ww versions wrote; init replaces one left as written.
GENERATED_LAUNCHERS = (
    _layered_launcher(
        "WW_MACHINE_CONFIG_DIR", "machine", f"{FILE_STEM}.machine.json"
    ),
    _single_file_launcher(SETTINGS_FILE),
    _single_file_launcher("agentic-workflows.json"),
    """#!/bin/sh
set -eu
project_root=$(CDPATH= cd "$(dirname "$0")" && pwd)
cd "$project_root"
exec ww-agentic-workflows "$@"
""",
)

AGENT_INSTRUCTIONS = (
    files("ww.assets").joinpath("agent_instructions.md").read_text(encoding="utf-8")
)
# The skills ``init`` offers to install into each agent directory, by name:
# ``ww`` to work through ww, ``noww`` for the operator to opt out of it, and
# ``ww-rule`` to write rules for ww's steps from the operator's words.
WW_SKILL_NAME = "ww"
SKILLS = {
    name: files("ww.assets").joinpath(f"{name}_skill.md").read_text(encoding="utf-8")
    for name in (WW_SKILL_NAME, "noww", "ww-rule")
}


def skill_location(directory: str, name: str) -> str:
    """Where a skill lives inside an agent directory."""
    return f"{directory}/skills/{name}/SKILL.md"
