# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww features``, the repo-only ``feature_maps`` setting, and init's question."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ww.cli import main
from ww.config_files import FEATURE_MAPS_ATTRIBUTES_LINE, FEATURE_MAPS_IGNORE_LINE

AGENTS = """name: agents
summary: Agent adapters behind one registry.
aliases: [adapters]
parts:
  - src/registry.py#Registry
docs:
  - docs/guide.md#Agents
"""


def _project(root: Path, *, enabled: bool = True) -> Path:
    root.mkdir(exist_ok=True)
    (root / "ww.json").write_text(
        json.dumps({"feature_maps": enabled}), encoding="utf-8"
    )
    (root / "src").mkdir()
    (root / "src" / "registry.py").write_text("class Registry: ...\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("# Guide\n\n## Agents\n", encoding="utf-8")
    features = root / ".ww" / "features"
    features.mkdir(parents=True)
    (features / "agents.yaml").write_text(AGENTS, encoding="utf-8")
    return root


def _run(root: Path, *args: str) -> int:
    return main(["--root", str(root), *args])


def test_the_index_lists_each_map_with_its_summary_and_aliases(
    tmp_path: Path, capsys
) -> None:
    root = _project(tmp_path)

    assert _run(root, "features") == 0
    out = capsys.readouterr().out
    assert "- `agents` — Agent adapters behind one registry. (also: adapters)" in out

    assert _run(root, "features", "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["maps"] == [
        {
            "name": "agents",
            "summary": "Agent adapters behind one registry.",
            "aliases": ["adapters"],
        }
    ]


def test_show_prints_the_map_and_check_passes_while_every_pointer_resolves(
    tmp_path: Path, capsys
) -> None:
    root = _project(tmp_path)

    assert _run(root, "features", "show", "agents") == 0
    shown = capsys.readouterr().out
    assert "- `src/registry.py#Registry`" in shown
    assert "No longer resolving" not in shown

    assert _run(root, "features", "check") == 0
    assert "1 feature map(s), every pointer resolves." in capsys.readouterr().out


def test_a_pointer_that_no_longer_resolves_is_shown_and_fails_the_check(
    tmp_path: Path, capsys
) -> None:
    root = _project(tmp_path)
    (root / "src" / "registry.py").write_text("class Agents: ...\n", encoding="utf-8")

    assert _run(root, "features", "show", "agents") == 0
    assert (
        "- parts[0] src/registry.py#Registry: does not contain 'Registry'"
        in capsys.readouterr().out
    )

    assert _run(root, "features", "check") == 1
    out = capsys.readouterr().out
    assert out.startswith("1 problem(s) in 1 feature map(s):")
    assert ".ww/features/agents.yaml: parts[0] src/registry.py#Registry" in out

    assert _run(root, "features", "check", "--json") == 1
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert [problem["map"] for problem in problems] == ["agents"]


def test_show_names_the_maps_when_one_is_unknown(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)

    assert _run(root, "features", "show", "hooks") == 1
    assert "no feature map 'hooks'; maps: agents" in capsys.readouterr().err


def test_with_feature_maps_off_the_commands_say_so_and_the_template_still_prints(
    tmp_path: Path, capsys
) -> None:
    root = _project(tmp_path, enabled=False)
    (root / ".ww" / "features" / "agents.yaml").write_text("broken: [", "utf-8")

    assert _run(root, "features", "check") == 0
    assert "Feature maps are off in this project" in capsys.readouterr().out
    assert _run(root, "features", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": False}
    assert _run(root, "features", "template") == 0
    assert "name: example" in capsys.readouterr().out


@pytest.mark.parametrize("level", ["local", "user"])
def test_feature_maps_is_refused_outside_the_repo_file(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    root = _project(tmp_path / "project")
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(user))
    target = root / "ww.local.json" if level == "local" else user / "ww.json"
    target.write_text('{"feature_maps": false}', encoding="utf-8")

    assert _run(root, "features") == 1
    err = capsys.readouterr().err
    assert "sets feature_maps, which applies to everyone working on the project" in err
    assert "set it in ww.json instead" in err


def _git(root: Path) -> Path:
    root.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _answer(monkeypatch: pytest.MonkeyPatch, feature_maps: str) -> list[str]:
    asked: list[str] = []

    def answer(prompt: str) -> str:
        asked.append(prompt)
        return feature_maps if "feature maps" in prompt else ""

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    return asked


def test_init_keeps_feature_maps_on_a_yes_and_commits_their_directory(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _git(tmp_path / "project")
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(tmp_path / "user"))
    asked = _answer(monkeypatch, "y")

    assert _run(root, "init", "--no-hooks") == 0
    capsys.readouterr()

    assert any("Keep feature maps for this project? [y/N]" in p for p in asked)
    assert json.loads((root / "ww.json").read_text("utf-8"))["feature_maps"] is True
    ignore = (root / ".gitignore").read_text("utf-8").splitlines()
    assert ignore.index(FEATURE_MAPS_IGNORE_LINE) > ignore.index(".ww/*")
    attributes = (root / ".gitattributes").read_text("utf-8").splitlines()
    assert attributes == [FEATURE_MAPS_ATTRIBUTES_LINE]

    # The repo file's answer stands: a second init neither asks nor duplicates.
    asked.clear()
    assert _run(root, "init", "--no-hooks") == 0
    assert not any("feature maps" in prompt for prompt in asked)
    assert (root / ".gitattributes").read_text("utf-8").splitlines() == attributes


def test_init_leaves_feature_maps_off_on_a_no(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _git(tmp_path / "project")
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(tmp_path / "user"))
    _answer(monkeypatch, "n")

    assert _run(root, "init", "--no-hooks") == 0
    capsys.readouterr()

    assert json.loads((root / "ww.json").read_text("utf-8"))["feature_maps"] is False
    assert FEATURE_MAPS_IGNORE_LINE not in (root / ".gitignore").read_text("utf-8")
    assert not (root / ".gitattributes").exists()


def test_init_without_input_leaves_feature_maps_off(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _git(tmp_path / "project")
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(tmp_path / "user"))

    assert _run(root, "init", "--no-input") == 0
    capsys.readouterr()

    assert json.loads((root / "ww.json").read_text("utf-8"))["feature_maps"] is False
    assert not (root / ".gitattributes").exists()
