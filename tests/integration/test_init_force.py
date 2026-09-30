# SPDX-License-Identifier: GPL-3.0-or-later
"""``init --force``, the concrete agent permissions, and init's next step."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ww.cli import main


def _git_project(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _interactive(monkeypatch: pytest.MonkeyPatch, answers: dict[str, str]) -> list[str]:
    """Answer each init question by what it asks, and record the questions."""
    prompts: list[str] = []

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        for fragment, reply in answers.items():
            if fragment in prompt:
                return reply
        return ""

    tty = type("Tty", (), {"isatty": lambda self: True})
    monkeypatch.setattr("ww.cli.initialization.sys.stdin", tty())
    monkeypatch.setattr("builtins.input", answer)
    return prompts


def _choices(root: Path) -> dict[str, object]:
    return json.loads((root / ".ww/init-choices.json").read_text(encoding="utf-8"))


def test_force_asks_again_and_only_adds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _git_project(tmp_path)
    (root / ".claude").mkdir()
    no = {
        "Use Git worktrees": "n",
        ".gitignore": "n",
        "@WW_AGENT_INSTRUCTIONS.md": "n",
        ".claude/skills?": "n",
        "Create which?": "none",
        "Install ww hooks": "n",
    }
    _interactive(monkeypatch, no)
    assert main(["--root", str(root), "init"]) == 0
    capsys.readouterr()
    assert not (root / ".claude/skills").exists()
    assert not (root / ".claude/settings.json").exists()
    assert not (root / "AGENTS.md").exists()

    # Without --force every remembered answer stands: nothing is asked again.
    prompts = _interactive(monkeypatch, {})
    assert main(["--root", str(root), "init"]) == 0
    capsys.readouterr()
    assert not any(".claude/skills?" in prompt for prompt in prompts)

    yes = {"Use Git worktrees": "n", "Create which?": "codex"}
    prompts = _interactive(monkeypatch, yes)
    assert main(["--root", str(root), "init", "--force"]) == 0
    output = capsys.readouterr().out

    asked = "\n".join(prompts)
    # What the settings files already say is configuration, not a remembered
    # answer, and init never rewrites it.
    assert "Use ww" not in asked
    assert "Task ID format" not in asked
    for question in (
        ".gitignore",
        "@WW_AGENT_INSTRUCTIONS.md",
        ".claude/skills?",
        "Create which?",
        "Install ww hooks for claudecode",
    ):
        assert question in asked
    # One more agent was added, with every skill, and the other answers apply.
    for directory in (".claude", ".codex"):
        for skill in ("ww", "noww", "ww-rule"):
            assert (root / directory / "skills" / skill / "SKILL.md").is_file()
    assert "@WW_AGENT_INSTRUCTIONS.md" in (root / "AGENTS.md").read_text()
    assert ".ww/*" in (root / ".gitignore").read_text().splitlines()
    assert (root / ".claude/settings.json").is_file()
    assert "Allow ww to run without confirmation" in output
    choices = _choices(root)
    assert choices["agents"][".claude"] is True  # type: ignore[index]
    assert choices["agents"][".codex"] is True  # type: ignore[index]
    assert choices["update_gitignore"] is True
    assert choices["link_instructions"] is True
    assert choices["hooks"]["claudecode"] is True  # type: ignore[index]

    # Forcing again, now declining everything, removes and duplicates nothing.
    before = {
        path: (root / path).read_text(encoding="utf-8")
        for path in (
            ".gitignore",
            "AGENTS.md",
            ".claude/settings.json",
            "ww-agentic-workflows.json",
            ".claude/skills/noww/SKILL.md",
        )
    }
    _interactive(monkeypatch, no)
    assert main(["--root", str(root), "init", "--force"]) == 0
    capsys.readouterr()
    for path, content in before.items():
        assert (root / path).read_text(encoding="utf-8") == content, path
    assert (root / ".codex/skills/ww/SKILL.md").is_file()


def test_force_without_input_reapplies_the_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _git_project(tmp_path)
    (root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    init = ["--root", str(root), "init", "--no-input"]

    assert main([*init, "--no-update-gitignore"]) == 0
    capsys.readouterr()
    assert ".ww/*" not in (root / ".gitignore").read_text().splitlines()
    assert _choices(root)["update_gitignore"] is False

    assert main([*init, "--force"]) == 0
    output = capsys.readouterr().out

    lines = (root / ".gitignore").read_text().splitlines()
    assert lines.count(".ww/*") == 1
    assert lines.count("!.ww/team.md") == 1
    assert lines.count("node_modules/") == 1
    assert _choices(root)["update_gitignore"] is True
    # The notice was shown by the first run, and --force shows it again.
    assert "Allow ww to run without confirmation" in output

    assert main([*init, "--force"]) == 0
    capsys.readouterr()
    assert (root / ".gitignore").read_text().splitlines() == lines


def test_the_notice_names_each_agents_file_and_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / "ww-agentic-workflows.json").write_text(
        '{"executable": "ww-agentic-workflows-dev"}\n', encoding="utf-8"
    )

    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    output = capsys.readouterr().out

    assert "ACTION NEEDED" not in output
    assert "claudecode: merge these entries into .claude/settings.json" in output
    snippet = output.split(".claude/settings.json\n\n", 1)[1].split("\n\n", 1)[0]
    assert json.loads(snippet) == {
        "permissions": {
            "allow": [
                "Bash(ww-agentic-workflows-dev *)",
                "Bash(./ww *)",
                "Bash(ww *)",
            ]
        }
    }
    assert (
        "  codex: allow every command starting with:\n\n"
        "     ww-agentic-workflows-dev\n"
        "     ./ww\n"
        "     ww   (when the shortcut exists)\n"
    ) in output
    # The trust note stays, naming the user and local levels.
    assert "ww-agentic-workflows.local.yaml here" in output
    assert "user configuration directory" in output


def test_without_a_known_agent_the_notice_names_the_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    output = capsys.readouterr().out

    assert "merge these entries" not in output
    assert "In your agent, allow every command starting with:" in output
    assert "     ./ww\n" in output


def test_json_output_names_the_next_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--root", str(tmp_path), "init", "--no-input", "--json"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["next_steps"] == [
        "Run the ww-setup skill to set ww up for you, your team and this project."
    ]
