# SPDX-License-Identifier: GPL-3.0-or-later
"""``init`` can allow ww's role commands in Claude Code's local settings."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ww.claude_permissions import ROLE_COMMANDS, SETTINGS_FILE
from ww.cli import main

LOCAL = SETTINGS_FILE


def _init(root: Path, *flags: str) -> int:
    return main(["--root", str(root), "init", "--no-input", "--no-hooks", *flags])


def _allow(root: Path) -> list[str]:
    return json.loads((root / LOCAL).read_text())["permissions"]["allow"]


def _expected(root: Path) -> list[str]:
    wrapper = root.resolve() / "ww"
    return [f"Bash({wrapper} {command})" for command in ROLE_COMMANDS]


def test_the_rules_are_created_with_the_absolute_wrapper_path(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()

    assert _init(tmp_path, "--permissions") == 0

    allow = _allow(tmp_path)
    assert allow == _expected(tmp_path)
    assert f"Bash({tmp_path.resolve()}/ww instruction *)" in allow
    assert f"Bash({tmp_path.resolve()}/ww discover*)" in allow
    assert not any(rule.startswith("Bash(ww ") for rule in allow)
    assert LOCAL in (tmp_path / ".gitignore").read_text().splitlines()


def test_the_rules_merge_into_an_existing_file_without_clobbering_it(
    tmp_path: Path,
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / LOCAL).write_text(
        json.dumps(
            {
                "model": "opus",
                "permissions": {"allow": ["Bash(ls *)"], "deny": ["Bash(rm *)"]},
                "env": {"A": "1"},
            }
        )
    )
    (tmp_path / ".gitignore").write_text("node_modules/")

    assert _init(tmp_path, "--permissions") == 0

    document = json.loads((tmp_path / LOCAL).read_text())
    assert document["model"] == "opus"
    assert document["env"] == {"A": "1"}
    assert document["permissions"]["deny"] == ["Bash(rm *)"]
    assert document["permissions"]["allow"] == ["Bash(ls *)", *_expected(tmp_path)]
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[0] == "node_modules/"
    assert lines.count(LOCAL) == 1
    # A second init changes nothing.
    before = (tmp_path / LOCAL).read_text()
    assert _init(tmp_path, "--permissions") == 0
    assert (tmp_path / LOCAL).read_text() == before


def test_an_ignored_file_is_not_added_to_gitignore_again(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".gitignore").write_text(f"{LOCAL}\n")

    assert _init(tmp_path, "--permissions") == 0

    assert (tmp_path / ".gitignore").read_text().splitlines().count(LOCAL) == 1


def test_a_git_checkout_that_already_ignores_the_file_is_left_alone(
    tmp_path: Path,
) -> None:
    (tmp_path / ".claude").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".git" / "info").mkdir(exist_ok=True)
    (tmp_path / ".git" / "info" / "exclude").write_text(".claude/\n")

    assert _init(tmp_path, "--permissions") == 0

    assert (
        not (tmp_path / ".gitignore").exists()
        or LOCAL not in (tmp_path / ".gitignore").read_text()
    )


def test_nothing_is_written_unless_asked_for(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()

    assert _init(tmp_path) == 0

    assert not (tmp_path / LOCAL).exists()
    assert _init(tmp_path, "--no-permissions") == 0
    assert not (tmp_path / LOCAL).exists()


def test_a_project_without_claude_code_is_not_offered_the_rules(
    tmp_path: Path,
) -> None:
    assert _init(tmp_path, "--permissions") == 0

    assert not (tmp_path / LOCAL).exists()


def _ask(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    asked: list[str] = []
    replies = iter(answers)

    def reply(prompt: str = "") -> str:
        asked.append(prompt)
        return next(replies)

    tty = type("Tty", (), {"isatty": lambda self: True})
    monkeypatch.setattr("ww.cli.initialization.sys.stdin", tty())
    monkeypatch.setattr(
        sys.modules["ww.cli.main"],
        "_initialization_options",
        lambda storage, args: (
            "workflows:\n  - task: Task.\n    steps:\n      - work: Work.\n",
            "{}\n",
            True,
            (),
        ),
    )
    monkeypatch.setattr("builtins.input", reply)
    return asked


@pytest.mark.parametrize("answer", ["", "n"])
def test_the_question_defaults_to_no_and_a_no_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    (tmp_path / ".claude").mkdir()
    asked = _ask(monkeypatch, [answer])

    assert main(["--root", str(tmp_path), "init", "--no-hooks"]) == 0

    assert any("role commands" in prompt and "[y/N]" in prompt for prompt in asked)
    assert not (tmp_path / LOCAL).exists()
    choices = json.loads((tmp_path / ".ww/init-choices.json").read_text())
    assert choices["permissions"] is False


def test_a_yes_writes_the_rules_and_is_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".claude").mkdir()
    _ask(monkeypatch, ["y"])

    assert main(["--root", str(tmp_path), "init", "--no-hooks"]) == 0
    assert _allow(tmp_path) == _expected(tmp_path)

    # Asked again nowhere: the remembered answer refreshes the rules.
    asked = _ask(monkeypatch, [])
    (tmp_path / LOCAL).unlink()
    assert main(["--root", str(tmp_path), "init", "--no-hooks"]) == 0
    assert asked == []
    assert _allow(tmp_path) == _expected(tmp_path)


def test_an_unreadable_settings_file_is_never_overwritten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / LOCAL).write_text("{not json")

    assert _init(tmp_path, "--permissions") == 0

    assert (tmp_path / LOCAL).read_text() == "{not json"
    assert "by hand" in capsys.readouterr().out
