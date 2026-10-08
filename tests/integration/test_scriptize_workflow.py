# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww-scriptize-rules``: scriptizing rules as a project task of its own."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration.test_git_extension import _run, branch_of
from tests.workflow_helpers import start_after_init
from ww.builtin_workflows import builtin_workflow, is_builtin
from ww.cli import main
from ww.config import load_configuration
from ww.defaults import SKILLS
from ww.errors import ConfigurationError
from ww.extensions import ExtensionRegistry
from ww.plan import compile_workflow_plan
from ww.project_config import load_project_config
from ww.service import WorkflowService
from ww.storage import Storage
from ww.workflow_config import StepDefinition

NAME = "ww-scriptize-rules"
LANE_HOOK = """hooks:
  before_start_workflow:
    - workflows: [task]
      argv: [sh, -c, "echo lane > lane-hook.txt"]
workflows:
  - name: task
    steps:
      - develop: Do it.
"""

pytestmark = pytest.mark.usefixtures("shipped_builtins")


def _project(root: Path, settings: dict[str, object] | None = None) -> Path:
    (root / "ww.yaml").write_text(LANE_HOOK, encoding="utf-8")
    (root / "ww.json").write_text(json.dumps(settings or {}), encoding="utf-8")
    return root


def _steps(steps: tuple[StepDefinition, ...]) -> Iterator[StepDefinition]:
    for step in steps:
        yield step
        yield from _steps(step.child_steps)
        yield from _steps(step.assessment_outcomes)


def _step(name: str) -> StepDefinition:
    return next(s for s in _steps(builtin_workflow(NAME).steps) if s.name == name)


def _start(root: Path, task_id: str = "S-1") -> WorkflowService:
    service = WorkflowService(Storage(root))
    service.start(
        NAME, task_id, agent="codex", workflow_runtime="single", init_artifact="All."
    )
    return service


def test_the_workflow_collects_agrees_builds_and_records() -> None:
    workflow = builtin_workflow(NAME)
    names = [step.name for step in _steps(workflow.steps)]

    assert [name for name in names if name != "assess"] == [
        "collect",
        "approaches",
        "scriptize",
        "build",
        "checks",
        "record",
    ]
    assert not workflow.needs_hooks_from and workflow.hooks_from is None
    assert workflow.restartable
    assert _step("approaches").interactive and _step("checks").interactive
    assert [c.label for c in _step("approaches").choices] == ["build", "nothing to do"]
    collect = _step("collect").description
    for piece in (
        "rules --json",
        "`unscriptized`",
        "`check_guidance`",
        "fewest",
        "`WW_STEP_CHANGED_FILES`",
        "never compares the branch with a base branch (`git diff master...`)",
    ):
        assert piece in collect, piece
    build = _step("build").description
    for piece in (
        "deliberately violating input",
        "a sample of real files",
        "real violations do not block",
        "baseline",
        "config files",
        # A check acts on the step's change set, never the repository.
        "`printf '%s\\n' \"$WW_STEP_CHANGED_FILES\" | xargs -d '\\n' <tool>`",
        '`os.environ["WW_STEP_CHANGED_FILES"]` on newlines',
        "or scans the whole repository",
        "`WW_STEP_CHANGED_FILES` set to just that file",
        "an unrelated clean file",
    ):
        assert piece in build, piece
    checks = " ".join(_step("checks").description.split())
    assert "rules convert <check> --covers" in checks
    assert "--check-argv -- <arg>...)" in checks
    assert "rules decline <rule-id>... --reason" in checks
    assert "--proven --dry-run" in checks and "--yes (" not in checks
    assert "Record nothing in this step" in checks
    assert [c.label for c in _step("checks").choices] == ["record", "none"]
    record = _step("record")
    assert not record.interactive and record.artifact_dependency == "checks"
    recorded = " ".join(record.description.split())
    assert "`--yes` in the place of `--dry-run`" in checks
    assert "never at the end" in checks
    assert "where its preview had `--dry-run`" in recorded
    assert "Add nothing after `--check-argv --`" in recorded
    # The previewed command keeps --dry-run ahead of the argv, where --yes goes.
    template = checks[checks.index("rules convert <check>") :]
    assert template.index("--dry-run") < template.index("--check-argv --")
    assert "never edit `ww-rule-automation.json` yourself" in recorded
    assert "commit that file in the main checkout" in recorded
    assert "`is-git-clean`" in recorded


