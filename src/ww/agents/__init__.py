# SPDX-License-Identifier: GPL-3.0-or-later
"""Everything ww knows about particular coding agents.

Each supported agent is an :class:`Agent` adapter; the
:class:`AgentRegistry` resolves the name a command or a step uses to one.
No other module keys a table by an agent's name.
"""

from .base import Agent
from .claude_code import ClaudeCode
from .codex import Codex
from .cursor import Cursor
from .generic import CUSTOM_PREFIX, CustomAgent, DeclaredAgent
from .hooks import (
    HOOK_EVENTS,
    FlatHooks,
    HookEvent,
    HookPayload,
    HookProtocol,
    Registration,
)
from .interaction import (
    FOREGROUND,
    PLAIN_TEXT,
    WAIT_VARIABLE,
    ChoiceMechanism,
    WaitMechanism,
    question_tool,
)
from .launch import Launcher, LaunchRequest
from .layout import (
    SHARED_DIRECTORY,
    SHARED_INSTRUCTION_FILE,
    AgentDiscovery,
    AvailableActions,
    Layout,
    instruction_files,
)
from .permissions import Permissions
from .registry import BUILTIN_AGENTS, AgentRegistry, builtin_agents, hook_agents

__all__ = [
    "BUILTIN_AGENTS",
    "CUSTOM_PREFIX",
    "FOREGROUND",
    "HOOK_EVENTS",
    "PLAIN_TEXT",
    "SHARED_DIRECTORY",
    "SHARED_INSTRUCTION_FILE",
    "WAIT_VARIABLE",
    "Agent",
    "AgentDiscovery",
    "AgentRegistry",
    "AvailableActions",
    "ChoiceMechanism",
    "ClaudeCode",
    "Codex",
    "Cursor",
    "CustomAgent",
    "DeclaredAgent",
    "FlatHooks",
    "HookEvent",
    "HookPayload",
    "HookProtocol",
    "LaunchRequest",
    "Launcher",
    "Layout",
    "Permissions",
    "Registration",
    "WaitMechanism",
    "builtin_agents",
    "hook_agents",
    "instruction_files",
    "question_tool",
]
