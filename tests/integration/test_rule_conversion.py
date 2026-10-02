# SPDX-License-Identifier: GPL-3.0-or-later
"""Recording scriptized checks outside a task: ``rules convert`` and ``decline``."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from ww.cli import main
from ww.config.rules import rule_text_hash
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.rule_store import STORE_FILE
from ww.service import WorkflowService
from ww.storage import Storage

CLI = "Keep the public CLI unchanged."
NAMES = "Name things clearly."
LOGS = "Log every failure."
WORKFLOWS = f"""workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        rules:
          - {CLI}
          - {NAMES}
          - {LOGS}
      - check: Check it.
"""


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _project(root: Path) -> Path:
    (root / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", ".", cwd=root)
    _git("config", "user.email", "t@e.st", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)
    return root


def _cli(root: Path, *arguments: str) -> int:
    return main(["--root", str(root), "rules", *arguments])


def _store(root: Path) -> dict:
    return json.loads((root / STORE_FILE).read_text(encoding="utf-8"))


def _states(root: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    capsys.readouterr()
    assert _cli(root, "--json") == 0
    data = json.loads(capsys.readouterr().out)
    return {rule["id"]: rule["scriptize"] for rule in data["steps"][0]["rules"]}


def _convert(root: Path, name: str, *covers: str, config: str | None = None) -> int:
    extra = ["--config", config] if config else []
    return _cli(
        root,
        "convert",
        name,
        "--covers",
        *covers,
        "--check-shell",
        "true",
        *extra,
        "--proven",
        "--yes",
    )


def test_convert_records_a_check_covering_the_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert _convert(root, "lint", "develop/1", "develop/2") == 0

    out = capsys.readouterr()
    assert "Check lint (creates the store's entry): true" in out.err
    assert "Confirmed with --yes." in out.err
    assert "Recorded check lint, covering develop/1, develop/2." in out.out
    store = _store(root)
    check = store["checks"]["lint"]
    assert (check["status"], check["approved_by"], check["proven"]) == (
        "converted",
        "operator",
        True,
    )
    assert check["covers"] == [rule_text_hash(CLI), rule_text_hash(NAMES)]
    entry = store["rules"][rule_text_hash(CLI)]
    assert (entry["status"], entry["check"]) == ("converted", "lint")
    assert _states(root, capsys) == {
        "develop/1": "converted",
        "develop/2": "converted",
        "develop/3": "unscriptized",
    }


def test_converting_again_replaces_the_check_and_unscriptizes_dropped_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1", "develop/2") == 0

    assert _convert(root, "lint", "develop/2", "develop/3") == 0

    out = capsys.readouterr()
    assert "replaces the store's entry" in out.err
    assert "No longer covered, so not scriptized: 1 rule wording(s)" in out.out
    store = _store(root)
    assert store["checks"]["lint"]["covers"] == [
        rule_text_hash(NAMES),
        rule_text_hash(LOGS),
    ]
    assert rule_text_hash(CLI) not in store["rules"]
    assert _states(root, capsys)["develop/1"] == "unscriptized"


def test_a_rule_moves_from_another_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1", "develop/2") == 0

    assert _convert(root, "names", "develop/2") == 0

    store = _store(root)
    assert store["checks"]["lint"]["covers"] == [rule_text_hash(CLI)]
    assert store["rules"][rule_text_hash(NAMES)]["check"] == "names"


def test_convert_needs_the_operator_or_yes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert (
        _cli(root, "convert", "lint", "--covers", "develop/1", "--check-argv", "true")
        == 1
    )

    assert "needs the operator's confirmation" in capsys.readouterr().err
    assert not (root / STORE_FILE).exists()


def test_a_dry_run_records_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert (
        _cli(
            root,
            "convert",
            "lint",
            "--covers",
            "develop/1",
            "--check-argv",
            "ruff",
            "check",
            "--config",
            "ruff.toml",
            "--dry-run",
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "Check lint (creates the store's entry): ruff check" in out
    assert "- config files: ruff.toml" in out
    assert "Dry run: nothing was recorded." in out
    assert not (root / STORE_FILE).exists()


def test_argv_after_the_separator_keeps_the_tools_own_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert (
        _cli(
            root,
            "convert",
            "lint",
            "--covers",
            "develop/1",
            "--config",
            "ruff.toml",
            "--proven",
            "--yes",
            "--check-argv",
            "--",
            "ruff",
            "check",
            "--select",
            "E",
            "--config",
            "ruff.toml",
        )
        == 0
    )

    capsys.readouterr()
    check = _store(root)["checks"]["lint"]
    assert check["argv"] == [
        "ruff",
        "check",
        "--select",
        "E",
        "--config",
        "ruff.toml",
    ]
    assert check["config"] == ["ruff.toml"]


def test_rules_add_takes_argv_after_the_separator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    (root / "rules/docs").mkdir(parents=True)
    (root / "ww.yaml").write_text(
        "rules:\n  docs: [rules/docs/]\n" + WORKFLOWS, encoding="utf-8"
    )

    assert (
        _cli(
            root,
            "add",
            "docs",
            "--text",
            "Lint the docs. Always.",
            "--check-argv",
            "--",
            "ruff",
            "check",
            "--select",
            "E",
        )
        == 0
    ), capsys.readouterr().err

    (written,) = (root / "rules/docs").glob("*.md")
    assert "argv: [ruff, check, --select, E]" in written.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("covers", "message"),
    [
        (["develop/9"], "no rule 'develop/9' is declared"),
        (["develop/1", "develop/1"], "named twice"),
    ],
)
def test_convert_refuses_unknown_rules(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    covers: list[str],
    message: str,
) -> None:
    root = _project(tmp_path)

    assert _convert(root, "lint", *covers) == 1

    assert message in capsys.readouterr().err


def test_decline_records_rules_as_not_convertible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1", "develop/3") == 0

    assert (
        _cli(root, "decline", "develop/3", "--reason", "Needs judgement.", "--yes") == 0
    )

    store = _store(root)
    entry = store["rules"][rule_text_hash(LOGS)]
    assert (entry["status"], entry["reason"]) == ("not_convertible", "Needs judgement.")
    assert store["checks"]["lint"]["covers"] == [rule_text_hash(CLI)]
    states = _states(root, capsys)
    assert states["develop/3"] == "not_convertible"
    capsys.readouterr()
    assert _cli(root) == 0
    assert "(judged: declined for scriptizing)" in capsys.readouterr().out


def test_a_check_whose_config_is_missing_is_judged_instead(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1", config="lint.toml") == 0
    service = WorkflowService(Storage(root))
    service.start(
        "task", "TASK-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )
    service.next("TASK-1")
    (root / "app.py").write_text("print(1)\n", encoding="utf-8")

    held = service.complete("TASK-1", artifact="Built.", summary_for_next="Built.")

    assert held.verification is not None
    rule = next(rule for rule in held.verification.rules if rule.id == "develop/1")
    assert (rule.check, rule.missing) == ("lint", "lint.toml")
    page = MarkdownOutputAdapter().render_instruction(held)
    assert (
        "Its check `lint` does not run here: `lint.toml` is missing in this "
        "step's directory, so judge it instead."
    ) in page


def test_a_check_whose_config_exists_runs(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "lint.toml").write_text("", encoding="utf-8")
    assert _convert(root, "lint", "develop/1", config="lint.toml") == 0
    service = WorkflowService(Storage(root))
    service.start(
        "task", "TASK-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )
    service.next("TASK-1")

    state, _ = service.load("TASK-1")

    record = state.item_executions[state.cursor]
    (resolution,) = [r for r in record.rule_resolutions if r.id == "develop/1"]
    assert (resolution.status, resolution.check, resolution.missing) == (
        "converted",
        "lint",
        None,
    )


def _edit_store(root: Path, change: Callable[[dict], None]) -> None:
    store = _store(root)
    change(store)
    (root / STORE_FILE).write_text(json.dumps(store), encoding="utf-8")


def test_reconverting_a_revoked_check_keeps_its_rejections(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1", "develop/2") == 0
    assert _cli(root, "revoke", "lint", "--reason", "Too slow.", "--yes") == 0

    assert _convert(root, "lint", "develop/2") == 0

    store = _store(root)
    entry = store["rules"][rule_text_hash(CLI)]
    assert entry["status"] == "rejected"
    assert entry["reason"].endswith("Too slow.")
    assert store["checks"]["lint"]["status"] == "converted"
    states = _states(root, capsys)
    assert (states["develop/1"], states["develop/2"]) == ("rejected", "converted")


@pytest.mark.parametrize("path", ["/etc/hosts", "../lint.toml", "conf/../../x"])
def test_convert_refuses_a_config_path_outside_the_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    root = _project(tmp_path)

    assert _convert(root, "lint", "develop/1", config=path) == 1

    assert "must be a relative path inside the project" in capsys.readouterr().err
    assert not (root / STORE_FILE).exists()


def test_convert_normalises_config_paths(tmp_path: Path) -> None:
    root = _project(tmp_path)

    assert _convert(root, "lint", "develop/1", config="./conf//lint.toml") == 0

    assert _store(root)["checks"]["lint"]["config"] == ["conf/lint.toml"]


def test_a_check_left_covering_nothing_is_removed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1") == 0
    assert _convert(root, "names", "develop/2", "develop/3") == 0
    capsys.readouterr()

    assert _convert(root, "other", "develop/1", "develop/2") == 0

    out = capsys.readouterr()
    assert "- removes check lint (converted): it covers nothing more" in out.err
    assert "- takes `develop/2` out of check names" in out.err
    assert "Removed, covering nothing more: check(s) lint." in out.out
    store = _store(root)
    assert "lint" not in store["checks"]
    assert store["checks"]["names"]["covers"] == [rule_text_hash(LOGS)]

    assert _cli(root, "decline", "develop/3", "--reason", "Judgement.", "--yes") == 0

    assert "names" not in _store(root)["checks"]


def test_an_emptied_pending_revision_is_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1", "develop/2") == 0

    def pend(store: dict) -> None:
        check = store["checks"]["lint"]
        check["pending"] = {"shell": "false", "covers": [rule_text_hash(NAMES)]}

    _edit_store(root, pend)
    capsys.readouterr()

    assert _convert(root, "names", "develop/2") == 0

    assert "- drops the pending revision of check lint" in capsys.readouterr().err
    check = _store(root)["checks"]["lint"]
    assert "pending" not in check
    assert check["covers"] == [rule_text_hash(CLI)]


def test_the_preview_reads_an_old_stores_proposal_as_unscriptized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1") == 0

    def propose(store: dict) -> None:
        store["rules"][rule_text_hash(NAMES)] = {
            "text": NAMES,
            "status": "approach_proposed",
            "approach": "grep for it",
            "check": "lint",
        }
        store["checks"]["lint"]["pending"] = {
            "shell": "false",
            "covers": [rule_text_hash(CLI), rule_text_hash(NAMES)],
        }

    _edit_store(root, propose)
    capsys.readouterr()

    assert (
        _cli(
            root,
            "convert",
            "lint",
            "--covers",
            "develop/1",
            "develop/2",
            "--check-shell",
            "true",
            "--dry-run",
        )
        == 0
    )

    out = capsys.readouterr().out
    assert (
        "`develop/1`: Keep the public CLI unchanged. (now: converted by check lint)"
        in (out)
    )
    assert "`develop/2`: Name things clearly. (now: unscriptized)" in out
    assert "warning" not in out
    assert "- drops the pending revision of check lint" in out


def test_a_dry_run_shows_every_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1", "develop/2") == 0
    assert _convert(root, "names", "develop/3") == 0
    before = _store(root)
    capsys.readouterr()

    assert (
        _cli(
            root,
            "convert",
            "lint",
            "--covers",
            "develop/3",
            "--check-shell",
            "true",
            "--dry-run",
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "(replaces the store's entry)" in out
    assert "- takes `develop/3` out of check names" in out
    assert "- removes check names (converted): it covers nothing more" in out
    assert (
        f"- returns rule {rule_text_hash(CLI)[:12]} ({CLI}) to not scriptized"
    ) in out
    assert _store(root) == before


def test_a_dry_run_refuses_a_bad_check_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert (
        _cli(
            root,
            "convert",
            "Bad_Name",
            "--covers",
            "develop/1",
            "--check-shell",
            "true",
            "--dry-run",
        )
        == 1
    )

    out = capsys.readouterr()
    assert "must be kebab-case" in out.err
    assert "Check Bad_Name" not in out.out


def test_convert_and_decline_report_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1", "develop/2") == 0
    capsys.readouterr()

    assert (
        _cli(
            root,
            "convert",
            "lint",
            "--covers",
            "develop/2",
            "--check-shell",
            "true",
            "--yes",
            "--json",
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == {
        "converted": {
            "check": "lint",
            "rules": ["develop/2"],
            "unscriptized": [rule_text_hash(CLI)],
            "moved": [],
            "dropped_checks": [],
            "dropped_revisions": [],
        }
    }

    assert _cli(root, "decline", "develop/2", "--reason", "No.", "--yes", "--json") == 0

    assert json.loads(capsys.readouterr().out) == {
        "declined": {
            "rules": ["develop/2"],
            "unscriptized": [],
            "moved": [{"rule": rule_text_hash(NAMES), "from": "lint"}],
            "dropped_checks": ["lint"],
            "dropped_revisions": [],
        }
    }


def test_the_text_listing_names_each_scriptize_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1") == 0
    assert _convert(root, "names", "develop/2") == 0
    assert _cli(root, "revoke", "names", "--reason", "Wrong.", "--yes") == 0
    capsys.readouterr()

    assert _cli(root) == 0

    out = capsys.readouterr().out
    lines = {
        rule_id: next(line for line in out.splitlines() if f"`{rule_id}`" in line)
        for rule_id in ("develop/1", "develop/2", "develop/3")
    }
    assert "(checked by store check `lint`)" in lines["develop/1"]
    assert "(judged: its check was rejected)" in lines["develop/2"]
    assert "(judged: not scriptized yet)" in lines["develop/3"]


def test_the_step_page_names_the_missing_config_file(tmp_path: Path) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1", config="lint.toml") == 0
    service = WorkflowService(Storage(root))
    service.start(
        "task", "TASK-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )

    step = service.next("TASK-1")

    line = next(rule for rule in step.rules if rule.id == "develop/1")
    assert (line.has_command, line.check, line.missing) == (False, "lint", "lint.toml")
    page = MarkdownOutputAdapter().render_instruction(step)
    assert (
        "  Its check `lint` does not run here: `lint.toml` is missing in this "
        "step's directory."
    ) in page


def test_a_worktree_without_the_config_judges_the_rule(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "ww.yaml").write_text(
        "hooks:\n  before_start_workflow:\n    - workflows: [task]\n"
        "      handlers:\n"
        "        - ext/ww/git/handlers:start-task-branch: ~\n"
        "        - ext/ww/git/handlers:create-worktree: ~\n" + WORKFLOWS,
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(".ww/\ntrees/\nlint.toml\n", encoding="utf-8")
    (root / "ww.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "ww/git": {
                        "separate_branch": True,
                        "base_branches": {"default": "main"},
                        "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
                        "worktrees": True,
                        "worktree_dir": "./trees",
                        "worktree_name_format": "{{ww.task.id}}",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _git("config", "commit.gpgsign", "false", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "worktrees", cwd=root)
    # Built on the project root, as on an unmerged branch: never committed.
    (root / "lint.toml").write_text("", encoding="utf-8")
    assert _convert(root, "lint", "develop/1", config="lint.toml") == 0
    service = WorkflowService(Storage(root))
    service.start(
        "task", "TASK-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )

    service.next("TASK-1")

    state, _ = service.load("TASK-1")
    assert (root / "trees" / "TASK-1").is_dir()
    record = state.item_executions[state.cursor]
    (resolution,) = [r for r in record.rule_resolutions if r.id == "develop/1"]
    assert (resolution.status, resolution.check, resolution.missing) == (
        "judged",
        "lint",
        "lint.toml",
    )


def _dry_convert(root: Path, name: str, *covers: str) -> int:
    return _cli(
        root,
        "convert",
        name,
        "--covers",
        *covers,
        "--check-shell",
        "true",
        "--dry-run",
    )


def test_a_check_left_covering_nothing_is_removed_with_its_pending_revision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1") == 0

    def pend(store: dict) -> None:
        store["checks"]["lint"]["pending"] = {
            "shell": "false",
            "covers": [rule_text_hash(CLI), rule_text_hash(NAMES)],
        }
        store["rules"][rule_text_hash(NAMES)] = {
            "text": NAMES,
            "status": "proposed",
            "check": "lint",
        }

    _edit_store(root, pend)
    capsys.readouterr()

    assert _convert(root, "other", "develop/1") == 0

    assert "- removes check lint (converted): it covers nothing more" in (
        capsys.readouterr().err
    )
    store = _store(root)
    assert "lint" not in store["checks"]
    # The old store's interim entry stays, read as unscriptized.
    assert store["rules"][rule_text_hash(NAMES)]["status"] == "proposed"


def test_removing_a_proposed_check_is_shown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/2") == 0

    def propose(store: dict) -> None:
        store["checks"]["lint"]["status"] = "proposed"
        del store["rules"][rule_text_hash(NAMES)]

    _edit_store(root, propose)
    capsys.readouterr()

    assert _dry_convert(root, "names", "develop/2") == 0

    out = capsys.readouterr().out
    assert "- removes check lint (proposed): it covers nothing more" in out
    assert "warning" not in out


def test_a_changed_check_drops_its_pending_revision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    assert _convert(root, "lint", "develop/1", "develop/2") == 0
    assert _convert(root, "logs", "develop/3") == 0

    def pend(store: dict) -> None:
        store["checks"]["lint"]["pending"] = {
            "shell": "false",
            "covers": [rule_text_hash(CLI)],
        }
        store["checks"]["logs"]["pending"] = {
            "shell": "false",
            "covers": [rule_text_hash(LOGS)],
        }

    _edit_store(root, pend)
    capsys.readouterr()

    # Its own pending revision, replaced by the new command.
    assert _dry_convert(root, "lint", "develop/1", "develop/2") == 0
    assert "- drops the pending revision of check lint" in capsys.readouterr().out

    # Another check that loses a rule loses its revision too.
    assert _dry_convert(root, "names", "develop/1") == 0
    out = capsys.readouterr().out
    assert "- drops the pending revision of check lint" in out
    assert "check logs" not in out
