# SPDX-License-Identifier: GPL-3.0-or-later
"""Agents ww has no adapter for: custom agents, and agents declared in ww.json.

``custom:<name>`` names any agent, with every neutral default and the
project directory ``.<name>``.  A project can describe a simple agent with
data instead, under ``agents`` in ``ww.json``, without writing Python:

```json
{
  "agents": {
    "aider": {
      "title": "Aider",
      "directory": ".aider",
      "instruction_file": "CONVENTIONS.md",
      "skill_prefix": "/",
      "question_tool": "ask_user"
    }
  }
}
```

Every key is optional.  An agent that needs hooks, a permission format, or a
launcher is an extension's Python adapter instead (see
:mod:`ww.extensions.api`).
"""

from __future__ import annotations

import re
from typing import Any

from ww.errors import ConfigurationError

from .base import Agent
from .interaction import ChoiceMechanism, question_tool
from .layout import Layout

CUSTOM_PREFIX = "custom:"
# A custom or declared agent name, e.g. "my-agent.v2"; "-agent" does not match.
AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
DECLARATION_KEYS = frozenset(
    {"title", "directory", "instruction_file", "skill_prefix", "question_tool"}
)


class CustomAgent(Agent):
    """An agent ww knows only by name: every neutral default."""

    key = "custom"

    def __init__(self, name: str) -> None:
        if not name.startswith(CUSTOM_PREFIX) or not AGENT_NAME.fullmatch(
            short := name.removeprefix(CUSTOM_PREFIX)
        ):
            raise ConfigurationError(
                "custom agent name must contain letters, digits, ., _, or -"
            )
        super().__init__(name)
        self.title = short
        self.layout = Layout(f".{short}")


class DeclaredAgent(Agent):
    """An agent described by a project's ``agents`` settings."""

    key = "declared"

    def __init__(
        self,
        name: str,
        *,
        title: str | None = None,
        directory: str | None = None,
        instruction_file: str | None = None,
        skill_prefix: str = "/",
        choices: ChoiceMechanism | None = None,
    ) -> None:
        super().__init__(name)
        self.title = title or name
        self.layout = Layout(directory or f".{name}", instruction_file=instruction_file)
        self.skill_prefix = skill_prefix
        if choices is not None:
            self.choices = choices

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DeclaredAgent) and (
            self.name,
            self.title,
            self.layout,
            self.skill_prefix,
            self.choices,
        ) == (other.name, other.title, other.layout, other.skill_prefix, other.choices)

    def __hash__(self) -> int:
        return hash((self.name, self.layout))


def parse_declarations(
    data: Any, path: str, reserved: frozenset[str]
) -> tuple[DeclaredAgent, ...]:
    """The agents a settings file's ``agents`` object declares.

    ``reserved`` holds the built-in names, which a declaration may not take.
    """
    if data is None:
        return ()
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}.agents must be an object")
    agents: list[DeclaredAgent] = []
    for name, value in data.items():
        where = f"{path}.agents.{name}"
        if not isinstance(name, str) or not AGENT_NAME.fullmatch(name):
            raise ConfigurationError(
                f"{path}.agents has an invalid agent name {name!r}; use letters, "
                "digits, ., _, or -"
            )
        if name in reserved:
            raise ConfigurationError(
                f"{where}: {name!r} is a built-in agent and cannot be redeclared"
            )
        if not isinstance(value, dict):
            raise ConfigurationError(f"{where} must be an object")
        unknown = set(value) - DECLARATION_KEYS
        if unknown:
            raise ConfigurationError(
                f"{where} has unknown key(s): {', '.join(sorted(unknown))}"
            )
        for key, setting in value.items():
            if not isinstance(setting, str) or not setting.strip():
                raise ConfigurationError(f"{where}.{key} must be a non-empty string")
        tool = value.get("question_tool")
        agents.append(
            DeclaredAgent(
                name,
                title=value.get("title"),
                directory=value.get("directory"),
                instruction_file=value.get("instruction_file"),
                skill_prefix=value.get("skill_prefix", "/"),
                choices=(
                    question_tool(
                        tool, "A free-form answer is a comment, not a choice."
                    )
                    if tool
                    else None
                ),
            )
        )
    return tuple(agents)
