# SPDX-License-Identifier: GPL-3.0-or-later
"""Which adapter serves an agent name.

A name resolves, in this order, to a built-in agent, an agent the project
declares in ``ww.json``, an extension's agent addressed as
``ext/<vendor>/<extension>/agents:<key>``, or ``custom:<name>`` with every
neutral default.  Built-in and extension agents are both :class:`Agent`
subclasses the registry creates with the name they were addressed by.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from ww.errors import ConfigurationError

from .base import Agent
from .claude_code import ClaudeCode
from .codex import Codex
from .cursor import Cursor
from .generic import CUSTOM_PREFIX, CustomAgent, DeclaredAgent

if TYPE_CHECKING:
    from ww.extensions.registry import ExtensionRegistry

# In the order ww lists them, and tries them when a plan compiles for any agent.
BUILTIN_AGENTS: tuple[type[Agent], ...] = (Codex, ClaudeCode, Cursor)
BUILTIN_NAMES = frozenset(agent.key for agent in BUILTIN_AGENTS)
# The order init offers, and the help names, the built-ins' hooks in.
_HOOK_ORDER: tuple[type[Agent], ...] = (ClaudeCode, Codex, Cursor)
EXTENSION_PREFIX = "ext/"


class AgentRegistry:
    """The agents one project can address by name."""

    def __init__(
        self,
        declared: tuple[DeclaredAgent, ...] | None = (),
        extensions: ExtensionRegistry | None = None,
    ) -> None:
        """``declared`` ``None`` reads the declarations from ``extensions``'
        settings when a name first needs them."""
        self._builtins = {agent.key: agent for agent in BUILTIN_AGENTS}
        self._declared_agents = declared
        self._extensions = extensions

    @classmethod
    def of(cls, extensions: ExtensionRegistry) -> AgentRegistry:
        """The agents of the project whose extensions and settings are given."""
        return cls(None, extensions)

    @property
    def _declared(self) -> dict[str, DeclaredAgent]:
        declared = self._declared_agents
        if declared is None:
            declared = self._extensions.config.agents if self._extensions else ()
            self._declared_agents = declared
        return {agent.name: agent for agent in declared}

    def resolve(self, name: str) -> Agent:
        """The agent ``name`` addresses; an unknown name is a configuration error."""
        builtin = self._builtins.get(name)
        if builtin is not None:
            return builtin()
        declared = self._declared.get(name)
        if declared is not None:
            return declared
        if name.startswith(EXTENSION_PREFIX):
            if self._extensions is None:
                raise ConfigurationError(
                    f"agent {name!r} needs the project's extensions"
                )
            return self._extensions.agent(name)(name)
        if name.startswith(CUSTOM_PREFIX):
            return CustomAgent(name)
        raise ConfigurationError(
            f"unsupported agent {name!r}; choose one of: "
            f"{', '.join(agent.name for agent in self.known())}, "
            f"or use {CUSTOM_PREFIX}<name>"
        )

    def known(self) -> tuple[Agent, ...]:
        """Every agent this project names: built-in, declared, then the agents
        of the extensions its root settings list."""
        extension_agents: tuple[Agent, ...] = ()
        if self._extensions is not None:
            extension_agents = tuple(
                agent(name) for name, agent in self._extensions.configured_agents()
            )
        return (
            *(agent() for agent in BUILTIN_AGENTS),
            *self._declared.values(),
            *extension_agents,
        )


def hook_agents(agents: Iterable[Agent]) -> tuple[Agent, ...]:
    """The hook-capable ``agents``: the built-ins in hook order, then the rest."""
    capable = [agent for agent in agents if agent.hooks is not None]

    def rank(agent: Agent) -> int:
        kind = type(agent)
        return _HOOK_ORDER.index(kind) if kind in _HOOK_ORDER else len(_HOOK_ORDER)

    return tuple(sorted(capable, key=rank))


def builtin_agents() -> tuple[Agent, ...]:
    """The agents ww ships an adapter for, for places that read no settings."""
    return tuple(agent() for agent in BUILTIN_AGENTS)


def session_of(
    environ: Mapping[str, str], agents: Iterable[Agent] | None = None
) -> tuple[Agent, str] | None:
    """The agent whose session runs this process, and the session's ID.

    Each agent tells the commands it runs its session through its own
    environment variable; an agent without one, such as Cursor, is unknown.
    ``agents`` are the project's (``AgentRegistry.known()``); without them,
    the built-ins.
    """
    for agent in builtin_agents() if agents is None else agents:
        value = environ.get(agent.session_variable) if agent.session_variable else None
        if value:
            return agent, value
    return None
