# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww setup apply``: ww places a proposed configuration fragment itself."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

from ww.cli import main
from ww.config import load_configuration
from ww.extensions import ExtensionRegistry

REPO = """# The project's own workflows.
workflows:
  - name: task
    description: Implement a change.
    steps:
      - work: Work.
"""
FRAGMENT = """workflows:
  - name: review
    description: Review a change.
    modes: [gently]
    steps:
      - read: Read the change.
modes:
  - gently: Say it kindly.
settings:
  runtime: auto
  extensions:
    ww/git:
      separate_branch: true
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "ww.yaml").write_text(REPO, encoding="utf-8")
    (project / "ww.json").write_text(
        json.dumps({"limits": {"rounds": 3}}, indent=2) + "\n", encoding="utf-8"
    )
    return project


def _fragment(tmp_path: Path, text: str, name: str = "proposal.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _apply(root: Path, *arguments: str) -> int:
    return main(["--root", str(root), "setup", "apply", *arguments])


def _workflows(root: Path) -> list[str]:
    configuration = load_configuration(
        root / "ww.yaml", ExtensionRegistry.discover(root)
    )
    return [workflow.name for workflow in configuration.workflows]


def _snapshot(root: Path) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(root.iterdir())
        if path.is_file()
    }


def test_for_team_writes_the_shared_import_and_settings(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fragment = _fragment(tmp_path, FRAGMENT)

    assert _apply(root, str(fragment), "--for", "team", "--yes") == 0

    captured = capsys.readouterr()
    assert "ww-setup.yaml (new): adds workflow `review`, mode `gently`" in captured.err
    assert "ww.yaml: adds ww-setup.yaml to imports" in captured.err
    assert (
        "ww.json: sets runtime, extensions" in captured.err
    )
    assert captured.out.startswith("Applied.\n")
    repo = (root / "ww.yaml").read_text(encoding="utf-8")
    assert repo == "# The project's own workflows.\nimports:\n  - ww-setup.yaml\n" + (
        REPO.split("\n", 1)[1]
    )
    setup = yaml.safe_load((root / "ww-setup.yaml").read_text(encoding="utf-8"))
    assert set(setup) == {"workflows", "modes"}
    assert json.loads((root / "ww.json").read_text()) == {
        "limits": {"rounds": 3},
        "runtime": "auto",
        "extensions": {"ww/git": {"separate_branch": True}},
    }
    assert _workflows(root)[:2] == ["review", "task"]


def test_for_me_writes_local_files_and_creates_the_local_root(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fragment = _fragment(
        tmp_path, FRAGMENT.replace("runtime: auto", "limits:\n    fixes: 5")
    )

    assert _apply(root, str(fragment), "--for", "me", "--yes") == 0

    capsys.readouterr()
    assert (root / "ww.local.yaml").read_text() == (
        "imports:\n  - ww-setup.local.yaml\n"
    )
    assert "review" in (root / "ww-setup.local.yaml").read_text()
    assert json.loads((root / "ww.local.json").read_text()) == {
        "limits": {"fixes": 5},
        "extensions": {"ww/git": {"separate_branch": True}},
    }
    # The shared files are untouched.
    assert (root / "ww.yaml").read_text() == REPO
    assert not (root / "ww-setup.yaml").exists()
    assert "review" in _workflows(root)


def test_for_me_adds_the_import_to_an_existing_local_file(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "ww.local.yaml").write_text(
        "modes:\n  - economy: Short.\n", encoding="utf-8"
    )
    fragment = _fragment(tmp_path, "modes:\n  - gently: Kindly.\n")

    assert _apply(root, str(fragment), "--for", "me", "--yes") == 0

    capsys.readouterr()
    assert (root / "ww.local.yaml").read_text() == (
        "imports:\n  - ww-setup.local.yaml\nmodes:\n  - economy: Short.\n"
    )


def test_a_second_apply_merges_by_name(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "rules").mkdir()
    (root / "rules/short.md").write_text("Keep functions short.\n", encoding="utf-8")
    (root / "rules/named.md").write_text("Name things well.\n", encoding="utf-8")
    first = _fragment(
        tmp_path,
        FRAGMENT.split("settings:")[0]
        + "hooks:\n  after_complete:\n    - argv: ['true']\n"
        + "rules:\n  style: [rules/short.md]\n",
    )
    assert _apply(root, str(first), "--for", "team", "--yes") == 0
    second = _fragment(
        tmp_path,
        "workflows:\n  - name: review\n    description: Review again.\n"
        "    steps:\n      - read: Read.\n"
        "hooks:\n  after_complete:\n    - argv: ['echo', 'done']\n"
        "rules:\n  style: [rules/named.md]\n",
        "second.yaml",
    )

    assert _apply(root, str(second), "--for", "team", "--yes") == 0

    err = capsys.readouterr().err
    assert (
        "ww-setup.yaml: replaces workflow `review`, rule group `style`; appends 1 "
        "hook to after_complete"
    ) in err
    setup = yaml.safe_load((root / "ww-setup.yaml").read_text(encoding="utf-8"))
    assert setup["workflows"] == [
        {
            "name": "review",
            "description": "Review again.",
            "steps": [{"read": "Read."}],
        }
    ]
    assert setup["modes"] == [{"gently": "Say it kindly."}]
    assert setup["hooks"]["after_complete"] == [
        {"argv": ["true"]},
        {"argv": ["echo", "done"]},
    ]
    assert setup["rules"] == {"style": ["rules/named.md"]}
    # The repo file lists the import once.
    repo = yaml.safe_load((root / "ww.yaml").read_text())
    assert repo["imports"] == ["ww-setup.yaml"]


def test_a_setting_with_another_value_refuses_the_whole_apply(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _snapshot(root)
    fragment = _fragment(tmp_path, FRAGMENT + "  limits:\n    rounds: 5\n")

    assert _apply(root, str(fragment), "--for", "team", "--yes") == 1

    err = capsys.readouterr().err
    assert "does not overwrite them" in err
    assert "- limits.rounds is 3, the fragment proposes 5" in err
    assert _snapshot(root) == before


def test_a_fragment_that_would_not_load_restores_every_file(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _snapshot(root)
    fragment = _fragment(tmp_path, FRAGMENT.replace("modes: [gently]", "modes: [x]"))

    assert _apply(root, str(fragment), "--for", "team", "--yes") == 1

    err = capsys.readouterr().err
    assert "refused: the configuration would not be valid" in err
    assert "unknown mode(s): x" in err
    assert _snapshot(root) == before


def test_dry_run_shows_the_change_and_writes_nothing(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _snapshot(root)
    fragment = _fragment(tmp_path, FRAGMENT)

    assert _apply(root, str(fragment), "--for", "team", "--dry-run") == 0

    out = capsys.readouterr().out
    assert "`ww setup apply --for team` writes for the team" in out
    assert "Dry run: the configuration would be valid; nothing was written." in out
    assert _snapshot(root) == before

    assert _apply(root, str(fragment), "--for", "me", "--dry-run", "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["for"] == "me"
    assert report["applied"] is False
    assert [entry["path"] for entry in report["files"]] == [
        "ww-setup.local.yaml",
        "ww.local.yaml",
        "ww.local.json",
    ]
    assert _snapshot(root) == before


def test_without_a_terminal_it_needs_yes(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    before = _snapshot(root)

    assert _apply(root, str(_fragment(tmp_path, FRAGMENT)), "--for", "team") == 1

    err = capsys.readouterr().err
    assert "there is no terminal to ask at" in err
    assert _snapshot(root) == before


def test_the_json_report_after_applying(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fragment = _fragment(tmp_path, "modes:\n  - gently: Kindly.\n")

    assert _apply(root, str(fragment), "--for", "team", "--yes", "--json") == 0

    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is True
    assert report["files"][0] == {
        "path": "ww-setup.yaml",
        "created": True,
        "changes": ["adds mode `gently`"],
    }


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("imports: [a.yaml]\n", "unknown key(s): imports"),
        ("workflows: {task: {}}\n", "workflows must be a list"),
        ("workflows:\n  - 42\n", "workflows[0] needs a name"),
        ("settings: [1]\n", "settings must be a mapping"),
        ("[]\n", "must be a mapping of configuration keys"),
        ("settings:\n  limits:\n    rounds: 3\n", "changes nothing"),
    ],
)
def test_a_malformed_fragment_is_refused(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
    message: str,
) -> None:
    before = _snapshot(root)

    assert _apply(root, str(_fragment(tmp_path, text)), "--for", "team", "--yes") == 1

    assert message in capsys.readouterr().err
    assert _snapshot(root) == before


def test_an_unimported_setup_file_is_left_to_the_operator(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "ww-setup.yaml").write_text("modes: []\n", encoding="utf-8")
    fragment = _fragment(tmp_path, FRAGMENT)

    assert _apply(root, str(fragment), "--for", "team", "--yes") == 1

    assert "exists but ww.yaml does not import it" in (
        capsys.readouterr().err
    )


def test_a_definition_the_root_file_keeps_is_flagged(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fragment = _fragment(
        tmp_path,
        "workflows:\n  - name: task\n    description: Theirs.\n"
        "    steps:\n      - work: Work.\n",
    )

    assert _apply(root, str(fragment), "--for", "team", "--dry-run") == 0

    assert (
        "Warning: workflow `task` is also defined in ww.yaml, "
        "which takes precedence over ww-setup.yaml"
    ) in capsys.readouterr().out


def _identity(root: Path) -> dict[str, tuple[int, int]]:
    """Each file's inode and modification time: what any write would change."""
    return {
        path.name: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in sorted(root.iterdir())
        if path.is_file()
    }


