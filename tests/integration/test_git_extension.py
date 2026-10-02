# SPDX-License-Identifier: GPL-3.0-or-later
"""The ww/git extension against real temporary repositories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import util
from pathlib import Path

import pytest

from ww.errors import ConfigurationError
from ww.extensions import ExtensionCheckResult, ExtensionContext, ExtensionStore
from ww.variables import BRANCH_NAMING_STRATEGY

SOURCE = Path(__file__).parents[2] / "ext/ww/git/extension.py"


def _extension_module():
    spec = util.spec_from_file_location("ww_git_under_test", SOURCE)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


git_extension = _extension_module()


def _run(*command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _run("git", "init", "-q", "-b", "main", ".", cwd=tmp_path)
    _run("git", "config", "user.email", "t@e.st", cwd=tmp_path)
    _run("git", "config", "user.name", "Test", cwd=tmp_path)
    (tmp_path / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=tmp_path)
    _run("git", "commit", "-qm", "seed", cwd=tmp_path)
    return tmp_path


def context(
    root: Path,
    config: dict[str, object] | None = None,
    *,
    task_id: str | None = "TASK-1",
    workflow: str | None = "task",
    lane: str | None = None,
    values: dict[str, str] | None = None,
    arguments: tuple[str, ...] = (),
    workspace: Path | None = None,
    operation_id: str | None = None,
) -> ExtensionContext:
    return ExtensionContext(
        root=root,
        store=ExtensionStore(root, "ww/git"),
        config=config or {},
        task_id=task_id,
        run_id="01-task",
        workflow=workflow,
        lane=lane,
        values=values or {},
        arguments=arguments,
        workspace=workspace,
        operation_id=operation_id,
    )


def branch_of(root: Path) -> str:
    return _run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=root).stdout.strip()


def handler(name: str):
    return git_extension.EXTENSION.handlers_by_name[name].run


def command(name: str):
    return git_extension.EXTENSION.commands_by_name[name].run


@pytest.mark.parametrize("worktrees", [False, True])
def test_scriptizing_requires_default_before_creating_a_branch(
    repository: Path, worktrees: bool
) -> None:
    result = handler("start-task-branch")(
        context(
            repository,
            {
                "worktrees": worktrees,
                "worktree_dir": "wt",
                "base_branches": {"ww-scriptize-rules": "main"},
            },
            workflow="ww-scriptize-rules",
        )
    )
    assert not result.ok
    assert "extensions.ww/git.base_branches.default" in result.error
    assert branch_of(repository) == "main"
    assert not (repository / "wt").exists()


def test_scriptizing_returns_to_default_even_when_separate_branch_is_off(
    repository: Path,
) -> None:
    ctx = context(
        repository,
        {"base_branches": {"default": "main"}},
        workflow="ww-scriptize-rules",
    )
    assert handler("start-task-branch")(ctx).ok
    assert branch_of(repository) == "task-1"
    assert handler("return-to-base-branch")(ctx).ok
    assert branch_of(repository) == "main"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def test_defaults_apply_when_nothing_is_configured() -> None:
    settings = git_extension.settings_from({})

    assert settings.commit_format == "{{ww.task.id}}: {{commit_message}}"
    assert settings.separate_branch is False
    assert settings.worktrees is False
    assert settings.branch_format("task") == "{{ww.task.id}}"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"commit_format": "{{unknown}}: {{commit_message}}"}, "unknown placeholder"),
        ({"commit_format": "{{ww.task.id}}: subject"}, "commit_message"),
        (
            {"commit_format": "{{commit_message}} / {{commit_message}}"},
            "exactly one",
        ),
        ({"commit_format": "{{commit_message"}, "invalid interpolation"),
    ],
)
def test_commit_format_requires_one_known_message_placeholder(
    config: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        git_extension.settings_from(config)


def test_an_unknown_setting_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as error:
        git_extension.settings_from({"comit_format": "x"})

    # Silently ignoring a typo leaves the file looking configured.
    assert "unknown setting(s): comit_format" in str(error.value)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"separate_branch": "yes"}, "must be true or false"),
        ({"commit_format": ""}, "must be a non-empty string"),
        ({"branch_name_formats": ["a"]}, "map workflow names to formats"),
        ({"base_branches": {"default": {"shell": "echo main"}}}, "containing argv"),
        ({"base_branches": {"default": {"argv": []}}}, "non-empty array"),
        ({"base_branches": []}, "map workflow names, or default, to base"),
        (
            {"base_branches": {"task": {"argv": ["echo", ""]}}},
            "non-empty array",
        ),
        ({"worktrees": True}, "worktree_dir is required"),
    ],
)
def test_invalid_settings_are_rejected(config: dict, message: str) -> None:
    with pytest.raises(ConfigurationError) as error:
        git_extension.settings_from(config)

    assert message in str(error.value)


def test_branch_formats_fall_back_to_default() -> None:
    settings = git_extension.settings_from(
        {
            "branch_name_formats": {
                "default": "feature/{{ww.task.id}}",
                "bugfix": "hotfix/{{ww.task.id}}",
            }
        }
    )

    assert settings.branch_format("bugfix") == "hotfix/{{ww.task.id}}"
    assert settings.branch_format("task") == "feature/{{ww.task.id}}"
    assert settings.branch_format(None) == "feature/{{ww.task.id}}"


def test_the_settings_command_prints_what_resolved(repository: Path) -> None:
    output = command("settings")(
        context(
            repository,
            {
                "base_branches": {
                    "default": "master",
                    "task": {"argv": ["./find-base"]},
                },
            },
        )
    )

    assert json.loads(output)["base_branches"] == {
        "default": "master",
        "task": {"argv": ["./find-base"]},
    }


# --------------------------------------------------------------------------- #
# commit_format
# --------------------------------------------------------------------------- #


def test_commit_format_renders_the_subject(repository: Path) -> None:
    (repository / "new.txt").write_text("x\n", encoding="utf-8")

    result = handler("git-commit")(
        context(
            repository,
            {
                "commit_format": (
                    "Task {{ww.task.id}}: {{commit_message}} [{{ww.task.workflow}}]"
                )
            },
            values={"commit_message": "do a thing"},
        )
    )

    assert result.ok, result.error
    subject = _run("git", "log", "-1", "--format=%s", cwd=repository).stdout.strip()
    assert subject == "Task TASK-1: do a thing [task]"


@pytest.mark.parametrize(
    ("message", "subject"),
    [
        ("TASK-1: do a thing", "TASK-1: do a thing"),
        ("TASK-1:do a thing", "TASK-1: do a thing"),
        ("TASK-1: TASK-1: do a thing", "TASK-1: TASK-1: do a thing"),
        ("TASK-10: do a thing", "TASK-1: TASK-10: do a thing"),
        ("TASK-1:", "TASK-1: TASK-1:"),
    ],
)
def test_commit_does_not_repeat_a_prefix_the_agent_already_wrote(
    repository: Path, message: str, subject: str
) -> None:
    (repository / "new.txt").write_text("x\n", encoding="utf-8")

    result = handler("git-commit")(
        context(repository, values={"commit_message": message})
    )

    assert result.ok, result.error
    assert _run("git", "log", "-1", "--format=%s", cwd=repository).stdout.strip() == (
        subject
    )


def test_a_commit_records_the_branch_it_landed_on(repository: Path) -> None:
    (repository / "new.txt").write_text("x\n", encoding="utf-8")

    handler("git-commit")(context(repository, values={"commit_message": "work"}))

    recorded = json.loads(
        (repository / ".ww/ext/ww/git/commits.jsonl").read_text(encoding="utf-8")
    )
    assert recorded["branch"] == "main"
    assert recorded["message"] == "TASK-1: work"


def _refuse_signing(repository: Path) -> None:
    """Sign every commit with a program that always fails, like a locked agent."""
    _run("git", "config", "commit.gpgsign", "true", cwd=repository)
    _run("git", "config", "gpg.format", "openpgp", cwd=repository)
    _run("git", "config", "gpg.program", "false", cwd=repository)


def test_a_signing_failure_stops_for_the_operator_by_default(
    repository: Path,
) -> None:
    _refuse_signing(repository)
    (repository / "new.txt").write_text("x\n", encoding="utf-8")

    result = handler("git-commit")(
        context(repository, values={"commit_message": "work"})
    )

    assert not result.ok
    assert "failed to write commit object" in (result.error or "")


def test_on_signing_failure_unsigned_commits_once_more_without_a_signature(
    repository: Path,
) -> None:
    _refuse_signing(repository)
    (repository / "new.txt").write_text("x\n", encoding="utf-8")

    result = handler("git-commit")(
        context(
            repository,
            {"on_signing_failure": "unsigned"},
            values={"commit_message": "work"},
        )
    )

    assert result.ok, result.error
    assert "(unsigned: git could not sign it" in (result.output or "")
    assert _run("git", "log", "-1", "--format=%s", cwd=repository).stdout.strip() == (
        "TASK-1: work"
    )
    recorded = json.loads(
        (repository / ".ww/ext/ww/git/commits.jsonl").read_text(encoding="utf-8")
    )
    assert recorded["signed"] is False


def test_on_signing_failure_leaves_other_commit_errors_to_the_operator(
    repository: Path,
) -> None:
    hook = repository / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho refused by hook >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    (repository / "new.txt").write_text("x\n", encoding="utf-8")

    result = handler("git-commit")(
        context(
            repository,
            {"on_signing_failure": "unsigned"},
            values={"commit_message": "work"},
        )
    )

    assert not result.ok
    assert "refused by hook" in (result.error or "")


def test_on_signing_failure_accepts_only_its_two_policies() -> None:
    with pytest.raises(ConfigurationError, match="on_signing_failure must be one of"):
        git_extension.settings_from({"on_signing_failure": "skip"})


def test_commit_rejects_a_multiline_subject(repository: Path) -> None:
    (repository / "new.txt").write_text("x\n", encoding="utf-8")

    result = handler("git-commit")(
        context(repository, values={"commit_message": "subject\nbody"})
    )

    assert not result.ok
    assert result.error == "commit_message must be a single line"


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("subject\nbody", "commit_message must be a single line"),
        ("subject\rbody", "commit_message must be a single line"),
        ("   ", "commit_message is required"),
        ("one clear subject", None),
    ],
)
def test_the_commit_message_is_validated_when_supplied(
    message: str, error: str | None
) -> None:
    validate = git_extension.EXTENSION.handlers_by_name["git-commit"].validate

    assert validate is not None
    assert validate({"commit_message": message}) == error


def test_a_commit_stages_changes_before_running_a_pre_commit_hook(
    repository: Path,
) -> None:
    hook = repository / ".git/hooks/pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "if git diff --cached --quiet; then echo 'no staged files' >&2; exit 1; fi\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    (repository / "new.txt").write_text("x\n", encoding="utf-8")

    result = handler("git-commit")(
        context(repository, values={"commit_message": "work"})
    )

    assert result.ok, result.error


def test_a_commit_with_nothing_to_stage_succeeds_without_running_hooks(
    repository: Path,
) -> None:
    hook = repository / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
    hook.chmod(0o755)
    before = _run("git", "rev-parse", "HEAD", cwd=repository).stdout

    result = handler("git-commit")(
        context(repository, values={"commit_message": "work"})
    )

    assert result.ok, result.error
    assert result.output == "nothing to commit; the workspace has no changes"
    assert _run("git", "rev-parse", "HEAD", cwd=repository).stdout == before


def test_a_commit_retry_checks_the_operation_trailer(repository: Path) -> None:
    (repository / "new.txt").write_text("x\n", encoding="utf-8")
    first = handler("git-commit")(
        context(
            repository,
            values={"commit_message": "work"},
            operation_id="TASK-1:01-task:commit",
        )
    )
    assert first.ok, first.error

    # The same operation ID is an idempotency key: the second call returns the
    # original commit instead of creating a duplicate commit.
    second = handler("git-commit")(
        context(
            repository,
            values={"commit_message": "work"},
            operation_id="TASK-1:01-task:commit",
        )
    )
    assert second.ok, second.error
    count = _run("git", "rev-list", "--count", "HEAD", cwd=repository).stdout.strip()
    assert count == "2"


def test_a_commit_retry_finds_its_commit_behind_newer_ones(repository: Path) -> None:
    (repository / "new.txt").write_text("x\n", encoding="utf-8")
    first = handler("git-commit")(
        context(
            repository,
            values={"commit_message": "work"},
            operation_id="TASK-1:01-task:commit",
        )
    )
    assert first.ok, first.error
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "later\n\nwith a body"],
        cwd=repository,
        check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "2090-01-01T00:00:00+0000"},
    )

    found = git_extension._check_commit(
        context(repository, operation_id="TASK-1:01-task:commit")
    )

    assert found.status == "succeeded"
    assert found.result.output == first.output


def test_git_commit_does_not_duplicate_when_checker_is_uncertain(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repository / "new.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        git_extension,
        "_check_commit",
        lambda context: ExtensionCheckResult.unknown("ambiguous Git history"),
    )
    result = handler("git-commit")(
        context(
            repository,
            values={"commit_message": "work"},
            operation_id="TASK-1:01-task:ambiguous",
        )
    )
    assert not result.ok
    assert "ambiguous Git history" in result.error
    count = _run("git", "rev-list", "--count", "HEAD", cwd=repository).stdout.strip()
    assert count == "1"


def test_commit_rejects_a_workspace_that_is_not_the_worktree_root(
    repository: Path,
) -> None:
    nested = repository / "nested"
    nested.mkdir()
    (nested / "new.txt").write_text("x\n", encoding="utf-8")

    result = handler("git-commit")(
        context(
            repository,
            values={"commit_message": "work"},
            workspace=nested,
        )
    )

    assert not result.ok
    assert "not the Git worktree root" in result.error
    assert (nested / "new.txt").is_file()
    assert _run("git", "status", "--porcelain", cwd=repository).stdout.strip()


def test_status_path_parser_checks_both_sides_of_a_rename() -> None:
    assert git_extension._status_paths("R  renamed.txt\0original.txt\0") == (
        "renamed.txt",
        "original.txt",
    )


# --------------------------------------------------------------------------- #
# merge-branch
# --------------------------------------------------------------------------- #


def _feature_branch(repository: Path, name: str = "feature", path: str = "b.txt"):
    """A branch with one commit of its own, and one more commit on main."""
    _run("git", "checkout", "-qb", name, cwd=repository)
    (repository / path).write_text(f"{name}\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=repository)
    _run("git", "commit", "-qm", f"work on {name}", cwd=repository)
    _run("git", "checkout", "-q", "main", cwd=repository)
    (repository / "main.txt").write_text("main\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=repository)
    _run("git", "commit", "-qm", "work on main", cwd=repository)


def _merge(repository: Path, config: dict[str, object] | None = None, **kwargs):
    return handler("merge-branch")(
        context(
            repository,
            config,
            arguments=("feature", "Land slice TASK-1.1"),
            **kwargs,
        )
    )


def _head(repository: Path) -> str:
    return _run("git", "rev-parse", "HEAD", cwd=repository).stdout.strip()


def _merging(repository: Path) -> bool:
    return (repository / ".git/MERGE_HEAD").exists()


def test_merge_branch_merges_with_a_merge_commit_and_records_it(
    repository: Path,
) -> None:
    _feature_branch(repository)
    tip = _run("git", "rev-parse", "feature", cwd=repository).stdout.strip()

    result = _merge(repository)

    assert result.ok, result.error
    head = _head(repository)
    assert result.values == {"merge_commit": head}
    assert _run("git", "rev-parse", "HEAD^2", cwd=repository).stdout.strip() == tip
    subject = _run("git", "log", "-1", "--format=%s", cwd=repository).stdout.strip()
    assert subject == "TASK-1: Land slice TASK-1.1"
    assert not _merging(repository)
    recorded = json.loads(
        (repository / ".ww/ext/ww/git/commits.jsonl").read_text(encoding="utf-8")
    )
    assert (recorded["sha"], recorded["branch"], recorded["merged"]) == (
        head,
        "main",
        "feature",
    )


def test_merge_branch_aborts_a_conflict_and_fails_naming_the_files(
    repository: Path,
) -> None:
    _feature_branch(repository, path="seed.txt")
    (repository / "seed.txt").write_text("main side\n", encoding="utf-8")
    _run("git", "commit", "-qam", "change seed on main", cwd=repository)
    before = _head(repository)

    result = _merge(repository)

    assert not result.ok
    assert "merging feature conflicts in: seed.txt" in result.error
    assert "the merge was aborted" in result.error
    assert not _merging(repository)
    assert _head(repository) == before
    assert _run("git", "status", "--porcelain", cwd=repository).stdout == ""


def test_merge_branch_refuses_a_dirty_workspace(repository: Path) -> None:
    _feature_branch(repository)
    before = _head(repository)
    (repository / "seed.txt").write_text("edited\n", encoding="utf-8")

    result = _merge(repository)

    assert not result.ok
    assert "uncommitted change(s)" in result.error
    assert _head(repository) == before


def test_merge_branch_stops_on_a_signing_failure_by_default(
    repository: Path,
) -> None:
    _feature_branch(repository)
    before = _head(repository)
    _refuse_signing(repository)

    result = _merge(repository)

    assert not result.ok
    assert "failed to write commit object" in result.error
    assert "the merge was aborted" in result.error
    assert not _merging(repository)
    assert _head(repository) == before


def test_merge_branch_merges_unsigned_when_on_signing_failure_allows(
    repository: Path,
) -> None:
    _feature_branch(repository)
    tip = _run("git", "rev-parse", "feature", cwd=repository).stdout.strip()
    _refuse_signing(repository)

    result = _merge(repository, {"on_signing_failure": "unsigned"})

    assert result.ok, result.error
    assert "(unsigned: git could not sign it" in result.output
    assert _run("git", "rev-parse", "HEAD^2", cwd=repository).stdout.strip() == tip
    assert not _merging(repository)
    recorded = json.loads(
        (repository / ".ww/ext/ww/git/commits.jsonl").read_text(encoding="utf-8")
    )
    assert recorded["signed"] is False


def test_merge_branch_retry_returns_the_merge_an_attempt_already_made(
    repository: Path,
) -> None:
    _feature_branch(repository)
    first = _merge(repository, operation_id="TASK-1:01-task:land")
    assert first.ok, first.error

    second = _merge(repository, operation_id="TASK-1:01-task:land")

    assert second.ok, second.error
    assert second.values == first.values
    assert _head(repository) == first.values["merge_commit"]


def test_merge_branch_redoes_its_own_interrupted_merge_but_not_another(
    repository: Path,
) -> None:
    _feature_branch(repository)
    _run(
        "git",
        "merge",
        "--no-ff",
        "--no-commit",
        "-m",
        "Land",
        "-m",
        "WW-Operation: TASK-1:01-task:land",
        "feature",
        cwd=repository,
    )

    foreign = _merge(repository, operation_id="TASK-1:01-task:other")
    assert not foreign.ok
    assert "a merge is already in progress" in foreign.error
    assert _merging(repository)

    own = _merge(repository, operation_id="TASK-1:01-task:land")
    assert own.ok, own.error
    assert not _merging(repository)
    assert own.values == {"merge_commit": _head(repository)}


def _interrupted_conflict(repository: Path) -> str:
    """A conflicted merge this operation started and never concluded.

    Beside the conflict, both sides change ``lines.txt`` in different places,
    which git merges cleanly and stages as part of the merge's own state.
    """
    lines = repository / "lines.txt"
    lines.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    _run("git", "add", "lines.txt", cwd=repository)
    _run("git", "commit", "-qm", "add lines", cwd=repository)
    _run("git", "checkout", "-qb", "feature", cwd=repository)
    lines.write_text("ONE\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    _run("git", "commit", "-qam", "feature lines", cwd=repository)
    _run("git", "checkout", "-q", "main", cwd=repository)
    lines.write_text("one\ntwo\nthree\nfour\nFIVE\n", encoding="utf-8")
    _run("git", "commit", "-qam", "main lines", cwd=repository)
    _run("git", "checkout", "-q", "feature", cwd=repository)
    _run("git", "branch", "-q", "-m", "feature", "feature-base", cwd=repository)
    _run("git", "checkout", "-q", "main", cwd=repository)
    _feature_branch(repository, path="seed.txt")
    _run("git", "checkout", "-q", "feature", cwd=repository)
    _run("git", "merge", "-q", "--no-edit", "feature-base", cwd=repository)
    _run("git", "checkout", "-q", "main", cwd=repository)
    (repository / "seed.txt").write_text("main side\n", encoding="utf-8")
    _run("git", "commit", "-qam", "change seed on main", cwd=repository)
    conflicted = subprocess.run(
        [
            "git",
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            "Land",
            "-m",
            "WW-Operation: TASK-1:01-task:land",
            "feature",
        ],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    assert conflicted.returncode and _merging(repository)
    staged = _run("git", "diff", "--cached", "--name-only", "HEAD", cwd=repository)
    assert "lines.txt" in staged.stdout.split()
    return _head(repository)


def test_merge_branch_keeps_a_resolution_staged_in_its_interrupted_merge(
    repository: Path,
) -> None:
    before = _interrupted_conflict(repository)
    (repository / "seed.txt").write_text("resolved\n", encoding="utf-8")
    _run("git", "add", "seed.txt", cwd=repository)

    result = _merge(repository, operation_id="TASK-1:01-task:land")

    assert not result.ok
    assert "was worked on since" in result.error
    assert "`git commit`" in result.error
    assert _merging(repository)
    assert _head(repository) == before
    assert (repository / "seed.txt").read_text() == "resolved\n"
    staged = _run("git", "diff", "--cached", "--name-only", cwd=repository).stdout
    assert "seed.txt" in staged.split()


def test_merge_branch_keeps_an_edited_conflict_file_in_its_interrupted_merge(
    repository: Path,
) -> None:
    _interrupted_conflict(repository)
    (repository / "seed.txt").write_text("being resolved\n", encoding="utf-8")

    result = _merge(repository, operation_id="TASK-1:01-task:land")

    assert not result.ok
    assert "was worked on since" in result.error
    assert (repository / "seed.txt").read_text() == "being resolved\n"


def test_merge_branch_redoes_its_interrupted_merge_while_the_conflict_is_untouched(
    repository: Path,
) -> None:
    before = _interrupted_conflict(repository)

    result = _merge(repository, operation_id="TASK-1:01-task:land")

    # Merged again from scratch: the same conflict, aborted and reported.
    assert not result.ok
    assert "merging feature conflicts in: seed.txt;" in result.error
    assert not _merging(repository)
    assert _head(repository) == before


def test_merge_branch_refuses_a_detached_head(repository: Path) -> None:
    _feature_branch(repository)
    _run("git", "checkout", "-q", "--detach", "main", cwd=repository)
    before = _head(repository)

    result = _merge(repository)

    assert not result.ok
    assert "HEAD is detached" in result.error
    assert _head(repository) == before


@pytest.mark.parametrize(
    ("state", "operation"),
    [
        ("rebase-merge", "a rebase"),
        ("rebase-apply", "a rebase"),
        ("CHERRY_PICK_HEAD", "a cherry-pick"),
        ("REVERT_HEAD", "a revert"),
    ],
)
def test_merge_branch_refuses_while_another_operation_is_in_progress(
    repository: Path, state: str, operation: str
) -> None:
    _feature_branch(repository)
    before = _head(repository)
    marker = repository / ".git" / state
    if state.startswith("rebase"):
        marker.mkdir()
    else:
        marker.write_text(before + "\n", encoding="utf-8")

    result = _merge(repository)

    assert not result.ok
    assert f"{operation}" in result.error and "in progress" in result.error
    assert _head(repository) == before


def test_merge_branch_reports_a_branch_already_merged(repository: Path) -> None:
    _feature_branch(repository)
    assert _merge(repository).ok
    head = _head(repository)

    again = _merge(repository)

    assert again.ok, again.error
    assert "already merged" in again.output
    assert again.values == {"merge_commit": ""}
    assert _head(repository) == head


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("feature",), "takes two args"),
        (("", "Land"), "rendered empty"),
        (("--force", "Land"), "not a branch name"),
        (("feature", "two\nlines"), "one non-empty line"),
        (("missing", "Land"), "branch 'missing' does not exist"),
    ],
)
def test_merge_branch_rejects_unusable_arguments(
    repository: Path, arguments: tuple[str, ...], message: str
) -> None:
    result = handler("merge-branch")(context(repository, arguments=arguments))

    assert not result.ok
    assert message in result.error


# --------------------------------------------------------------------------- #
# Branching
# --------------------------------------------------------------------------- #


def test_nothing_happens_when_branching_is_off(repository: Path) -> None:
    result = handler("start-task-branch")(context(repository))

    assert result.ok
    assert "current branch" in result.output
    assert branch_of(repository) == "main"


def test_a_task_branch_is_created_from_the_base(repository: Path) -> None:
    _run("git", "switch", "-qc", "other", cwd=repository)

    result = handler("start-task-branch")(
        context(
            repository,
            {
                "separate_branch": True,
                "base_branches": {"default": "main"},
                "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
            },
        )
    )

    assert result.ok, result.error
    assert branch_of(repository) == "feature/task-1"
    # Cut from the configured base, not from wherever HEAD happened to be.
    merge_base = _run(
        "git", "merge-base", "feature/task-1", "main", cwd=repository
    ).stdout.strip()
    main = _run("git", "rev-parse", "main", cwd=repository).stdout.strip()
    assert merge_base == main


def test_a_child_task_branch_uses_its_parent_task_branch(repository: Path) -> None:
    config = {
        "separate_branch": True,
        "base_branches": {"default": "main"},
        "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
    }
    parent = handler("start-task-branch")(context(repository, config, task_id="TASK-1"))
    assert parent.ok, parent.error

    child = handler("start-task-branch")(
        context(repository, config, task_id="TASK-1/TASK-1.1")
    )

    assert child.ok, child.error
    assert branch_of(repository) == "feature/task-1-task-1.1"
    base = _run(
        "git", "merge-base", "feature/task-1-task-1.1", "feature/task-1", cwd=repository
    ).stdout.strip()
    parent_head = _run(
        "git", "rev-parse", "feature/task-1", cwd=repository
    ).stdout.strip()
    assert base == parent_head


def test_the_branch_format_is_chosen_per_workflow(repository: Path) -> None:
    config = {
        "separate_branch": True,
        "branch_name_formats": {
            "default": "feature/{{ww.task.id}}",
            "bugfix": "hotfix/{{ww.task.id}}",
        },
    }

    handler("start-task-branch")(context(repository, config, workflow="bugfix"))

    assert branch_of(repository) == "hotfix/task-1"


def test_an_explicit_branch_strategy_overrides_the_workflow(repository: Path) -> None:
    config = {
        "separate_branch": True,
        "branch_name_formats": {
            "default": "feature/{{ww.task.id}}",
            "bugfix": "hotfix/{{ww.task.id}}",
            "experiment": "experiment/{{ww.task.id}}",
        },
    }

    result = handler("start-task-branch")(
        context(
            repository,
            config,
            workflow="bugfix",
            values={BRANCH_NAMING_STRATEGY: "experiment"},
        )
    )

    assert result.ok, result.error
    assert branch_of(repository) == "experiment/task-1"


def test_an_unknown_explicit_branch_strategy_fails(repository: Path) -> None:
    result = handler("start-task-branch")(
        context(
            repository,
            {
                "separate_branch": True,
                "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
            },
            values={BRANCH_NAMING_STRATEGY: "missing"},
        )
    )

    assert not result.ok
    assert result.error == "branch naming strategy not found: missing"


def test_the_base_branch_is_chosen_per_workflow(repository: Path) -> None:
    _run("git", "branch", "develop", cwd=repository)
    config = {
        "separate_branch": True,
        "base_branches": {"default": "main", "bugfix": "develop"},
    }

    result = handler("start-task-branch")(
        context(repository, config, workflow="bugfix")
    )

    assert result.ok, result.error
    assert "from develop" in result.output


def test_an_argv_command_can_choose_the_base_branch(repository: Path) -> None:
    _run("git", "branch", "develop", cwd=repository)
    config = {
        "separate_branch": True,
        "base_branches": {
            "default": "main",
            "task": {
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; print(sys.argv[1])",
                    "develop",
                ]
            },
        },
    }

    result = handler("start-task-branch")(context(repository, config))

    assert result.ok, result.error
    assert "from develop" in result.output


def test_base_branch_command_arguments_support_context_tokens(repository: Path) -> None:
    _run("git", "branch", "task-base", cwd=repository)
    config = {
        "separate_branch": True,
        "base_branches": {
            "default": {
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; print(sys.argv[1] + '-base')",
                    "{{ww.task.workflow}}",
                ]
            }
        },
    }

    result = handler("start-task-branch")(context(repository, config))

    assert result.ok, result.error
    assert "from task-base" in result.output


def test_a_base_branch_command_reads_the_lane(repository: Path) -> None:
    _run("git", "branch", "hotfix-base", cwd=repository)
    config = {
        "separate_branch": True,
        "base_branches": {
            "hotfix": {
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; print(sys.argv[1] + '-base')",
                    "{{ww.task.lane}}",
                ]
            }
        },
    }

    result = handler("start-task-branch")(
        context(repository, config, workflow="chores", lane="hotfix")
    )

    assert result.ok, result.error
    assert "from hotfix-base" in result.output


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("import sys; sys.exit(7)", "base branch command failed"),
        ("print('')", "empty branch name"),
        ("print('main\\ndevelop')", "exactly one line"),
    ],
)
def test_an_invalid_base_branch_command_fails_cleanly(
    repository: Path, script: str, message: str
) -> None:
    result = handler("start-task-branch")(
        context(
            repository,
            {
                "separate_branch": True,
                "base_branches": {"default": {"argv": [sys.executable, "-c", script]}},
            },
        )
    )

    assert not result.ok
    assert message in result.error


def test_starting_a_branch_twice_adopts_it(repository: Path) -> None:
    config = {"separate_branch": True}
    start = handler("start-task-branch")
    start(context(repository, config))

    again = start(context(repository, config))

    # ww retries an extension handler whole, so this must not fail or reset it.
    assert again.ok
    assert "already on task-1" in again.output


def test_a_branch_is_adopted_after_switching_away(repository: Path) -> None:
    config = {"separate_branch": True}
    handler("start-task-branch")(context(repository, config))
    _run("git", "switch", "-q", "main", cwd=repository)

    result = handler("start-task-branch")(context(repository, config))

    assert result.ok
    assert "switched to existing task-1" in result.output


def test_a_task_is_required_to_name_a_branch(repository: Path) -> None:
    result = handler("start-task-branch")(
        context(repository, {"separate_branch": True}, task_id=None)
    )

    assert not result.ok
    assert "a task is required" in result.error


def test_returning_to_base_switches_back(repository: Path) -> None:
    config = {"separate_branch": True, "base_branches": {"default": "main"}}
    handler("start-task-branch")(context(repository, config))
    assert branch_of(repository) == "task-1"

    result = handler("return-to-base-branch")(context(repository, config))

    assert result.ok, result.error
    assert branch_of(repository) == "main"


def test_returning_to_base_is_a_noop_when_no_branch_was_made(
    repository: Path,
) -> None:
    result = handler("return-to-base-branch")(context(repository))

    assert result.ok
    assert "no task branch" in result.output


# --------------------------------------------------------------------------- #
# Worktrees
# --------------------------------------------------------------------------- #


def _worktree_config(repository: Path) -> dict[str, object]:
    return {
        "worktrees": True,
        "worktree_dir": str(repository / "trees"),
        "worktree_name_format": "{{ww.task.id}}",
        "base_branches": {"default": "main"},
        "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
    }


def test_branch_and_worktree_creation_are_separate(repository: Path) -> None:
    config = _worktree_config(repository)
    branch = handler("start-task-branch")(context(repository, config))

    assert branch.ok, branch.error
    assert branch.working_directory is None
    assert branch_of(repository) == "main"
    assert not (repository / "trees" / "TASK-1").exists()

    result = handler("create-worktree")(context(repository, config))

    assert result.ok, result.error
    path = repository / "trees" / "TASK-1"
    assert (path / "seed.txt").is_file()
    assert result.working_directory == path
    assert str(path) in result.output
    assert "do this task's work there" in result.output
    assert branch_of(path) == "feature/task-1"
    # The main checkout is left alone.
    assert branch_of(repository) == "main"


def test_git_variable_recovers_the_recorded_task_worktree(repository: Path) -> None:
    config = _worktree_config(repository)
    task_context = context(repository, config)
    assert handler("start-task-branch")(task_context).ok
    created = handler("create-worktree")(task_context)
    assert created.ok

    resolved = git_extension._task_workspace_dir(task_context)

    assert resolved == str((repository / "trees/TASK-1").resolve())


def test_a_commit_uses_the_task_worktree_when_one_is_selected(
    repository: Path,
) -> None:
    config = _worktree_config(repository)
    handler("start-task-branch")(context(repository, config))
    started = handler("create-worktree")(context(repository, config))
    assert started.working_directory is not None
    (started.working_directory / "work.txt").write_text("work\n", encoding="utf-8")

    result = handler("git-commit")(
        context(
            repository,
            config,
            values={"commit_message": "worktree work"},
            workspace=started.working_directory,
        )
    )

    assert result.ok, result.error
    assert branch_of(repository) == "main"
    subject = _run(
        "git", "log", "-1", "--format=%s", cwd=started.working_directory
    ).stdout.strip()
    assert subject == "TASK-1: worktree work"


def test_creating_a_worktree_twice_adopts_it(repository: Path) -> None:
    config = _worktree_config(repository)
    handler("start-task-branch")(context(repository, config))
    handler("create-worktree")(context(repository, config))

    again = handler("create-worktree")(context(repository, config))

    assert again.ok
    assert "already at" in again.output
    assert again.working_directory == repository / "trees" / "TASK-1"


def test_create_worktree_adopts_a_worktree_that_already_holds_the_branch(
    repository: Path,
) -> None:
    """A worktree left elsewhere by an earlier round is reused, not duplicated."""
    config = _worktree_config(repository)
    handler("start-task-branch")(context(repository, config))
    elsewhere = repository / "old-location" / "TASK-1"
    _run(
        "git", "worktree", "add", "-q", str(elsewhere), "feature/task-1", cwd=repository
    )

    result = handler("create-worktree")(context(repository, config))

    assert result.ok, result.error
    assert result.working_directory == elsewhere.resolve()
    assert "adopted the existing worktree" in result.output
    assert not (repository / "trees" / "TASK-1").exists()
    recorded = git_extension._recorded_branch(context(repository, config), "TASK-1")
    assert recorded is not None
    assert recorded["worktree"] == str(elsewhere.resolve())
    assert git_extension._task_workspace_dir(context(repository, config)) == str(
        elsewhere.resolve()
    )


def test_create_worktree_is_a_noop_when_worktrees_are_disabled(
    repository: Path,
) -> None:
    result = handler("create-worktree")(context(repository))

    assert result.ok
    assert "not enabled" in result.output


def test_primary_checkout_is_used_when_it_has_the_configured_task_branch(
    repository: Path,
) -> None:
    config = {
        **_worktree_config(repository),
        "branch_name_formats": {"task": "custom/{{ww.task.workflow}}/{{ww.task.id}}"},
    }
    task_branch = "custom/task/task-1"
    _run("git", "branch", task_branch, "main", cwd=repository)
    _run("git", "switch", "-q", task_branch, cwd=repository)

    branch = handler("start-task-branch")(context(repository, config))
    worktree = handler("create-worktree")(context(repository, config))

    assert branch.ok, branch.error
    assert branch.working_directory == repository
    assert worktree.ok, worktree.error
    assert worktree.working_directory == repository
    assert not (repository / "trees" / "TASK-1").exists()


def test_returning_to_base_does_nothing_under_worktrees(repository: Path) -> None:
    result = handler("return-to-base-branch")(
        context(repository, _worktree_config(repository))
    )

    assert result.ok
    assert "leave the main checkout alone" in result.output


def test_a_worktree_is_removed_when_it_is_clean(repository: Path) -> None:
    config = _worktree_config(repository)
    handler("start-task-branch")(context(repository, config))
    handler("create-worktree")(context(repository, config))

    result = handler("remove-task-worktree")(context(repository, config))

    assert result.ok, result.error
    assert not (repository / "trees" / "TASK-1").exists()


def test_removing_a_worktree_refuses_while_it_holds_changes(
    repository: Path,
) -> None:
    config = _worktree_config(repository)
    handler("start-task-branch")(context(repository, config))
    handler("create-worktree")(context(repository, config))
    (repository / "trees" / "TASK-1" / "wip.txt").write_text("x", encoding="utf-8")

    result = handler("remove-task-worktree")(context(repository, config))

    # Never --force: that would throw away work nobody asked to discard.
    assert not result.ok
    assert (repository / "trees" / "TASK-1").exists()


def test_removing_a_worktree_is_a_noop_when_none_was_recorded(
    repository: Path,
) -> None:
    result = handler("remove-task-worktree")(
        context(repository, _worktree_config(repository))
    )

    assert result.ok
    assert "no worktree recorded" in result.output


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def test_branches_lists_what_was_opened(repository: Path) -> None:
    config = {"separate_branch": True, "base_branches": {"default": "main"}}
    handler("start-task-branch")(context(repository, config))

    assert "No branches recorded" in command("branches")(
        context(repository, arguments=("TASK-9",))
    )
    listed = command("branches")(context(repository))
    assert "TASK-1" in listed
    assert "(from main)" in listed


# --------------------------------------------------------------------------- #
# Projects: repository resolution
# --------------------------------------------------------------------------- #


def test_repository_resolves_the_task_workspace_and_its_worktrees(
    repository: Path,
) -> None:
    nested = repository / "services" / "api"
    nested.mkdir(parents=True)
    _run("git", "init", "-q", "-b", "main", ".", cwd=nested)
    _run("git", "config", "user.email", "t@e.st", cwd=nested)
    _run("git", "config", "user.name", "Test", cwd=nested)
    _run("git", "config", "commit.gpgsign", "false", cwd=nested)
    (nested / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=nested)
    _run("git", "commit", "-qm", "seed", cwd=nested)
    worktree = repository / "wt"
    _run("git", "worktree", "add", "-q", str(worktree), "-b", "wt", cwd=nested)

    assert git_extension._repository(context(repository)) == repository.resolve()
    assert git_extension._repository(context(repository, workspace=nested)) == (
        nested.resolve()
    )
    assert git_extension._repository(context(repository, workspace=worktree)) == (
        nested.resolve()
    )
    # A plain directory inside a repository belongs to that repository.
    plain = repository / "plain"
    plain.mkdir()
    assert git_extension._repository(context(repository, workspace=plain)) == (
        repository.resolve()
    )


# --------------------------------------------------------------------------- #
# Template namespace
# --------------------------------------------------------------------------- #


def git_variable(name: str):
    namespace = git_extension.EXTENSION.namespace
    assert namespace is not None and namespace.name == "git"
    return {variable.name: variable.resolve for variable in namespace.variables}[name]


def test_git_variables_come_from_the_branch_record(repository: Path) -> None:
    config = {
        "separate_branch": True,
        "base_branches": {"default": "main"},
        "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
    }
    task_context = context(repository, config)
    # Nothing recorded yet: not available, which ww reports as an error.
    assert git_variable("branch")(task_context) is None
    assert git_variable("base_branch")(task_context) is None

    assert handler("start-task-branch")(task_context).ok
    _run("git", "switch", "-q", "main", cwd=repository)

    # The record, not whatever the checkout is on now.
    assert git_variable("branch")(task_context) == "feature/task-1"
    assert git_variable("base_branch")(task_context) == "main"


def test_a_child_git_branch_is_its_parent_branch_and_child_id(
    repository: Path,
) -> None:
    config = {
        "separate_branch": True,
        "base_branches": {"default": "main"},
        "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
    }
    assert handler("start-task-branch")(context(repository, config)).ok
    child_context = context(repository, config, task_id="TASK-1/TASK-1.1")
    assert handler("start-task-branch")(child_context).ok

    assert git_variable("branch")(child_context) == "feature/task-1-task-1.1"
    assert git_variable("base_branch")(child_context) == "feature/task-1"


def test_the_branch_strategy_variable_names_the_format_in_use(
    repository: Path,
) -> None:
    config = {"branch_name_formats": {"default": "{{ww.task.id}}", "hotfix": "h"}}
    strategy = git_variable("branch_strategy")

    assert strategy(context(repository, config, workflow="task")) == "default"
    assert strategy(context(repository, config, workflow="hotfix")) == "hotfix"
    assert (
        strategy(
            context(
                repository,
                config,
                workflow="task",
                values={BRANCH_NAMING_STRATEGY: "hotfix"},
            )
        )
        == "hotfix"
    )
