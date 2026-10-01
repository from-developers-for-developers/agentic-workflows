# SPDX-License-Identifier: GPL-3.0-or-later
"""Discovering agents and the skills installed for them."""

from pathlib import Path

import pytest

from ww.discovery import AGENT_DIRECTORIES, AgentDiscovery, normalize_agent
from ww.errors import ConfigurationError


def test_discovers_a_codex_skill(tmp_path: Path) -> None:
    skill = tmp_path / ".codex/skills/ww"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# ww\n", encoding="utf-8")

    available = AgentDiscovery(tmp_path).available("codex")

    assert "ww" in available.skills


def test_normalizes_supported_and_custom_agents() -> None:
    assert list(AGENT_DIRECTORIES) == [
        "codex",
        "claudecode",
        "gemini",
        "antigravity",
        "deepseek",
        "kimi",
        "cursor",
        "grok",
    ]
    assert normalize_agent("codex") == "codex"
    assert normalize_agent("custom:blablabla") == "blablabla"

    with pytest.raises(ConfigurationError, match="custom agent name"):
        normalize_agent("custom:../outside")
    with pytest.raises(ConfigurationError, match="use custom"):
        normalize_agent("unknown")
