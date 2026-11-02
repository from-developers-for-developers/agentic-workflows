# SPDX-License-Identifier: GPL-3.0-or-later
"""``readonly: true``: a run that checks the branch against its rules and
changes nothing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ww.config import load_configuration, parse_yaml_text
from ww.errors import ConfigurationError, StateError
from ww.execution_models import PlanItemExecution
from ww.extensions import ExtensionRegistry
from ww.instructions import Instruction
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.plan import compile_workflow_plan
from ww.service import READONLY_FAILURE, WorkflowService
from ww.storage import Storage

# ``review`` carries a judged rule and a group with a checked rule on Markdown
# files, which a readonly step gets only by naming it;
# ``tests`` is a command whose failure would otherwise open a repair.
WORKFLOWS = """rules:
  docs: [rules/docs/]
workflows:
  - name: review
    readonly: true
    steps:
      - name: review
        description: Review the branch.
        rules:
          - Keep the public CLI unchanged.
          - docs
      - name: tests
        argv: [sh, -c, "echo two tests failed; exit 1"]
        on_failure: fix
      - name: report
        description: Report.
"""
HEADER_RULE = """---
paths: ["*.md"]
check:
  shell: grep -L foo $WW_STEP_CHANGED_FILES || true
  assert: [empty]
---
Include "foo" in every Markdown file you change.
"""
GIT_SETTINGS = {"extensions": {"ww/git": {"base_branches": {"default": "main"}}}}


def _git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _project(
    root: Path,
    workflows: str = WORKFLOWS,
    settings: dict[str, object] | None = None,
) -> Path:
    rules = root / "rules/docs"
    rules.mkdir(parents=True)
    (rules / "header.md").write_text(HEADER_RULE, encoding="utf-8")
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    (root / "ww.json").write_text(
        json.dumps(GIT_SETTINGS if settings is None else settings), "utf-8"
    )
    (root / "old.md").write_text("no marker\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", ".", cwd=root)
    _git("config", "user.email", "t@e.st", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)
    return root


def _branch(root: Path) -> None:
    """A feature branch with a committed and an uncommitted Markdown file."""
    _git("checkout", "-qb", "feature", cwd=root)
    (root / "committed.md").write_text("no marker\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "work", cwd=root)
    (root / "uncommitted.md").write_text("foo\n", encoding="utf-8")


def _start(root: Path, workflow: str = "review") -> WorkflowService:
    service = WorkflowService(Storage(root))
    service.start(
        workflow, "R-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )
    return service


def _markdown(instruction: Instruction) -> str:
    return MarkdownOutputAdapter().render_instruction(instruction)


def _record(service: WorkflowService, name: str) -> PlanItemExecution:
    state, snapshot = service.load("R-1")
    index = next(
        index
        for index, item in enumerate(snapshot.plan.items)
        if item.name == name and item.phase == "step"
    )
    return state.item_executions[index]


def _artifact(root: Path, name: str) -> str:
    (path,) = (root / ".ww/tasks/R-1/runs/01-review/steps").glob(f"*-{name}.md")
    return path.read_text(encoding="utf-8")


def _verdict(rule_id: str, verdict: str) -> str:
    if verdict == "pass":
        return json.dumps(
            {"id": rule_id, "status": "judged", "verdict": "pass", "checked": "c"}
        )
    return json.dumps(
        {
            "id": rule_id,
            "status": "judged",
            "verdict": "fail",
            "failures": [{"file": "cli.py", "line": 3, "what": "renamed a flag"}],
        }
    )


def test_readonly_must_be_a_boolean_and_is_frozen_with_the_plan(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="readonly must be true or false"):
        parse_yaml_text(
            "workflows:\n  - name: r\n    readonly: yes please\n    steps: []\n",
            tmp_path / "ww.yaml",
        )
    service = _start(_project(tmp_path))

    _, snapshot = service.load("R-1")

    assert snapshot.plan.readonly
    assert snapshot.plan.to_dict()["readonly"] is True


def test_every_page_says_the_run_is_readonly(tmp_path: Path) -> None:
    service = _start(_project(tmp_path))

    page = service.next("R-1")

    assert page.item_name == "review"
    assert "This run is readonly (`readonly: true`)" in _markdown(page)


def test_a_completion_that_changed_a_file_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _start(root)
    service.next("R-1")
    (root / "old.md").write_text("changed\n", encoding="utf-8")
    (root / "new.txt").write_text("new\n", encoding="utf-8")

    with pytest.raises(StateError, match="readonly.*new.txt, old.md"):
        service.complete("R-1", artifact="Reviewed.", summary_for_next="Done.")

    _git("checkout", "--", "old.md", cwd=root)
    (root / "new.txt").unlink()
    page = service.complete("R-1", artifact="Reviewed.", summary_for_next="Done.")
    assert page.item_name != "review"


def test_rules_apply_to_the_branch_and_failures_are_reported(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _branch(root)
    service = _start(root)
    service.next("R-1")

    held = service.complete("R-1", artifact="Reviewed.", summary_for_next="Done.")

    # The checked rule failed on the committed file, and the step is not
    # sent back: its verifier judges the branch, committed and uncommitted.
    assert held.verification is not None
    assert held.verification.subject == "branch"
    assert held.verification.files == ("committed.md", "uncommitted.md")
    rendered = _markdown(held)
    assert "Files the branch changed (2)" in rendered
    assert "judge what the branch changed" in rendered
    page = service.complete(
        "R-1",
        artifact="One violation.",
        rule_results=(_verdict("review/1", "fail"),),
    )

    record = _record(service, "review")
    assert record.status == "completed"
    assert not any(item.repair_of for item in service.load("R-1")[1].plan.items)
    artifact = _artifact(root, "review")
    assert "- `docs/header`: failed" in artifact
    assert "  - committed.md" in artifact
    assert "- `review/1`: verified fail" in artifact
    assert "cli.py:3 — renamed a flag" in artifact
    # The failing command is reported too, and the run goes on.
    assert page.item_name == "report"
    tests = _record(service, "tests")
    assert tests.status == "failed"
    assert tests.error is not None and READONLY_FAILURE in tests.error


def test_a_selection_may_name_existing_files_checked_whole(tmp_path: Path) -> None:
    workflows = WORKFLOWS.replace(
        "    steps:\n      - name: review\n",
        "    steps:\n      - name: scope\n        description: Scope it.\n"
        "        select_rules_for: [review]\n      - name: review\n",
    )
    root = _project(tmp_path, workflows)
    service = _start(root)
    assert service.next("R-1").item_name == "scope"
    page = service.complete("R-1", artifact="Old docs.", summary_for_next="Old.")
    assert page.item_name == "select-rules"
    assert "--select-files <step>=<glob>,<glob>" in _markdown(page)
    candidates = service.rule_candidates("R-1").steps[0][1]
    assert [(rule.id, rule.has_command) for rule in candidates] == [
        ("review/1", False),
        ("docs/header", True),
    ]

    page = service.complete(
        "R-1",
        artifact="Only the header rule, on the old docs.",
        rule_selection=("review=docs/header",),
        rule_files=("review=*.md",),
    )
    assert page.item_name == "review"
    assert [rule.id for rule in page.rules] == ["docs/header"]
    page = service.complete("R-1", artifact="Read.", summary_for_next="Read.")

    # main has no branch changes, but the named file is checked whole; the
    # left-out judged rule is not judged.
    assert page.item_name == "report"
    artifact = _artifact(root, "review")
    assert "- `docs/header`: failed" in artifact
    assert "  - old.md" in artifact
    assert "- `review/1`: left out (by `scope`)" in artifact


def test_select_files_is_refused_outside_a_readonly_run(tmp_path: Path) -> None:
    workflows = WORKFLOWS.replace("    readonly: true\n", "").replace(
        "    steps:\n      - name: review\n",
        "    steps:\n      - name: scope\n        description: Scope it.\n"
        "        select_rules_for: [review]\n      - name: review\n",
    )
    service = _start(_project(tmp_path, workflows))
    service.next("R-1")
    service.complete("R-1", artifact="Scoped.", summary_for_next="Scoped.")

    with pytest.raises(StateError, match="--select-files names the files a readonly"):
        service.complete(
            "R-1",
            artifact="Why.",
            rule_selection=("review=review/1",),
            rule_files=("review=*.md",),
        )


def test_nothing_to_check_when_the_branch_has_no_changes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    service = _start(root)
    service.next("R-1")

    page = service.complete("R-1", artifact="Reviewed.", summary_for_next="Done.")

    assert page.verification is None
    artifact = _artifact(root, "review")
    assert "- `docs/header`: not applicable" in artifact
    assert "- `review/1`: not applicable" in artifact


def test_without_a_base_branch_only_uncommitted_work_counts(tmp_path: Path) -> None:
    root = _project(tmp_path, settings={})
    _branch(root)
    service = _start(root)
    service.next("R-1")

    held = service.complete("R-1", artifact="Reviewed.", summary_for_next="Done.")

    assert held.verification is not None
    assert held.verification.files == ("uncommitted.md",)


def test_hooks_that_write_are_left_out_and_a_writing_step_is_refused(
    tmp_path: Path,
) -> None:
    hooks = """hooks:
  before_start_workflow:
    - ext/ww/git/handlers:is-git-clean: ~
    - ext/ww/git/handlers:start-task-branch: ~
  before_complete_workflow:
    - ext/ww/git/handlers:git-commit: ~