def test_it_has_its_own_git_lifecycle() -> None:
    workflow = builtin_workflow(NAME)
    assert is_builtin(workflow)
    assert [hook.handler.name for hook in workflow.hooks] == [
        "ext/ww/git/handlers:is-git-clean",
        "ext/ww/git/handlers:start-task-branch",
        "ext/ww/git/handlers:create-worktree",
        "ext/ww/git/handlers:git-commit",
        "ext/ww/git/handlers:return-to-base-branch",
    ]


@pytest.mark.parametrize("worktrees", [False, True])
def test_it_starts_from_default_without_borrowing_lane_hooks(
    tmp_path: Path, worktrees: bool
) -> None:
    root = _project(
        tmp_path,
        {
            "extensions": {
                "ww/git": {
                    "base_branches": {"default": "main", NAME: "other"},
                    "separate_branch": False,
                    "worktrees": worktrees,
                    "worktree_dir": "wt",
                }
            }
        },
    )
    _run("git", "init", "-q", "-b", "main", ".", cwd=root)
    _run("git", "config", "user.email", "t@e.st", cwd=root)
    _run("git", "config", "user.name", "Test", cwd=root)
    (root / ".gitignore").write_text(".ww/\nwt/\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=root)
    _run("git", "commit", "-qm", "seed", cwd=root)
    base = _run("git", "rev-parse", "main", cwd=root).stdout.strip()
    _run("git", "switch", "-c", "other", cwd=root)
    (root / "other.txt").write_text("other branch\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=root)
    _run("git", "commit", "-qm", "other", cwd=root)

    service = _start(root)
    page = service.next("S-1")
    assert page.item_name == "collect"
    workspace = root / "wt" / "S-1" if worktrees else root
    assert branch_of(workspace) == "s-1"
    assert _run("git", "rev-parse", "HEAD", cwd=workspace).stdout.strip() == base
    assert not (workspace / "other.txt").exists()
    assert not (workspace / "lane-hook.txt").exists()
    if worktrees:
        assert branch_of(root) == "other"


@pytest.mark.parametrize("source", ["task", "", 3])
def test_the_old_lane_setting_is_refused(tmp_path: Path, source: object) -> None:
    path = tmp_path / "ww.json"
    path.write_text(json.dumps({"workflows": {NAME: {"hooks_from": source}}}))
    with pytest.raises(ConfigurationError, match=r"unknown key\(s\): hooks_from"):
        load_project_config(path)


def test_record_transition_shares_commit_message_with_project_hook(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path, {"extensions": {"ww/git": {"base_branches": {"default": "main"}}}}
    )
    path = root / "ww.yaml"
    path.write_text(
        path.read_text().replace(
            "hooks:\n",
            "hooks:\n  before_complete_workflow:\n"
            "    - ext/ww/git/handlers:git-commit: ~\n",
            1,
        ),
        encoding="utf-8",
    )
    _run("git", "init", "-q", "-b", "main", ".", cwd=root)
    _run("git", "config", "user.email", "t@e.st", cwd=root)
    _run("git", "config", "user.name", "Test", cwd=root)
    (root / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    _run("git", "add", "-A", cwd=root)
    _run("git", "commit", "-qm", "seed", cwd=root)
    service = _start(root)
    for name in ("collect", "approaches", "assess", "build", "checks", "assess"):
        page = service.next("S-1")
        assert page.item_name == name
        if name in {"approaches", "checks"}:
            service.interact(
                "S-1",
                transcript="Agent: Approve?\nOperator: Looks good, continue.",
                choice="build" if name == "approaches" else "record",
                end=True,
            )
        service.complete("S-1", artifact=f"Finished {name}.", summary_for_next="Done.")
        if name == "assess":
            page = service.next("S-1", outcome="positive")

    page = service.next("S-1")
    assert page.item_name == "record"
    assert [value.name for value in page.required_values] == ["commit_message"]
    # Completing record feeds the same input to both automatic commit hooks.
    (root / "check-config.json").write_text("{}\n", encoding="utf-8")
    service.complete(
        "S-1",
        (("commit_message", "Add check configuration"),),
        artifact="Recorded checks.",
        summary_for_next="Recorded.",
    )
    assert branch_of(root) == "main"
    assert _run("git", "show", "s-1:check-config.json", cwd=root).stdout == "{}\n"
    summary = service.next("S-1")
    assert summary.item_name == "update-workflow-summary"
    done = service.complete("S-1", (("summary", "Scriptized."),), artifact="Done.")
    assert done.status == "completed"


def test_a_configured_workflow_may_take_a_lanes_hooks(tmp_path: Path) -> None:
    root = tmp_path
    (root / "ww.yaml").write_text(
        LANE_HOOK
        + "  - name: chores\n    hooks_from: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(root))

    service.start(
        "chores", "C-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )

    assert (root / "lane-hook.txt").exists()


def test_the_skill_starts_the_workflow() -> None:
    skill = SKILLS["ww-scriptize"]

    assert skill.startswith("---\nname: ww-scriptize\ndescription: ")
    assert f"--workflow {NAME} " in skill
    prose = " ".join(skill.split())
    assert "Never edit ww's configuration files yourself" in prose
    assert "from `WW_STEP_CHANGED_FILES` and examines only those" in prose


def test_discover_and_the_catalog_list_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    assert main(["--root", str(root), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert NAME in [w["name"] for w in report["builtin_workflows"]]
    assert main(["--root", str(root), "workflows"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert NAME in [w["name"] for w in catalog["workflows"]]


CHORES = """hooks:
  before_start_workflow:
    - workflows: [task]
      argv: [sh, -c, "echo lane > lane-hook.txt"]
    - workflows: [chores]
      argv: [sh, -c, "echo own > own-hook.txt"]
  before_complete:
    - workflows: [task]
      argv: ["true"]
      on_failure: fix
workflows:
  - name: task
    steps:
      - develop: Do it.
  - name: chores
    hooks_from: task
    steps:
      - work: Work.
  - name: heir
    inherit: chores
"""


def _service(root: Path, config: str) -> WorkflowService:
    (root / "ww.yaml").write_text(config, encoding="utf-8")
    return WorkflowService(Storage(root))


@pytest.mark.parametrize("workflow", ["chores", "heir"])
def test_a_workflow_with_a_lane_keeps_its_own_hooks(
    tmp_path: Path, workflow: str
) -> None:
    service = _service(tmp_path, CHORES)

    service.start(
        workflow, "C-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )

    assert (tmp_path / "lane-hook.txt").read_text().strip() == "lane"
    assert (tmp_path / "own-hook.txt").read_text().strip() == "own"


def test_a_fix_hook_reaches_a_workflow_through_its_lane(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(CHORES, encoding="utf-8")
    configuration = load_configuration(
        tmp_path / "ww.yaml", ExtensionRegistry.discover(tmp_path)
    )

    plan = compile_workflow_plan(configuration, tmp_path, "chores", "codex", "C-1")

    (work,) = (item for item in plan.items if item.name == "work")
    assert [check.source for check in work.checks] == ["hook"]
    assert plan.hooks_from == "task" and plan.lane == "task"


def test_a_handoff_workflow_with_a_lane_must_end_with_its_handoff(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """hooks:
  after_complete:
    - workflows: [task]
      argv: ["true"]
workflows:
  - name: task
    steps:
      - develop: Do it.
  - name: choose
    hooks_from: task
    steps:
      - name: select
        handoff_to: task
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="must end with its handoff_to"):
        load_configuration(tmp_path / "ww.yaml", ExtensionRegistry.discover(tmp_path))


NEEDS_LANE = """workflows:
  - name: task
    steps:
      - develop: Do it.
  - name: chores
    needs_hooks_from: true
    steps:
      - work: Work.
"""
CONFIGURED_REFUSAL = "name that lane with hooks_from in its definition in ww.yaml"


def test_a_configured_workflow_needing_a_lane_names_ww_yaml(tmp_path: Path) -> None:
    service = _service(tmp_path, NEEDS_LANE)

    with pytest.raises(ConfigurationError, match=CONFIGURED_REFUSAL):
        service.start("chores", "C-1", agent="codex", init_artifact="Do.")


def test_a_handoff_to_a_workflow_needing_a_lane_is_refused(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        NEEDS_LANE
        + """  - name: choose
    steps:
      - name: select
        handoff_to: chores
""",
    )
    start_after_init(service, "choose", "C-1", agent="codex")

    with pytest.raises(ConfigurationError, match=CONFIGURED_REFUSAL):
        service.next("C-1")

    assert service.tasks.read_handoff("C-1") is None


def test_a_bootstrap_start_needing_a_lane_opens_no_request(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        """workflows:
  - name: chores
    needs_hooks_from: true
    steps:
      - name: create
        description: Create the issue.
        variables:
          - name: task_id
      - work: Work.
""",
    )

    with pytest.raises(ConfigurationError, match=CONFIGURED_REFUSAL):
        service.start("chores", None, agent="codex", init_artifact="Do.")

    assert not (service.storage.runtime_path / "bootstrap").exists()


def test_a_replan_that_drops_a_needed_lane_is_refused(tmp_path: Path) -> None:
    lane = NEEDS_LANE.replace(
        "    needs_hooks_from: true\n",
        "    needs_hooks_from: true\n    hooks_from: task\n",
    )
    service = _service(tmp_path, lane)
    start_after_init(service, "chores", "C-1", agent="codex")

    service = _service(tmp_path, NEEDS_LANE)
    change = service.plan_change("C-1")

    assert change is not None and change.refusal is not None
    assert CONFIGURED_REFUSAL in change.refusal


@pytest.mark.parametrize(
    ("local", "listed"),
    [({"enabled": True}, True), ({"enabled": False}, False)],
)
def test_levels_merge_enabled(
    tmp_path: Path, local: dict[str, object], listed: bool
) -> None:
    root = _project(tmp_path, {"workflows": {NAME: {"enabled": True}}})
    (root / "ww.local.json").write_text(
        json.dumps({"workflows": {NAME: local}}), encoding="utf-8"
    )

    settings = load_project_config(root / "ww.json")
    configuration = load_configuration(
        root / "ww.yaml", ExtensionRegistry.discover(root)
    )

    assert settings.workflow_enabled(NAME) is listed
    workflow = configuration.workflows_by_name.get(NAME)
    assert (workflow is not None) is listed
    assert workflow is None or workflow.hooks_from is None


def test_a_task_id_claim_is_asked_under_the_lane(tmp_path: Path) -> None:
    (tmp_path / "ww.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "ww/git": {
                        "worktrees": True,
                        "worktree_dir": "wt",
                        "worktree_name_format": "{{ww.task.lane}}-{{ww.task.id}}",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    service = _service(tmp_path, CHORES)
    (tmp_path / "wt" / "task-C-1").mkdir(parents=True)

    assert service._configured_lane("chores") == "task"
    assert service._configured_lane("heir") == "task"
    assert service._configured_lane("gone") == "gone"
    assert service._task_exists("C-1", "chores")
    assert not service._task_exists("C-2", "chores")


JUDGED = LANE_HOOK.replace(
    "      - develop: Do it.\n",
    "      - name: develop\n"
    "        description: Do it.\n"
    "        rules:\n"
    "          - Keep the public CLI unchanged.\n"
    "          - Name things clearly.\n",
)
NOTICE = "2 declared rules have no check yet, so a verifier judges them in every step."


def _judged(root: Path, settings: dict[str, object] | None = None) -> Path:
    _project(root, settings)
    (root / "ww.yaml").write_text(JUDGED, encoding="utf-8")
    return root


def _discover(root: Path, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["--root", str(root), "discover"]) == 0
    return capsys.readouterr().out


def _first_page(root: Path, workflow: str, capsys: pytest.CaptureFixture[str]) -> str:
    assert (
        main(
            [
                "--root",
                str(root),
                "start",
                "S-1",
                "--workflow",
                workflow,
                "--agent",
                "codex",
                "--runtime",
                "single",
                "--requirements",
                "Do.",
                "--role",
                "manager",
            ]
        )
        == 0
    )
    return capsys.readouterr().out


def test_discover_and_start_name_the_rules_without_a_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _judged(tmp_path)

    discovered = _discover(root, capsys)
    page = _first_page(root, "task", capsys)

    assert main(["--root", str(root), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    # Concise discovery leaves the suggestion to the JSON and the step pages.
    assert "declared rule" not in discovered
    assert "## Rules" not in discovered
    for text in (report["rules_notice"], page):
        assert NOTICE in text
        assert (
            f"The `ww-scriptize` skill starts `{NAME}`, which builds checks for "
            "them with the operator."
        ) in text
        assert "hooks_from" not in text
    assert main(["--root", str(root), "next", "S-1", "--role", "manager"]) == 0
    assert NOTICE not in capsys.readouterr().out


def test_no_notice_while_the_workflow_is_switched_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _judged(tmp_path, {"workflows": {NAME: {"enabled": False}}})

    assert "declared rule" not in _discover(root, capsys)
    assert "declared rule" not in _first_page(root, "task", capsys)


def test_lint_lists_the_rules_without_suggesting_a_switched_off_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _judged(tmp_path, {"workflows": {NAME: {"enabled": False}}})

    assert main(["--root", str(root), "lint"]) == 0

    out = capsys.readouterr().out
    assert "Warning: 2 rules have no check yet (develop/1, develop/2).\n" in out
    assert NAME not in out


def test_discover_and_start_ignore_a_store_that_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    (root / "ww.yaml").write_text(
        JUDGED.replace("    steps:\n", "    steps:\n      - plan: Plan it.\n"),
        encoding="utf-8",
    )
    (root / "ww-rule-automation.json").write_text("{not json", encoding="utf-8")

    assert "## Rules" not in _discover(root, capsys)
    assert (
        main(
            [
                "--root",
                str(root),
                "start",
                "S-1",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--runtime",
                "single",
                "--requirements",
                "Do.",
                "--role",
                "manager",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["rules_notice"] is None


def test_the_notice_follows_the_count_of_one_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _judged(tmp_path)
    assert (
        main(
            [
                "--root",
                str(root),
                "rules",
                "decline",
                "develop/1",
                "--reason",
                "Judgement.",
                "--yes",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["--root", str(root), "discover", "--json"]) == 0
    discovered = json.loads(capsys.readouterr().out)["rules_notice"]

    assert (
        "1 declared rule has no check yet, so a verifier judges it in every "
        f"step. The `ww-scriptize` skill starts `{NAME}`, which builds a check "
        "for it with the operator."
    ) in discovered


def test_no_notice_once_every_rule_is_scriptized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _judged(tmp_path)
    for rule in ("develop/1", "develop/2"):
        assert (
            main(
                [
                    "--root",
                    str(root),
                    "rules",
                    "decline",
                    rule,
                    "--reason",
                    "Judgement.",
                    "--yes",
                ]
            )
            == 0
        )
    capsys.readouterr()

    assert "declared rule" not in _discover(root, capsys)
