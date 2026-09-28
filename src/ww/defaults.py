# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in project defaults created by ``ww-agentic-workflows init``."""

from importlib.resources import files

from ww.config_files import SETTINGS_FILE
from ww.executable import DEFAULT_EXECUTABLE

DEFAULT_WORKFLOWS_YAML = """task_format: TASK-{uuid}

modes: []
handlers: []
hooks: {}
workflows: []
"""

DEFAULT_PROJECT_CONFIG_JSON = f"""{{
  "enabled": true,
  "executable": "{DEFAULT_EXECUTABLE}",
  "loop_max_times": 3,
  "extensions": {{}}
}}
"""


# ``./ww`` runs the binary the settings file names in ``executable``, read
# on every run so a project switches installs by editing one line. Without
# python3 or the key it runs the standard name.
def _launcher(settings_file: str) -> str:
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


PROJECT_LAUNCHER = _launcher(SETTINGS_FILE)
# Launchers earlier ww versions wrote; init replaces one left as written.
GENERATED_LAUNCHERS = (
    _launcher("agentic-workflows.json"),
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
