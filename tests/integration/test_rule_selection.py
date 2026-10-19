# SPDX-License-Identifier: GPL-3.0-or-later
"""``select_rules_for``: an earlier step chooses the judged rules a later one gets."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.cli import main
from ww.errors import ConfigurationError, StateError
from ww.execution_models import PlanItemExecution
from ww.instructions import Instruction
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.rule_stats import RuleStatsStore
from ww.service import WorkflowService
from ww.storage import Storage

# ``architect`` chooses for ``develop``, which carries two unscoped judged
# rules, a judged rule scoped to ``api/``, and a checked rule.
WORKFLOWS = """rules:
  docs: [rules/docs/]
workflows:
  - name: task
    steps:
      - name: architect
        description: Plan it.
        select_rules_for: [develop]
      - name: develop
        description: Develop it.
        rules:
          - Keep the public CLI unchanged.
          - Log every retry.
      - check: Check it.
"""
API_RULE = '---\npaths: ["api/*.py"]\n---\nDocument every public API function.\n'
HEADER_RULE = """---
paths: ["*.md"]
check:
  shell: grep -L foo $WW_STEP_CHANGED_FILES || true
  assert: [empty]
---
Include "foo" in every Markdown file you change.
"""


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _project(root: Path, workflows: str = WORKFLOWS) -> Path:
    rules = root / "rules/docs"
    rules.mkdir(parents=True)
    (rules / "api.md").write_text(API_RULE, encoding="utf-8")
    (rules / "header.md").write_text(HEADER_RULE, encoding="utf-8")
    (root / "ww.yaml").write_text(workflows, encoding="utf-8")
    _git("init", "-q", "-b", "main", ".", cwd=root)
    _git("config", "user.email", "t@e.st", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)
    return root


def _selecting(root: Path) -> tuple[WorkflowService, Instruction]:
    """The task after ``architect`` completed: its rule selection is open."""
    service = WorkflowService(Storage(root))
    service.start(
        "task", "TASK-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )
    assert service.next("TASK-1").item_name == "architect"
    page = service.complete("TASK-1", artifact="Planned.", summary_for_next="Plan.")
    return service, page


def _select(service: WorkflowService, *entries: str) -> Instruction:
    return service.complete(
        "TASK-1", artifact="Only the CLI matters here.", rule_selection=entries
    )


def _markdown(instruction: Instruction) -> str:
    return MarkdownOutputAdapter().render_instruction(instruction)


def _record(service: WorkflowService, name: str) -> PlanItemExecution:
    state, snapshot = service.load("TASK-1")
    index = next(
        index
        for index, item in enumerate(snapshot.plan.items)
        if item.name == name and item.phase == "step"
    )
    return state.item_executions[index]


def _verify(service: WorkflowService, *rule_ids: str) -> Instruction:
    return service.complete(
        "TASK-1",
        artifact="Verified.",
        rule_results=tuple(
            json.dumps(
                {"id": rule_id, "status": "judged", "verdict": "pass", "checked": "c"}
            )
            for rule_id in rule_ids
        ),
    )


def test_the_selecting_worker_gets_a_short_selection_page(tmp_path: Path) -> None:
    service, page = _selecting(_project(tmp_path))

    assert page.item_name == "select-rules"
    rendered = _markdown(page)
    assert "Choose the judged rules that `develop` will get" in rendered
    assert "rules candidates TASK-1" in rendered
    assert "--select-rules develop='<rule IDs, comma-separated; none: empty>'" in (
        rendered
    )
    candidates = service.rule_candidates("TASK-1")
    assert [step for step, _ in candidates.steps] == ["develop"]
    # The checked rule always runs, so it is no candidate.
    assert [rule.id for rule in candidates.steps[0][1]] == [
        "docs/api",
        "develop/1",
        "develop/2",
    ]


def test_in_auto_the_architects_worker_carries_on_into_the_selection(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service = WorkflowService(Storage(root))
    service.start(
        "task", "TASK-1", agent="codex", workflow_runtime="auto", init_artifact="Do."
    )
    assert service.next("TASK-1", caller_role="manager").item_name == "architect"
    token = assignment_token(service, "TASK-1")

    page = service.complete(
        "TASK-1",
        artifact="Planned.",
        summary_for_next="Plan.",
        caller_role="worker",
        assignment=token,
    )

    assert page.item_name == "select-rules"
    assert page.next_role == "worker"
    assert assignment_token(service, "TASK-1") == token
    handed_back = service.complete(
        "TASK-1",
        artifact="Why.",
        rule_selection=("develop=develop/1",),
        caller_role="worker",
        assignment=token,
    )
    assert handed_back.next_role == "manager"


def test_a_selection_is_required_explicit_and_among_the_candidates(
    tmp_path: Path,
) -> None:
    service, _ = _selecting(_project(tmp_path))

    with pytest.raises(StateError, match="--select-rules is missing for 'develop'"):
        _select(service)
    with pytest.raises(StateError, match="not among its candidates"):
        _select(service, "develop=docs/header")
    with pytest.raises(StateError, match="write <step>=<id>"):
        _select(service, "review=develop/1")
    with pytest.raises(StateError, match="needs --artifact"):
        service.complete("TASK-1", rule_selection=("develop=",))

    page = _select(service, "develop=")

    assert page.item_name == "develop"
    assert page.rules_left_out == 3


def test_the_target_gets_and_is_verified_against_only_the_selection(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service, _ = _selecting(root)

    page = _select(service, "develop=develop/1")

    assert page.item_name == "develop"
    assert [rule.id for rule in page.rules] == ["docs/header", "develop/1"]
    assert (page.rules_left_out, page.rules_left_out_by) == (2, "architect")
    assert "`architect` left out 2 more judged rules" in _markdown(page)
    (root / "app.py").write_text("x\n", encoding="utf-8")
    assert service.check("TASK-1").judged_left_out == ("docs/api", "develop/2")
    held = service.complete("TASK-1", artifact="Done.", summary_for_next="Done.")
    assert [rule.id for rule in held.verification.rules] == ["develop/1"]

    _verify(service, "develop/1")

    record = _record(service, "develop")
    assert record.status == "completed"
    assert record.rules_left_out == ("docs/api", "develop/2")
    artifact = (root / ".ww/tasks/TASK-1/runs/01-task/steps/03-develop.md").read_text(
        encoding="utf-8"
    )
    assert "left out" in artifact and "by `architect`" in artifact
    stats = RuleStatsStore(service.storage).load()
    assert (stats["develop/2"].left_out, stats["develop/1"].judged) == (1, 1)


def test_a_left_out_rule_whose_scope_the_change_reaches_is_judged(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    service, _ = _selecting(root)
    _select(service, "develop=develop/1")
    (root / "api").mkdir()
    (root / "api/v1.py").write_text("x\n", encoding="utf-8")

    held = service.complete("TASK-1", artifact="Done.", summary_for_next="Done.")

    assert [rule.id for rule in held.verification.rules] == ["docs/api", "develop/1"]
    assert _record(service, "develop").rules_left_out == ("develop/2",)


def test_the_selection_command_lists_and_completes_on_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    _selecting(root)

    assert main(["--root", str(root), "rules", "candidates", "TASK-1"]) == 0
    out = capsys.readouterr().out
    assert "## `develop`" in out
    assert "- `docs/api` — api/*.py — Document every public API function." in out
    assert (
        main(
            [
                "--root",
                str(root),
                "complete",
                "TASK-1",
                "--artifact",
                "Why.",
                "--select-rules",
                "develop=develop/1,develop/2",
            ]
        )
        == 0
    )
    selection = _record(WorkflowService(Storage(root)), "develop")
    assert selection.status == "in_progress"


@pytest.mark.parametrize(
    ("targets", "message"),
    [
        ("[review]", "'review' is not a later agent step"),
        ("[architect]", "'architect' is not a later agent step"),
    ],
)
def test_a_target_must_be_one_later_agent_step(
    tmp_path: Path, targets: str, message: str
) -> None:
    root = _project(
        tmp_path,
        WORKFLOWS.replace(
            "select_rules_for: [develop]", f"select_rules_for: {targets}"
        ),
    )
    service = WorkflowService(Storage(root))

    with pytest.raises(ConfigurationError, match=message):
        service.start("task", "TASK-1", agent="codex", init_artifact="Do.")


def test_lint_rejects_a_target_that_is_no_later_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(
        tmp_path,
        WORKFLOWS.replace("select_rules_for: [develop]", "select_rules_for: [review]"),
    )

    assert main(["--root", str(root), "lint"]) == 1

    assert (
        "'review' is not a later agent step of the workflow" in capsys.readouterr().err
    )


def test_a_target_in_an_assessment_outcome_is_found_by_its_name(
    tmp_path: Path,
) -> None:
    workflows = WORKFLOWS.replace(
        """      - name: develop
        description: Develop it.
        rules:
          - Keep the public CLI unchanged.
          - Log every retry.
