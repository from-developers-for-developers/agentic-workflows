# SPDX-License-Identifier: GPL-3.0-or-later
"""In a Git project tasks read the configuration committed on ``config_ref``.

Whatever the operator checks out or edits in the main checkout, a task keeps
reading the read-only snapshot of the commit it started with, until
``ww config refresh`` moves it on.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ww import reached_files
from ww.cli import main
from ww.config_files import checkout_directory, snapshot_of
from ww.config_snapshots import ConfigSnapshots, _make_read_only
from ww.errors import ConfigurationError, StateError
from ww.rule_conversion import require_committable
from ww.storage import Storage

TASK = "T1"
SETTINGS = {"config_ref": "main"}
# The rule's check records where it ran and the root it was given.
CONFIG = """imports: [ww-rules.yaml]
workflows:
  - name: task
    steps:
      - name: work
        description: Do the work.
        rules:
          - text: Lint it.
            argv: [python3, "{{ww.root}}/tools/lint.py"]
            files: [tools/lint.py]
"""
RULES = """rules:
  style: [rules/style]
"""
LINT = (
    "import os, pathlib\n"
    "root = os.environ['WW_ROOT']\n"
    "pathlib.Path(os.environ['RAN']).write_text(os.getcwd() + '\\n' + root)\n"
)


def _git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", message, cwd=root)
    return _git("rev-parse", "HEAD", cwd=root)


def _project(root: Path, settings: dict[str, object] | None = None) -> str:
    (root / "ww.yaml").write_text(CONFIG, encoding="utf-8")
    (root / "ww.json").write_text(json.dumps(settings or SETTINGS), encoding="utf-8")
    (root / "ww-rules.yaml").write_text(RULES, encoding="utf-8")
    (root / "rules/style").mkdir(parents=True)
    (root / "rules/style/naming.md").write_text("Name things well.\n", encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools/lint.py").write_text(LINT, encoding="utf-8")
    (root / "README.md").write_text("Not configuration.\n", encoding="utf-8")
    (root / ".gitignore").write_text(".ww/*\nwt/\nnode_modules/\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", ".", cwd=root)
    _git("config", "user.email", "t@e.st", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    return _commit(root, "seed")


def _start(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    arguments = ["--root", str(root), "start", TASK, "--workflow", "task"]
    arguments += [
        "--agent",
        "codex",
        "--requirements",
        "Do it.",
        "--description",
        "Do it",
    ]
    assert main(arguments) == 0
    capsys.readouterr()


def _pin(root: Path) -> str | None:
    state = Storage(root).task_persistence.read_execution_state(TASK)
    assert state is not None
    return state.config_commit


def _complete(root: Path) -> list[str]:
    return [
        "--root",
        str(root),
        "complete",
        TASK,
        "--artifact",
        "Done.",
        "--summary",
        "Done.",
    ]


def test_a_snapshot_holds_the_committed_configuration_and_what_it_names(
    tmp_path: Path,
) -> None:
    commit = _project(tmp_path)
    source = ConfigSnapshots(tmp_path).current()
    assert source.commit == commit
    assert source.ref == "main"
    assert source.directory == tmp_path.resolve() / ".ww/config" / commit
    files = sorted(
        path.relative_to(source.directory).as_posix()
        for path in source.directory.rglob("*")
        if path.is_file()
    )
    assert files == [
        ".ww-reached-files.json",
        "rules/style/naming.md",
        "tools/lint.py",
        "ww-rules.yaml",
        "ww.json",
        "ww.yaml",
    ]
    with pytest.raises(PermissionError):
        (source.directory / "ww.yaml").write_text("changed", encoding="utf-8")
    # A commit's copy is written once.
    assert ConfigSnapshots(tmp_path).current() == source


def test_a_snapshot_holds_the_files_a_rule_files_check_reaches(
    tmp_path: Path,
) -> None:
    """TASK-74: scripts named only in a rule file's ``check`` are copied too.

    One rule reaches its script through ``$WW_ROOT`` in ``shell`` alone, the
    other through ``{{ww.root}}`` in ``argv`` and lists a helper the script
    calls in ``files``; no YAML or JSON file names any of them.
    """
    _project(tmp_path)
    (tmp_path / "rules/style/timestamps.md").write_text(
        "---\ncheck:\n"
        '  shell: python3 "$WW_ROOT/tools/timestamps.py" $WW_STEP_CHANGED_FILES\n'
        "  assert: [empty]\n---\nKeep migration timestamps in order.\n",
        encoding="utf-8",
    )
    (tmp_path / "rules/style/structure.md").write_text(
        "---\ncheck:\n"
        "  argv: [sh, '{{ww.root}}/tools/structure.sh']\n"
        "  files: [tools/structure.sh, tools/structure.php]\n"
        "---\nKeep the structure.\n",
        encoding="utf-8",
    )
    for name in ("timestamps.py", "structure.sh", "structure.php", "unused.sh"):
        (tmp_path / "tools" / name).write_text("# script\n", encoding="utf-8")
    _commit(tmp_path, "rule-file checks")

    source = ConfigSnapshots(tmp_path).current()

    tools = sorted(path.name for path in (source.directory / "tools").iterdir())
    assert tools == ["lint.py", "structure.php", "structure.sh", "timestamps.py"]


def test_a_snapshot_records_the_files_its_checks_reach_that_the_commit_lacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """TASK-74: the scan runs once, as the snapshot is written; every
    command then warns from its record, which no checkout change touches."""
    _project(tmp_path)
    (tmp_path / "rules/style/gone.md").write_text(
        "---\ncheck:\n"
        '  shell: python3 "$WW_ROOT/tools/gone.py" "$WW_ROOT/tools/$NAME.py"\n'
        "  files: [tools/lint.py, tools/helper.php]\n"
        "---\nCheck it.\n",
        encoding="utf-8",
    )
    commit = _commit(tmp_path, "a check reaching uncommitted files")

    snapshots = ConfigSnapshots(tmp_path)
    source = snapshots.current()

    assert [(reach.owner, reach.file) for reach in snapshots.missing(source)] == [
        ("rule file rules/style/gone.md", "tools/gone.py"),
        ("rule file rules/style/gone.md", "tools/helper.php"),
    ]
    assert main(["--root", str(tmp_path), "rules", "--json"]) == 0
    err = capsys.readouterr().err
    assert (
        "ww warning: rule file rules/style/gone.md reaches `tools/gone.py`, "
        f"which commit {commit[:12]} on main does not hold"
    ) in err
    assert "`tools/helper.php`" in err
    assert "a script its script calls included, in the check's `files`" in err
    # The record is the snapshot's: creating the file in the checkout
    # changes nothing until it is committed.
    (tmp_path / "tools/gone.py").write_text("# script\n", encoding="utf-8")
    (tmp_path / "tools/helper.php").write_text("<?php\n", encoding="utf-8")
    assert len(ConfigSnapshots(tmp_path).missing(source)) == 2
    _commit(tmp_path, "commit the scripts")
    assert main(["--root", str(tmp_path), "rules", "--json"]) == 0
    assert "ww warning" not in capsys.readouterr().err


def test_a_snapshot_an_earlier_ww_wrote_is_written_again(tmp_path: Path) -> None:
    """TASK-74: a copy without the record left out what rule files' checks
    reach; it is replaced by a complete one."""
    _project(tmp_path)
    (tmp_path / "rules/style/reached.md").write_text(
        '---\ncheck:\n  shell: python3 "$WW_ROOT/tools/reached.py"\n---\nCheck.\n',
        encoding="utf-8",
    )
    (tmp_path / "tools/reached.py").write_text("# script\n", encoding="utf-8")
    commit = _commit(tmp_path, "rule-file check")
    old = tmp_path / ".ww/config" / commit
    (old / "tools").mkdir(parents=True)
    (old / "ww.yaml").write_text(CONFIG, encoding="utf-8")
    (old / "tools/lint.py").write_text(LINT, encoding="utf-8")
    _make_read_only(old)

    source = ConfigSnapshots(tmp_path).current()

    assert source.directory == old.resolve()
    assert (source.directory / "tools/reached.py").is_file()
    assert (source.directory / ".ww-reached-files.json").is_file()
    assert [entry.name for entry in (tmp_path / ".ww/config").iterdir()] == [commit]


def test_the_checkout_keeps_its_scan_until_a_file_it_read_changes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TASK-74: read from the checkout, the scan is kept in ``.ww`` and read
    again only when a file it looked at changes, as a few ``stat`` calls
    tell."""
    _project(tmp_path, {"config_ref": False})
    (tmp_path / ".ww").mkdir()
    snapshots = ConfigSnapshots(tmp_path)
    source = snapshots.current()
    assert snapshots.missing(source) == ()
    assert (tmp_path / ".ww/reached-files.json").is_file()

    scans: list[str] = []
    real_scan = reached_files.scan

    def counted(files: reached_files.ConfigFiles) -> reached_files.Scan:
        scans.append("scan")
        return real_scan(files)

    monkeypatch.setattr(reached_files, "scan", counted)
    assert snapshots.missing(source) == ()
    assert scans == []

    # A new rule file changes its directory; the scan runs again.
    (tmp_path / "rules/style/gone.md").write_text(
        '---\ncheck:\n  argv: [python3, "{{ww.root}}/tools/gone.py"]\n---\nCheck.\n',
        encoding="utf-8",
    )
    assert [reach.file for reach in snapshots.missing(source)] == ["tools/gone.py"]
    assert len(scans) == 1
    assert main(["--root", str(tmp_path), "rules", "--json"]) == 0
    err = capsys.readouterr().err
    assert (
        "ww warning: rule file rules/style/gone.md reaches `tools/gone.py`, which "
        f"is missing from {tmp_path.resolve()}"
    ) in err
    assert len(scans) == 1
    # Creating the reached file is seen, too.
    (tmp_path / "tools/gone.py").write_text("# script\n", encoding="utf-8")
    assert snapshots.missing(source) == ()
    assert len(scans) == 2


