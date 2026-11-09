# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolving agent names to adapters, and what each agent's layout finds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.agents import (
    PLAIN_TEXT,
    Agent,
    AgentDiscovery,
    AgentRegistry,
    ClaudeCode,
    Codex,
    CustomAgent,
    DeclaredAgent,
    Layout,
    builtin_agents,
    question_tool,
)
from ww.errors import ConfigurationError
from ww.extensions import Extension, ExtensionRegistry
from ww.project_config import ProjectConfig, load_project_config


class Aider(Agent):
    key = "aider"
    title = "Aider"
    layout = Layout(".aider", instruction_file="CONVENTIONS.md")
    choices = question_tool("ask", "A free-form answer is a comment, not a choice.")


def _extensions(tmp_path: Path, settings: dict[str, object]) -> ExtensionRegistry:
    (tmp_path / "ww.json").write_text(json.dumps(settings), encoding="utf-8")
    return ExtensionRegistry(
        tmp_path,
        (Extension(vendor="acme", name="aider", agents=(Aider,)),),
        config=load_project_config(tmp_path / "ww.json"),
    )


def test_ww_supports_claude_code_codex_and_cursor() -> None:
    assert [agent.name for agent in builtin_agents()] == [
        "codex",
        "claudecode",
        "cursor",
    ]
    assert [agent.name for agent in AgentRegistry().known()] == [
        "codex",
        "claudecode",
        "cursor",
    ]


@pytest.mark.parametrize("name", ["gemini", "antigravity", "kimi", "grok"])
def test_removed_agents_are_unknown_but_usable_as_custom(name: str) -> None:
    registry = AgentRegistry()

    with pytest.raises(ConfigurationError, match="or use custom:<name>"):
        registry.resolve(name)
    custom = registry.resolve(f"custom:{name}")
    assert custom.choices == PLAIN_TEXT
    assert custom.layout == Layout(f".{name}")


def test_a_custom_agent_never_becomes_the_built_in_of_the_same_name() -> None:
    custom = AgentRegistry().resolve("custom:claudecode")

    assert isinstance(custom, CustomAgent)
    assert custom.name == "custom:claudecode"
    assert custom.choices == PLAIN_TEXT
    assert custom.hooks is None
    assert isinstance(AgentRegistry().resolve("claudecode"), ClaudeCode)


def test_a_custom_agent_name_must_be_a_plain_name() -> None:
    with pytest.raises(ConfigurationError, match="custom agent name"):
        AgentRegistry().resolve("custom:../outside")


def test_codex_runs_skills_with_a_dollar_and_claude_code_with_a_slash() -> None:
    assert Codex().skill_command("ww-setup") == "$ww-setup"
    assert ClaudeCode().skill_command("ww", "fix it") == "/ww fix it"


def test_a_project_declares_an_agent_with_data(tmp_path: Path) -> None:
    extensions = _extensions(
        tmp_path,
        {
            "agents": {
                "aider": {
                    "title": "Aider",
                    "instruction_file": "CONVENTIONS.md",
                    "question_tool": "ask_user",
                }
            }
        },
    )
    registry = AgentRegistry.of(extensions)

    agent = registry.resolve("aider")

    assert isinstance(agent, DeclaredAgent)
    assert agent.title == "Aider"
    assert agent.layout == Layout(".aider", instruction_file="CONVENTIONS.md")
    assert "`ask_user` tool" in agent.choices.instruction
    assert [known.name for known in registry.known()][-1] == "aider"


@pytest.mark.parametrize(
    ("agents", "message"),
    [
        ({"codex": {"title": "Codex"}}, "built-in agent"),
        ({"-x": {}}, "invalid agent name"),
        ({"aider": {"hooks": "x"}}, "unknown key"),
        ({"aider": {"title": ""}}, "non-empty string"),
        ([], "must be an object"),
    ],
)
def test_declarations_are_validated(
    tmp_path: Path, agents: object, message: str
) -> None:
    (tmp_path / "ww.json").write_text(json.dumps({"agents": agents}), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_project_config(tmp_path / "ww.json")


def test_an_extension_agent_is_addressed_by_its_reference(tmp_path: Path) -> None:
    extensions = _extensions(tmp_path, {"extensions": {"acme/aider": {}}})
    registry = AgentRegistry.of(extensions)
    reference = "ext/acme/aider/agents:aider"

    agent = registry.resolve(reference)

    assert isinstance(agent, Aider)
    assert agent.name == reference
    assert reference in [known.name for known in registry.known()]
    with pytest.raises(ConfigurationError, match="has no agent 'other'"):
        registry.resolve("ext/acme/aider/agents:other")
    with pytest.raises(ConfigurationError, match="but a handler is required here"):
        extensions.handler(reference)


def test_an_extension_agent_must_be_an_agent_class() -> None:
    with pytest.raises(TypeError, match="Agent subclasses"):
        Extension(vendor="acme", name="aider", agents=(Aider(),))  # type: ignore[arg-type]


def test_a_layout_discovers_its_own_and_shared_skills(tmp_path: Path) -> None:
    for directory in (".codex/skills/ww", ".agents/skills/shared", ".claude/skills/c"):
        (tmp_path / directory).mkdir(parents=True)
        (tmp_path / directory / "SKILL.md").write_text("# s\n", encoding="utf-8")
    (tmp_path / ".codex/commands").mkdir()
    (tmp_path / ".codex/commands/review.md").write_text("Review.", encoding="utf-8")

    available = AgentDiscovery(tmp_path, Codex.layout).available()

    assert available.skills == {"ww", "shared"}
    assert available.slash_commands == {"review"}
    assert AgentDiscovery(tmp_path, None).available().skills == frozenset()


def test_project_config_declares_no_agents_by_default() -> None:
    assert ProjectConfig().agents == ()
