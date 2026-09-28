# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in project defaults created by ``ww-agentic-workflows init``."""

from importlib.resources import files

from ww.config_files import (
    FILE_STEM,
    LOCAL_SETTINGS_FILE,
    MACHINE_DIR_VARIABLE,
    MACHINE_SETTINGS_FILE,
    SETTINGS_FILE,
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
  "loop_max_times": 3,
  "task_format": "TASK-{{uuid}}",
  "extensions": {{}}
}}
"""


# ``./ww`` runs the binary the settings levels name in ``executable``, read on
# every run so a project switches installs by editing one line; the local file
# wins over the repo one, which wins over the machine's. Without python3 or the
# key it runs the standard name.
PROJECT_LAUNCHER = f"""#!/bin/sh
set -eu
project_root=$(CDPATH= cd "$(dirname "$0")" && pwd)
cd "$project_root"
executable={DEFAULT_EXECUTABLE}
if command -v python3 >/dev/null 2>&1; then
  configured=$(python3 -c '
import json, os
machine = os.environ.get("{MACHINE_DIR_VARIABLE}") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "{FILE_STEM}",
)
value = None
for path in (
    os.path.join(machine, "{MACHINE_SETTINGS_FILE}"),
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
# ``ww`` to work through ww, ``noww`` for the operator to opt out of it.
WW_SKILL_NAME = "ww"
SKILLS = {
    name: files("ww.assets").joinpath(f"{name}_skill.md").read_text(encoding="utf-8")
    for name in (WW_SKILL_NAME, "noww")
}


def skill_location(directory: str, name: str) -> str:
    """Where a skill lives inside an agent directory."""
    return f"{directory}/skills/{name}/SKILL.md"