def test_validating_never_touches_the_project(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _identity(root)
    fragment = _fragment(tmp_path, FRAGMENT)

    assert _apply(root, str(fragment), "--for", "team", "--dry-run") == 0
    assert _identity(root) == before

    # Refused for want of a terminal: still nothing touched.
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    assert _apply(root, str(fragment), "--for", "team") == 1
    capsys.readouterr()
    assert _identity(root) == before
    assert not (root / "ww-setup.yaml").exists()


def test_writes_go_through_symbolic_links_and_keep_the_mode(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    target = shared / "settings.json"
    (root / "ww.json").replace(target)
    target.chmod(0o640)
    (root / "ww.json").symlink_to(target)

    fragment = _fragment(tmp_path, FRAGMENT)
    assert _apply(root, str(fragment), "--for", "team", "--yes") == 0

    capsys.readouterr()
    link = root / "ww.json"
    assert link.is_symlink()
    assert json.loads(target.read_text())["runtime"] == "auto"
    assert target.stat().st_mode & 0o777 == 0o640


def test_a_hook_already_in_place_is_not_added_again(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hooks = "hooks:\n  after_complete:\n    - argv: ['true']\n"
    fragment = _fragment(tmp_path, "modes:\n  - gently: Kindly.\n" + hooks)
    assert _apply(root, str(fragment), "--for", "team", "--yes") == 0
    capsys.readouterr()

    # The same fragment again changes nothing.
    assert _apply(root, str(fragment), "--for", "team", "--yes") == 1
    assert "changes nothing" in capsys.readouterr().err

    mixed = _fragment(
        tmp_path,
        "modes:\n  - brief: Short.\n" + hooks + "    - argv: ['echo', 'done']\n",
        "mixed.yaml",
    )
    assert _apply(root, str(mixed), "--for", "team", "--yes") == 0

    err = capsys.readouterr().err
    assert (
        "ww-setup.yaml: adds mode `brief`; appends 1 hook to after_complete; "
        "skips 1 hook already in after_complete"
    ) in err
    setup = yaml.safe_load((root / "ww-setup.yaml").read_text(encoding="utf-8"))
    assert setup["hooks"]["after_complete"] == [
        {"argv": ["true"]},
        {"argv": ["echo", "done"]},
    ]


def test_a_failed_write_puts_every_file_back_and_leaves_no_temporary_file(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(root)
    replace = Path.replace

    def failing_replace(self: Path, target: Path) -> Path:
        if self.name.endswith(".json.ww-tmp"):
            raise PermissionError(13, "Permission denied", str(target))
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)

    fragment = _fragment(tmp_path, FRAGMENT)
    assert _apply(root, str(fragment), "--for", "team", "--yes") == 1

    err = capsys.readouterr().err
    assert "cannot write" in err
    assert "nothing was written" in err
    assert "Traceback" not in err
    assert _snapshot(root) == before
    assert not [path.name for path in root.iterdir() if path.name.endswith(".ww-tmp")]