""",
        """      - assess:
          question: Is it big?
          outcomes:
            positive:
              steps:
                - name: develop
                  description: Develop it.
                  rules:
                    - Keep the public CLI unchanged.
            negative:
              stop_workflow: true
""",
    )
    service, _ = _selecting(_project(tmp_path, workflows))

    page = _select(service, "develop=")

    assert page.item_name == "assess"


def test_a_target_with_an_attached_assessment_names_the_step_itself(
    tmp_path: Path,
) -> None:
    # ``develop`` also holds its outcome's ``fix``; its name is one step.
    workflows = WORKFLOWS.replace(
        """          - Log every retry.
""",
        """          - Log every retry.
        assess:
          assess: Are there issues?
          positive:
            steps:
              - name: fix
                description: Fix them.
                rules:
                  - Keep fixes small.
""",
    )
    service, _ = _selecting(_project(tmp_path, workflows))

    page = _select(service, "develop=")

    assert page.item_name == "develop"
    assert [rule.id for rule in page.rules] == ["docs/header"]
    assert page.rules_left_out == 3


def test_without_select_rules_for_the_step_gets_every_rule(tmp_path: Path) -> None:
    root = _project(
        tmp_path, WORKFLOWS.replace("        select_rules_for: [develop]\n", "")
    )
    service = WorkflowService(Storage(root))
    service.start(
        "task", "TASK-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )
    service.next("TASK-1")

    page = service.complete("TASK-1", artifact="Planned.", summary_for_next="Plan.")

    assert page.item_name == "develop"
    assert page.rules_left_out == 0
    assert [rule.id for rule in page.rules] == [
        "docs/api",
        "docs/header",
        "develop/1",
        "develop/2",
    ]


# TASK-47: ``develop`` is a parent with substeps; only those with judged rules
# are targets.
PARENT_WORKFLOWS = """workflows:
  - name: task
    steps:
      - name: architect
        description: Plan it.
        select_rules_for: [develop]
      - name: develop
        steps:
          - name: plan
            description: Plan the change.
          - name: implement
            description: Implement it.
            rules:
              - Keep the public CLI unchanged.
          - name: write-tests
            description: Write the tests.
            rules:
              - Name every test after its behaviour.
