# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in project defaults created by ``ww-agentic-workflows init``."""

from importlib.resources import files

DEFAULT_WORKFLOWS_YAML = """task_format: TASK-{uuid}

modes: []
handlers: []
hooks: {}
workflows: []
"""

DEFAULT_PROJECT_CONFIG_JSON = """{
  "enabled": true,
  "loop_max_times": 3,
  "extensions": {}
}
"""

PROJECT_LAUNCHER = """#!/bin/sh
set -eu
project_root=$(CDPATH= cd "$(dirname "$0")" && pwd)
cd "$project_root"
exec ww-agentic-workflows "$@"
"""

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
