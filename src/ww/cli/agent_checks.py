# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww agents --check``: whether each agent can be run headless here."""

from __future__ import annotations

from collections.abc import Sequence

from ww.agents import (
    Agent,
    AgentRegistry,
    CatalogCache,
    HeadlessLauncher,
    Readiness,
)
from ww.extensions import ExtensionRegistry
from ww.storage import Storage

# The models a report names before it says how many more there are.
_SHOWN_MODELS = 12


def catalog_cache(storage: Storage) -> CatalogCache:
    """Where model catalogs are kept between checks."""
    return CatalogCache(storage.runtime_path / "agents" / "catalogs")


def render_agent_checks(
    storage: Storage, extensions: ExtensionRegistry, names: Sequence[str]
) -> tuple[str, bool]:
    """The report for ``names`` (every agent that runs headless when empty),
    and whether none of them has an error."""
    registry = AgentRegistry.of(extensions)
    agents = (
        [registry.resolve(name) for name in names]
        if names
        else [agent for agent in registry.known() if agent.launcher("headless")]
    )
    cache = catalog_cache(storage)
    sections = []
    ok = True
    for agent in agents:
        launcher = agent.launcher("headless")
        if not isinstance(launcher, HeadlessLauncher):
            sections.append(
                f"## {agent.title} (`{agent.name}`)\n\n"
                "- Error: ww cannot run this agent headless.\n"
            )
            ok = False
            continue
        readiness = launcher.probe(
            extensions.config.headless_settings(agent.name), cache=cache
        )
        ok = ok and readiness.ready
        sections.append(_section(agent, launcher, readiness))
    if not sections:
        return "# Headless agents\n\nNo agent here can be run headless.\n", True
    return "# Headless agents\n\n" + "\n".join(sections), ok


def _section(agent: Agent, launcher: HeadlessLauncher, readiness: Readiness) -> str:
    lines = [
        f"## {agent.title} (`{agent.name}`)",
        "",
        f"- Binary: {readiness.binary or 'not found'}",
    ]
    if readiness.binary is not None:
        lines += [
            f"- Version: {readiness.version or 'unknown'}",
            f"- Login: {_login(readiness.logged_in)}",
            f"- Models: {_models(launcher, readiness)}",
            f"- Reasoning: {_reasoning(launcher)}",
        ]
    lines += [
        f"- {finding.level.capitalize()}: {finding.message}"
        for finding in readiness.findings
    ]
    return "\n".join(lines) + "\n"


def _login(logged_in: bool | None) -> str:
    return {True: "logged in", False: "not logged in", None: "unknown"}[logged_in]


def _models(launcher: HeadlessLauncher, readiness: Readiness) -> str:
    catalog = readiness.catalog
    if catalog is None:
        if launcher.lists_models:
            return "not listed"
        return "no model list; " + launcher.models_description()
    names = sorted(catalog.models)
    shown = []
    for name in names[:_SHOWN_MODELS]:
        levels = catalog.models[name]
        shown.append(f"`{name}` ({', '.join(levels)})" if levels else f"`{name}`")
    more = len(names) - len(shown)
    return ", ".join(shown) + (f", and {more} more" if more > 0 else "")


def _reasoning(launcher: HeadlessLauncher) -> str:
    levels = launcher.reasoning_levels
    if levels is None:
        return "none; choose a model variant instead"
    return ", ".join(levels)