"""


def test_a_parent_target_expands_to_its_substeps_with_judged_rules(
    tmp_path: Path,
) -> None:
    service, page = _selecting(_project(tmp_path, PARENT_WORKFLOWS))

    assert page.item_name == "select-rules"
    candidates = service.rule_candidates("TASK-1")
    assert [step for step, _ in candidates.steps] == ["implement", "write-tests"]
    rendered = _markdown(page)
    assert "Choose the judged rules that `implement`, `write-tests` will get" in (
        rendered
    )
    assert "--select-rules implement=" in rendered
    assert "--select-rules write-tests=" in rendered


def test_a_parent_target_reaches_substeps_at_any_depth(tmp_path: Path) -> None:
    workflows = """workflows:
  - name: task
    steps:
      - name: architect
        description: Plan it.
        select_rules_for: [develop]
      - name: develop
        steps:
          - name: implement
            description: Implement it.
            rules:
              - Keep the public CLI unchanged.
          - name: verify
            steps:
              - name: write-tests
                description: Write the tests.
                rules:
                  - Name every test after its behaviour.
"""
    service, _ = _selecting(_project(tmp_path, workflows))

    candidates = service.rule_candidates("TASK-1")
    assert [step for step, _ in candidates.steps] == ["implement", "write-tests"]


def test_a_parent_target_without_judged_rules_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflows = """workflows:
  - name: task
    steps:
      - name: architect
        description: Plan it.
        select_rules_for: [develop]
      - name: develop
        steps:
          - name: plan
            description: Plan the change.