"""
    root = _project(tmp_path, hooks + WORKFLOWS)

    def plan(text: str) -> tuple[str, ...]:
        (root / "ww.yaml").write_text(text, encoding="utf-8")
        extensions = ExtensionRegistry.discover(root)
        configuration = load_configuration(root / "ww.yaml", extensions)
        compiled = compile_workflow_plan(
            configuration, root, "review", "codex", "R-1", extensions
        )
        return tuple(item.name for item in compiled.items)

    names = plan(hooks + WORKFLOWS)
    assert "is-git-clean" in names
    assert "start-task-branch" not in names and "git-commit" not in names
    assert "start-task-branch" in plan(
        hooks + WORKFLOWS.replace("    readonly: true\n", "")
    )
    with pytest.raises(ConfigurationError, match="is readonly.*which writes"):
        plan(
            WORKFLOWS.replace(
                "      - name: report\n        description: Report.\n",
                "      - ext/ww/git/handlers:git-commit: ~\n",
            )
        )


def test_a_step_may_name_every_rule_group(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        WORKFLOWS.replace(
            "        description: Report.\n",
            "        description: Report.\n        rules: ['*']\n",
        ),
    )
    service = _start(root)
    _, snapshot = service.load("R-1")

    report = next(item for item in snapshot.plan.items if item.name == "report")
    assert [rule.id for rule in report.rules] == ["docs/header"]


@pytest.mark.usefixtures("shipped_builtins")
def test_the_bundled_workflow_checks_the_named_files_whole(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "rules/docs/plain.md").write_text("Write plain English.\n", "utf-8")
    service = _start(root, "ww-check-rules")
    assert service.load("R-1")[1].plan.readonly
    assert service.next("R-1").item_name == "scope"
    service.complete("R-1", artifact="old.md, docs rules.", summary_for_next="Old.")

    page = service.complete(
        "R-1",
        artifact="The request names old.md.",
        rule_selection=("check=docs/header,docs/plain",),
        rule_files=("check=old.md",),
    )
    assert page.item_name == "check"
    held = service.complete("R-1", artifact="old.md.", summary_for_next="Read.")

    assert held.verification is not None
    assert held.verification.subject == "files"
    assert held.verification.diff_command is None
    rendered = _markdown(held)
    assert "Files to judge, each whole (1)" in rendered
    assert "judge the listed files as they are" in rendered
    service.complete(
        "R-1", artifact="Fine.", rule_results=(_verdict("docs/plain", "pass"),)
    )
    artifact = (next((root / ".ww/tasks/R-1/runs").iterdir()) / "steps").glob(
        "*-check.md"
    )
    text = next(artifact).read_text(encoding="utf-8")
    assert "- `docs/header`: failed" in text
    assert "- `docs/plain`: verified pass" in text