def test_without_git_or_with_config_ref_false_the_checkout_is_read(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(CONFIG, encoding="utf-8")
    assert ConfigSnapshots(tmp_path).current().directory == tmp_path.resolve()
    assert ConfigSnapshots(tmp_path).current().commit is None

    other = tmp_path / "repo"
    other.mkdir()
    _project(other, {"config_ref": False})
    assert ConfigSnapshots(other).current().commit is None
    assert not (other / ".ww/config").exists()


def test_a_ref_without_ww_yaml_reads_the_checkout(tmp_path: Path) -> None:
    _project(tmp_path)
    _git("rm", "-q", "ww.yaml", cwd=tmp_path)
    _git("commit", "-qm", "drop", cwd=tmp_path)
    (tmp_path / "ww.yaml").write_text(CONFIG, encoding="utf-8")
    assert ConfigSnapshots(tmp_path).current().commit is None


def test_a_configured_ref_that_names_nothing_is_an_error(tmp_path: Path) -> None:
    _project(tmp_path, {"config_ref": "release"})
    with pytest.raises(ConfigurationError, match="config_ref 'release'"):
        ConfigSnapshots(tmp_path).current()


def test_the_default_ref_is_the_git_default_base_branch(tmp_path: Path) -> None:
    commit = _project(
        tmp_path, {"extensions": {"ww/git": {"base_branches": {"default": "main"}}}}
    )
    assert ConfigSnapshots(tmp_path).current().commit == commit


def test_a_task_keeps_its_commit_whatever_the_checkout_holds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _project(tmp_path)
    _start(tmp_path, capsys)
    assert _pin(tmp_path) == first

    # The operator commits a new configuration on main, then checks out a
    # branch whose ww.yaml has no rule at all.
    (tmp_path / "rules/style/naming.md").write_text("Renamed.\n", encoding="utf-8")
    second = _commit(tmp_path, "rename")
    _git("switch", "-qc", "other", cwd=tmp_path)
    (tmp_path / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Do it.\n",
        encoding="utf-8",
    )
    _commit(tmp_path, "other")

    assert main(["--root", str(tmp_path), "status", TASK]) == 0
    status = capsys.readouterr().out
    assert f"config: commit {first[:7]} of main; main is 1 commit(s) ahead" in status

    assert main(["--root", str(tmp_path), "next", TASK]) == 0
    page = " ".join(capsys.readouterr().out.split())
    snapshot = tmp_path.resolve() / ".ww/config" / first
    assert f"read from `{snapshot}`, a read-only copy of commit `{first[:7]}`" in page

    ran = tmp_path / "ran.txt"
    monkeypatch.setenv("RAN", str(ran))
    assert main(_complete(tmp_path)) == 0
    capsys.readouterr()
    cwd, root = ran.read_text(encoding="utf-8").splitlines()
    assert Path(root) == snapshot
    assert Path(cwd).resolve() == tmp_path.resolve()
    assert second != first


def test_config_refresh_moves_a_task_to_the_refs_current_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _project(tmp_path)
    _start(tmp_path, capsys)
    (tmp_path / "README.md").write_text("Changed.\n", encoding="utf-8")
    second = _commit(tmp_path, "readme")

    assert main(["--root", str(tmp_path), "config", "refresh", TASK]) == 0
    assert f"now reads the configuration of {second[:7]}" in capsys.readouterr().out
    assert _pin(tmp_path) == second
    # The first commit's snapshot is read by nobody any more.
    assert not (tmp_path / ".ww/config" / first).exists()

    assert main(["--root", str(tmp_path), "config", "refresh", TASK]) == 0
    assert "already reads" in capsys.readouterr().out


def test_a_removed_snapshot_is_written_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commit = _project(tmp_path)
    _start(tmp_path, capsys)
    snapshots = ConfigSnapshots(tmp_path)
    snapshots.prune(())
    assert snapshots.snapshots() == ()
    assert main(["--root", str(tmp_path), "instruction", TASK]) == 0
    assert snapshots.snapshots() == (commit,)


def test_prune_keeps_the_pinned_and_the_current_commits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pinned = _project(tmp_path)
    _start(tmp_path, capsys)
    (tmp_path / "README.md").write_text("One.\n", encoding="utf-8")
    stale = _commit(tmp_path, "one")
    snapshots = ConfigSnapshots(tmp_path)
    snapshots.materialize(stale)
    (tmp_path / "README.md").write_text("Two.\n", encoding="utf-8")
    current = _commit(tmp_path, "two")
    snapshots.materialize(current)

    assert main(["--root", str(tmp_path), "config", "prune"]) == 0
    assert "Removed 1 configuration snapshot(s)." in capsys.readouterr().out
    assert set(snapshots.snapshots()) == {pinned, current}


def test_local_files_of_the_checkout_apply_over_the_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(tmp_path)
    (tmp_path / "ww.local.yaml").write_text(
        "workflows:\n  - name: mine\n    description: Only mine.\n"
        "    steps:\n      - work: Do it.\n",
        encoding="utf-8",
    )
    assert main(["--root", str(tmp_path), "discover"]) == 0
    assert "- mine — [local: ww.local.yaml] Only mine." in capsys.readouterr().out


def test_commands_that_edit_the_configuration_read_the_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(tmp_path)
    (tmp_path / "ww.yaml").write_text("workflows: [", encoding="utf-8")
    assert main(["--root", str(tmp_path), "discover"]) == 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "lint"]) == 1
    assert "ww.yaml" in capsys.readouterr().err


def test_checks_may_not_name_files_git_ignores(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    # Outside Git nothing is refused.
    require_committable(("node_modules/.bin/eslint",), plain)
    repository = tmp_path / "repo"
    repository.mkdir()
    _project(repository)
    require_committable(("tools/lint.py", "tools/new.py"), repository)
    with pytest.raises(StateError, match="`node_modules/.bin/eslint` is ignored"):
        require_committable(("tools/lint.py", "node_modules/.bin/eslint"), repository)


def test_a_snapshot_maps_back_to_its_checkout(tmp_path: Path) -> None:
    snapshot = tmp_path / ".ww/config" / ("a" * 40)
    assert checkout_directory(snapshot) == tmp_path
    unknown = tmp_path / ".ww/config/abc"
    assert checkout_directory(unknown) == unknown
    assert snapshot_of(snapshot / "rules/x.md") == snapshot
    assert snapshot_of(tmp_path / "rules/x.md") is None