"""
    root = _project(tmp_path, workflows)

    with pytest.raises(ConfigurationError, match="no step inside 'develop'"):
        WorkflowService(Storage(root)).start(
            "task", "TASK-1", agent="codex", init_artifact="Do."
        )
    assert main(["--root", str(root), "lint"]) == 1
    assert "no step inside 'develop'" in capsys.readouterr().err


def test_lint_rejects_a_target_that_starting_the_workflow_rejects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # ``implement`` names two steps: starting refuses it, so lint must too.
    workflows = (
        PARENT_WORKFLOWS.replace(
            "select_rules_for: [develop]", "select_rules_for: [implement]"
        )
        + """      - name: polish
        steps:
          - name: implement
            description: Polish it.
"""
    )
    root = _project(tmp_path, workflows)
    with pytest.raises(ConfigurationError, match="names several steps"):
        WorkflowService(Storage(root)).start(
            "task", "TASK-1", agent="codex", init_artifact="Do."
        )

    assert main(["--root", str(root), "lint"]) == 1
    assert "names several steps" in capsys.readouterr().err


# The ``process-pull-request`` shape: inside one items body, ``analyze``
# chooses the rules ``fix`` gets. The body is compiled once over the
# collection, so it selects as any step does.
IN_ITEMS = """rules:
  docs: [rules/docs/]
workflows:
  - name: task
    steps:
      - name: comments
        description: Register one item per comment.
        items:
          steps:
            - name: analyze
              description: Analyze the comments.
              select_rules_for: [fix]
            - name: fix
              description: Fix them.
              rules:
                - Keep the public CLI unchanged.
                - Log every retry.
      - check: Check it.
"""


def test_a_step_inside_items_chooses_for_a_later_step_of_the_same_body(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, IN_ITEMS)
    assert main(["--root", str(root), "lint"]) == 0
    service = WorkflowService(Storage(root))
    service.start(
        "task", "TASK-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )
    assert service.next("TASK-1").item_name == "comments"
    assert (
        service.complete("TASK-1", artifact="None.", summary_for_next="None.").item_name
        == "analyze"
    )
    page = service.complete("TASK-1", artifact="Read.", summary_for_next="Read.")
    assert page.item_name == "select-rules"

    fix = _select(service, "fix=fix/1")

    assert fix.item_name == "fix"
    assert [rule.id for rule in fix.rules] == ["docs/header", "fix/1"]
    assert fix.rules_left_out_by == "analyze"
    (root / "app.py").write_text("x\n", encoding="utf-8")
    held = service.complete("TASK-1", artifact="Fixed.", summary_for_next="Fixed.")
    assert held.verification is not None
    assert [rule.id for rule in held.verification.rules] == ["fix/1"]


def test_a_step_before_items_chooses_for_a_step_inside_them(
    tmp_path: Path,
) -> None:
    workflows = IN_ITEMS.replace("              select_rules_for: [fix]\n", "").replace(
        "    steps:\n      - name: comments\n",
        "    steps:\n      - name: architect\n        description: Plan it.\n"
        "        select_rules_for: [fix]\n      - name: comments\n",
    )
    root = _project(tmp_path, workflows)

    assert main(["--root", str(root), "lint"]) == 0
    service, page = _selecting(root)
    assert page.item_name == "select-rules"
    assert _select(service, "fix=fix/2").item_name == "comments"
    service.complete("TASK-1", artifact="None.", summary_for_next="None.")
    fix = service.complete("TASK-1", artifact="Read.", summary_for_next="Read.")
    assert fix.item_name == "fix"
    assert [rule.id for rule in fix.rules] == ["docs/header", "fix/2"]


def test_a_step_inside_children_still_cannot_select_rules(tmp_path: Path) -> None:
    workflows = """workflows:
  - name: task
    steps:
      - name: split
        description: Split it.
        children:
          steps:
            - name: refine
              description: Refine it.
              select_rules_for: [review]
            - implement:
                workflow: plain
            - name: review
              description: Review it.
              rules:
                - Log every retry.
  - name: plain
    steps:
      - implement: Implement it.
"""
    root = _project(tmp_path, workflows)

    with pytest.raises(ConfigurationError, match="inside children cannot select"):
        WorkflowService(Storage(root)).start(
            "task", "TASK-1", agent="codex", init_artifact="Do."
        )
