# SPDX-License-Identifier: GPL-3.0-or-later
"""Recording scriptized checks outside a task: ``rules convert`` and ``decline``."""

from __future__ import annotations

import json
import subprocess
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
    assert (rule.state, rule.check, rule.missing) == ("judged", "lint", "lint.toml")
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
