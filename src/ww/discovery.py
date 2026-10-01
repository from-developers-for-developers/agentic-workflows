# SPDX-License-Identifier: GPL-3.0-or-later
"""Local discovery of agent skills and commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ww.errors import ConfigurationError

AGENT_DIRECTORIES = {
    "codex": ".codex",
    "claudecode": ".claude",
    "gemini": ".gemini",
    "antigravity": ".antigravity",
    "deepseek": ".deepseek",
    "kimi": ".kimi",
    "cursor": ".cursor",
    "grok": ".grok",
}
CUSTOM_AGENT_PREFIX = "custom:"
# A custom agent name, e.g. "my-agent.v2"; "-agent" does not match.
_AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class AvailableActions:
    """Project-local action names available to one agent integration."""

    skills: frozenset[str]
    slash_commands: frozenset[str]


class AgentDiscovery:
    """Discover skills and commands in project-local agent directories."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def available(self, agent: str) -> AvailableActions:
        """Return normalized skill and slash-command names for ``agent``."""
        skills, commands = self._discover(self._roots(normalize_agent(agent)))
        return AvailableActions(
            frozenset(item.removeprefix("/") for item in skills), frozenset(commands)
        )

    def profile(self, agent: str, name: str) -> Path | None:
        """Return a project-local profile from the selected agent's directory."""
        normalized = normalize_agent(agent)
        directory = AGENT_DIRECTORIES.get(normalized, f".{normalized}")
        profiles = self.root / directory / "agents"
        if not profiles.is_dir():
            return None
        return next(
            (
                path
                for path in profiles.rglob("*")
                if path.is_file() and path.stem == name
            ),
            None,
        )

    def _roots(self, agent: str) -> tuple[Path, ...]:
        directory = AGENT_DIRECTORIES.get(agent)
        if directory is None:
            if not _AGENT_NAME.fullmatch(agent):
                raise ConfigurationError(f"invalid custom agent name {agent!r}")
            directory = f".{agent}"
        agent_root = self.root / directory
        return (self.root / ".agents", agent_root)

    @staticmethod
    def _discover(roots: tuple[Path, ...]) -> tuple[set[str], set[str]]:
        skills: set[str] = set()
        commands: set[str] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("SKILL.md"):
                if "skills" in path.parts:
                    skills.add(f"/{path.parent.name}")
            for path in root.rglob("*.md"):
                if "commands" in path.parts:
                    commands.add(path.stem)
        return skills, commands


def normalize_agent(agent: str) -> str:
    """Validate a public agent argument and strip an explicit custom prefix."""
    if agent in AGENT_DIRECTORIES:
        return agent
    if agent.startswith(CUSTOM_AGENT_PREFIX):
        custom_name = agent.removeprefix(CUSTOM_AGENT_PREFIX)
        if _AGENT_NAME.fullmatch(custom_name):
            return custom_name
        raise ConfigurationError(
            "custom agent name must contain letters, digits, ., _, or -"
        )
    supported = ", ".join(AGENT_DIRECTORIES)
    raise ConfigurationError(
        f"unsupported agent {agent!r}; choose one of: {supported}, "
        f"or use {CUSTOM_AGENT_PREFIX}<name>"
    )
