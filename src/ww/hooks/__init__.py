# SPDX-License-Identifier: GPL-3.0-or-later
"""Native agent hooks: the session-start context line, translated per agent
by the agent's hook protocol (:mod:`ww.agents.hooks`)."""

from .install import (
    HookInstallation,
    HookInstallError,
    hook_protocol,
    hook_snippet,
    hooks_file,
    hooks_installed,
    install_hooks,
    manual_instructions,
    registered_elsewhere,
    uninstall_hooks,
)
from .runtime import HookAnswer, answer_hook, is_project_root

__all__ = [
    "HookAnswer",
    "HookInstallError",
    "HookInstallation",
    "answer_hook",
    "hook_protocol",
    "hook_snippet",
    "hooks_file",
    "hooks_installed",
    "install_hooks",
    "is_project_root",
    "manual_instructions",
    "registered_elsewhere",
    "uninstall_hooks",
]
