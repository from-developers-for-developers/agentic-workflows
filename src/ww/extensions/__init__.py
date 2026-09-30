# SPDX-License-Identifier: GPL-3.0-or-later
"""Pluggable extensions: handlers, modes, and commands from other vendors."""

from ww.extensions.api import (
    EXTENSION_API_VERSION,
    Extension,
    ExtensionCheckResult,
    ExtensionCommand,
    ExtensionContext,
    ExtensionHandler,
    ExtensionNamespace,
    ExtensionResult,
    ExtensionVariable,
    RuleGroupContribution,
)
from ww.extensions.registry import (
    ExtensionReference,
    ExtensionRegistry,
    is_extension_reference,
    parse_reference,
)
from ww.extensions.store import ExtensionStore

__all__ = [
    "EXTENSION_API_VERSION",
    "Extension",
    "ExtensionCommand",
    "ExtensionContext",
    "ExtensionCheckResult",
    "ExtensionHandler",
    "ExtensionNamespace",
    "ExtensionReference",
    "ExtensionRegistry",
    "ExtensionResult",
    "ExtensionVariable",
    "ExtensionStore",
    "RuleGroupContribution",
    "is_extension_reference",
    "parse_reference",
]
