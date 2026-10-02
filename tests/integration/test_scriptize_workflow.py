# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww-scriptize-rules``: scriptizing rules as a project task of its own."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

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
    assert workflow.needs_hooks_from and workflow.hooks_from is None
    assert workflow.restartable
    assert _step("approaches").interactive and _step("checks").interactive
    assert [c.label for c in _step("approaches").choices] == ["build", "nothing to do"]
    collect = _step("collect").description
    for piece in ("rules --json", "`unscriptized`", "`check_guidance`", "fewest"):
        assert piece in collect, piece
    build = _step("build").description
    for piece in (
        "deliberately violating input",
        "a sample of real files",
        "real violations do not block",
        "baseline",
        "config files",
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


def test_it_refuses_to_start_without_a_lane(tmp_path: Path) -> None:
    root = _project(tmp_path)

    with pytest.raises(ConfigurationError, match="name that lane in ww.json"):
        _start(root)


def test_it_runs_with_the_lanes_hooks(tmp_path: Path) -> None:
    root = _project(tmp_path, {"workflows": {NAME: {"hooks_from": "task"}}})

    service = _start(root)

    _, snapshot = service.load("S-1")
    assert snapshot.plan.items[0].phase == "before_start_workflow"
    assert (root / "lane-hook.txt").read_text().strip() == "lane"
    configuration = load_configuration(
        root / "ww.yaml", ExtensionRegistry.discover(root)
    )
    workflow = configuration.workflows_by_name[NAME]
    assert workflow.hooks_from == "task"
    assert is_builtin(workflow)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "nope",
            f'ww.json "workflows.{NAME}.hooks_from": workflow {NAME!r} takes its '
            "hooks from unknown workflow 'nope'",
        ),
        (NAME, "cannot take its hooks from itself"),
    ],
)
def test_a_bad_lane_is_refused(tmp_path: Path, source: str, message: str) -> None:
    root = _project(tmp_path, {"workflows": {NAME: {"hooks_from": source}}})

    with pytest.raises(ConfigurationError) as error:
        load_configuration(root / "ww.yaml", ExtensionRegistry.discover(root))

    assert message in str(error.value)


def test_a_lane_that_takes_hooks_itself_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path, {"workflows": {NAME: {"hooks_from": "copy"}}})
    (root / "ww.yaml").write_text(
        LANE_HOOK + "  - name: copy\n    hooks_from: task\n    steps:\n"
        "      - work: Work.\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="name that one"):
        load_configuration(root / "ww.yaml", ExtensionRegistry.discover(root))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"hooks_from": ""}, "hooks_from must name a workflow"),
        ({"hooks_from": 3}, "hooks_from must name a workflow"),
        ({"lane": "task"}, r"unknown key\(s\): lane"),
    ],
)
def test_the_setting_is_validated(
    tmp_path: Path, value: dict[str, object], message: str
) -> None:
    path = tmp_path / "ww.json"
    path.write_text(json.dumps({"workflows": {NAME: value}}), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_project_config(path)


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
    assert "never edit ww's configuration files yourself" in " ".join(skill.split())


def test_discover_lists_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)

    assert main(["--root", str(root), "discover"]) == 0

    assert f"`{NAME}`" in capsys.readouterr().out


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


def test_a_lane_for_a_builtin_the_project_replaces_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path, {"workflows": {NAME: {"hooks_from": "task"}}})
    (root / "ww.yaml").write_text(
        LANE_HOOK + f"  - name: {NAME}\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigurationError,
        match=f'ww.json "workflows.{NAME}.hooks_from" sets the lane of the built-in',
    ):
        load_configuration(root / "ww.yaml", ExtensionRegistry.discover(root))


@pytest.mark.parametrize(
    ("local", "listed"),
    [({"enabled": True}, True), ({"enabled": False}, False)],
)
def test_levels_merge_enabled_and_the_lane(
    tmp_path: Path, local: dict[str, object], listed: bool
) -> None:
    root = _project(tmp_path, {"workflows": {NAME: {"hooks_from": "task"}}})
    (root / "ww.local.json").write_text(
        json.dumps({"workflows": {NAME: local}}), encoding="utf-8"
    )

    settings = load_project_config(root / "ww.json")
    configuration = load_configuration(
        root / "ww.yaml", ExtensionRegistry.discover(root)
    )

    assert settings.builtin_hooks_from == {NAME: "task"}
    assert settings.workflow_enabled(NAME) is listed
    workflow = configuration.workflows_by_name.get(NAME)
    assert (workflow is not None) is listed
    assert workflow is None or workflow.hooks_from == "task"


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
LANE_HINT = f'`"workflows": {{"{NAME}": {{"hooks_from": "<workflow>"}}}}` in ww.json'


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

    for text in (discovered, page):
        assert NOTICE in text
        assert f"The `ww-scriptize` skill starts `{NAME}`" in text
        assert LANE_HINT in text
    assert discovered.index("## Rules") < discovered.index("## Workflows")
    assert main(["--root", str(root), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["rules_notice"].startswith(NOTICE)
    assert main(["--root", str(root), "next", "S-1", "--role", "manager"]) == 0
    assert NOTICE not in capsys.readouterr().out


def test_the_notice_drops_the_lane_hint_once_the_lane_is_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _judged(tmp_path, {"workflows": {NAME: {"hooks_from": "task"}}})

    discovered = _discover(root, capsys)

    assert NOTICE in discovered
    assert LANE_HINT not in discovered
    assert NOTICE not in _first_page(root, NAME, capsys)


def test_no_notice_while_the_workflow_is_switched_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _judged(tmp_path, {"workflows": {NAME: {"enabled": False}}})

    assert "declared rule" not in _discover(root, capsys)
    assert "declared rule" not in _first_page(root, "task", capsys)


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
