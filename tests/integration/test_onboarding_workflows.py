# SPDX-License-Identifier: GPL-3.0-or-later
"""ww's learning and setup workflows, and the skills that start them."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.builtin_workflows import builtin_workflow, is_builtin
from ww.cli import main
from ww.config import load_configuration
from ww.config_files import SHARED_RUNTIME_FILES, user_directory
from ww.defaults import SKILLS
from ww.executable import printed_executable
from ww.extensions import ExtensionRegistry
from ww.service import WorkflowService
from ww.storage import Storage
from ww.variables import EXECUTABLE, runtime_variable_values
from ww.workflow_config import StepDefinition, WorkflowConfiguration

ONBOARDING = (
    "ww-learn",
    "ww-express",
    "ww-learn-project",
    "ww-suggest",
    "ww-solve",
    "ww-rules-from-artifacts",
    "ww-automate",
)
LEARNING_DOCUMENTS = ("me", "myrole", "team", "company", "project")
REMARK = (
    "<!-- This file is maintained by ww for ww's own use. Do not use it for "
    "anything else. If you are an agent that is not doing ww work, ignore this "
    "file. -->"
)
NEW_SKILLS = (
    "ww-setup",
    "ww-learn",
    "ww-learn-project",
    "ww-suggest",
    "ww-refresh",
    "ww-solve",
    "ww-rules-from-artifacts",
    "ww-automate",
)

pytestmark = pytest.mark.usefixtures("shipped_builtins")


def _project(root: Path, settings: dict[str, object] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )
    (root / "ww.json").write_text(json.dumps(settings or {}), encoding="utf-8")
    return root


def _load(root: Path) -> WorkflowConfiguration:
    return load_configuration(root / "ww.yaml", ExtensionRegistry.discover(root))


def _steps(steps: tuple[StepDefinition, ...]) -> Iterator[StepDefinition]:
    for step in steps:
        yield step
        yield from _steps(step.child_steps)
        yield from _steps(step.loop_steps)
        yield from _steps(step.assessment_outcomes)


@pytest.mark.parametrize("workflow", ONBOARDING)
@pytest.mark.parametrize("agent", ["claudecode", "codex"])
def test_each_learning_workflow_plans_and_lints(
    tmp_path: Path, workflow: str, agent: str
) -> None:
    root = _project(tmp_path / "project")

    assert main(["--root", str(root), "lint"]) == 0
    assert main(["--root", str(root), "plan", "-w", workflow, "--agent", agent]) == 0


def test_the_learning_workflows_neither_branch_nor_commit() -> None:
    for name in ONBOARDING:
        workflow = builtin_workflow(name)
        assert workflow.runtime == "single"
        assert not workflow.hooks
        for step in _steps(workflow.steps):
            assert step.action is None or step.action.identifier not in {
                "argv",
                "shell",
            }


def test_every_learning_file_is_written_with_the_remark() -> None:
    written: dict[str, list[str]] = {}
    for name in ONBOARDING:
        for step in _steps(builtin_workflow(name).steps):
            for update in step.update_document:
                written.setdefault(update.name, []).append(update.instruction)

    for document in LEARNING_DOCUMENTS:
        assert written[document], document
        for instruction in written[document]:
            assert REMARK in instruction, document


def test_learning_documents_resolve_to_the_user_directory_and_project_root(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path / "project")
    documents = _load(root).documents_by_name
    service = WorkflowService(Storage(root))
    store = service.documents
    # A task working in a Git worktree still shares the project's files.
    worktree = tmp_path / "project" / "ww-worktrees" / "T-1"

    assert documents["me"].scope == "user"
    assert (
        store.path(documents["me"], "T-1", worktree)
        == (user_directory() / "me.md").resolve()
    )
    for name in ("myrole", "team", "company", "project"):
        assert documents[name].scope == "project"
        expected = (root / ".ww" / f"{name}.md").resolve()
        assert store.path(documents[name], "T-1", worktree) == expected
        assert store.path(documents[name], None) == expected
    # The operator's role is personal to the checkout: `init` re-includes
    # only the shared files in Git, so it stays ignored with the rest.
    assert "myrole.md" not in SHARED_RUNTIME_FILES


def _step(workflow: str, name: str) -> StepDefinition:
    return next(
        step for step in _steps(builtin_workflow(workflow).steps) if step.name == name
    )


def test_ww_learn_asks_about_the_operators_role_in_the_project() -> None:
    choose = _step("ww-learn", "choose")
    assert [choice.label for choice in choose.choices] == [
        "everything",
        "only me",
        "only my role",
        "only team and company",
        "not now",
    ]
    assert "{{ww.documents.myrole}}" in choose.description

    role = _step("ww-learn", "role")
    assert role.child_steps[0].assessment_question is not None
    assert '"only my role"' in role.child_steps[0].assessment_question
    interview = _step("ww-learn", "interview-role")
    assert interview.interactive
    assert [update.name for update in interview.update_document] == ["myrole"]
    assert "{{ww.documents.myrole}}" in interview.description

    finish = _step("ww-learn", "finish").description
    assert "--set learned.myrole=now" in finish
    assert "me.md and myrole.md stay on their machine" in finish


@pytest.mark.parametrize(
    ("workflow", "step"),
    [
        ("ww-suggest", "gather"),
        ("ww-solve", "propose"),
        ("ww-rules-from-artifacts", "read"),
        ("ww-automate", "analyse"),
    ],
)
def test_the_proposing_workflows_read_every_learning_file(
    workflow: str, step: str
) -> None:
    description = _step(workflow, step).description
    for document in LEARNING_DOCUMENTS:
        assert f"{{{{ww.documents.{document}}}}}" in description, document


def test_steps_name_ww_commands_with_the_configured_executable(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path / "project")
    service = WorkflowService(Storage(root))

    with printed_executable("ww-next"):
        start_after_init(service, "ww-suggest", "T-1", agent="codex")
        gather = service.next("T-1")

    assert gather.item_name == "gather"
    assert "`ww-next discover`" in gather.action_text
    assert "{{" not in gather.action_text
    with printed_executable(None):
        values = runtime_variable_values(root, "T-1")
    assert values[EXECUTABLE] == "./ww"


def test_discover_lists_the_learning_workflows_briefly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path / "project")

    assert main(["--root", str(root), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in report["builtin_workflows"]] == list(ONBOARDING)
    assert "task" in [item["name"] for item in report["workflows"]]

    assert main(["--root", str(root), "discover"]) == 0
    output = capsys.readouterr().out
    section = output.split("## ww's own workflows", 1)[1].split("\n## ", 1)[0]
    entries = [line for line in section.splitlines() if line.startswith("- ")]
    assert [entry.split("`")[1] for entry in entries] == list(ONBOARDING)
    # One short line each.
    assert all(len(entry) < 140 for entry in entries)


@pytest.mark.parametrize("workflow", ONBOARDING)
def test_the_settings_switch_each_learning_workflow_off(
    tmp_path: Path, workflow: str
) -> None:
    root = _project(tmp_path / "project", {"workflows": {workflow: {"enabled": False}}})

    configuration = _load(root)
    names = {item.name for item in configuration.workflows}

    assert workflow not in names
    assert set(ONBOARDING) - {workflow} <= names
    # A recommendation of the switched-off workflow is dropped, and the one
    # that made it is still listed as ww's own.
    for item in configuration.workflows:
        assert item.recommended_next_workflow in (None, *names)
        if item.name in ONBOARDING:
            assert is_builtin(item)


def test_switching_every_learning_workflow_off_drops_its_documents(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path / "project",
        {"workflows": {name: {"enabled": False} for name in ONBOARDING}},
    )

    configuration = _load(root)

    assert not set(ONBOARDING) & {item.name for item in configuration.workflows}
    assert not set(LEARNING_DOCUMENTS) & set(configuration.documents_by_name)
    assert "ww-narrate" not in {mode.name for mode in configuration.modes}
    assert "catchall" in configuration.workflows_by_name


def test_init_installs_the_setup_skills(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".claude").mkdir()

    assert main(["--root", str(tmp_path), "init", "--skills", "--no-input"]) == 0
    output = capsys.readouterr().out

    assert set(NEW_SKILLS) <= set(SKILLS)
    for name in NEW_SKILLS:
        skill = tmp_path / ".claude" / "skills" / name / "SKILL.md"
        assert skill.read_text(encoding="utf-8") == SKILLS[name]
        assert SKILLS[name].startswith(f"---\nname: {name}\ndescription: ")
        assert f".claude/skills/{name}/SKILL.md" in output


@pytest.mark.parametrize(
    ("skill", "workflow"),
    # ww-express has no skill of its own: ww-setup starts it.
    [(name, name) for name in ONBOARDING if name != "ww-express"],
)
def test_each_skill_starts_its_workflow(skill: str, workflow: str) -> None:
    assert f"--workflow {workflow} " in SKILLS[skill]
    assert "--mode ww-narrate" in SKILLS[skill]


def test_the_setup_skill_guides_and_records_the_state() -> None:
    text = SKILLS["ww-setup"]

    for workflow in ("ww-learn", "ww-learn-project", "ww-suggest"):
        assert f"`{workflow}`" in text
    assert "onboarding --set explain=true" in text
    assert "onboarding --set setup.done=true" in text
    assert "myrole.md" in text


def test_a_first_setup_asks_each_thing_once() -> None:
    setup = SKILLS["ww-setup"]
    assert "Put everything into one opening message" in setup
    assert "so that its `choose` step does not ask" in setup
    choose = _step("ww-learn", "choose").description
    assert "When the requirements already say what to cover" in choose
    for interview in ("interview-me", "interview-role", "interview-team"):
        assert (
            "do not wait for a confirmation" in _step("ww-learn", interview).description
        )


def test_interviews_and_reviews_converse_until_ww_done_and_record_once() -> None:
    for workflow, step in (
        ("ww-learn", "interview-me"),
        ("ww-learn", "interview-role"),
        ("ww-learn", "interview-team"),
        ("ww-learn-project", "review"),
        ("ww-suggest", "design"),
        ("ww-suggest", "propose"),
    ):
        description = _step(workflow, step).description
        assert "until the operator says `ww done`" in description, step
        assert "record the conversation once" in description, step
        assert "at most one follow-up" not in description, step
    for interview in ("interview-me", "interview-role", "interview-team"):
        description = _step("ww-learn", interview).description
        assert "in one message, numbered" in description
        assert "follow up where an answer deserves it" in description
    for skill in ("ww-setup", "ww-learn"):
        text = SKILLS[skill]
        assert "`ww done`" in text
        assert "record it once" in text or "record it, once" in text
        assert "as you go" not in text
        assert "at most one follow-up" not in text


def test_the_refresh_skill_offers_each_subject() -> None:
    text = SKILLS["ww-refresh"]
    assert "reruns `./ww\n   inspect`" in text

    assert "`learned.myrole`" in text
    for subject in ('"me"', '"my role in this project"', '"my team and company"'):
        assert subject in text
    assert '"only my role"' in text


def test_ww_learn_recommends_the_choice_covering_the_missing_files() -> None:
    description = _step("ww-learn", "choose").description

    assert "which are missing" in description
    for choice in ('"only me"', '"only my role"', '"only team and company"'):
        assert choice in description


def test_ww_learn_project_builds_on_the_inspect_profile() -> None:
    scan = _step("ww-learn-project", "scan").description
    executable = "{{ww.executable}}"
    assert f"First run `{executable} inspect`" in scan
    assert "only on what inspect cannot see" in scan
    for fact in ("integration branch", "exact argument list", "`task_format`"):
        assert fact in scan, fact
    assert "`commit_format`" in scan

    history = _step("ww-learn-project", "history").description
    assert "Start from the Fixes section" in history
    assert "one fix is not a pattern" in history
    assert "as its check" in history

    review = _step("ww-learn-project", "review")
    assert "setup facts first" in review.description
    (project,) = review.update_document
    assert 'first section is "Profile"' in project.instruction
    assert 'Then "Setup facts"' in project.instruction


def test_ww_suggest_designs_with_the_operator_before_proposing() -> None:
    workflow = builtin_workflow("ww-suggest")
    names = [step.name for step in _steps(workflow.steps)]

    assert [name for name in names if name != "assess"] == [
        "gather",
        "design",
        "setup",
        "propose",
        "apply",
    ]
    design = _step("ww-suggest", "design")
    assert design.interactive
    # Whom the setup is for is the design's last question, not a step of its own.
    assert [choice.label for choice in design.choices] == [
        "for me",
        "for the team",
        "learn first",
        "not now",
    ]
    description = design.description
    assert "in one message: numbered items" in description
    assert "stated as decided, not asked" in description
    assert "ww-setup.local.yaml" in description
    # Defaults follow the profile: lanes that exist, review by team shape,
    # worktrees for parallel work, and projects only when the layout found some.
    assert "only where such branches exist" in description
    assert "for a solo team shape, no interactive review" in description
    assert "self-review step" in description
    assert "the operator keeping the review" in description
    assert "on by default when several contributors are active" in description
    assert "ask for the paths of the sibling repositories" in description
    assert "only when the Fixes section shows a repeated cause" in description

    gather = _step("ww-suggest", "gather").description
    assert "{{ww.executable}} inspect" in gather
    assert 'no "Profile" section' in gather
    assert "proposal will be weaker" in gather
    assert "read only" in gather


def test_ww_suggest_proposes_a_complete_setup_shaped_by_the_project() -> None:
    propose = _step("ww-suggest", "propose")
    description = propose.description

    assert propose.interactive
    assert [choice.label for choice in propose.choices] == ["apply", "cancel"]
    assert "documentation's examples" in description
    for piece in (
        "`base_branches`",
        "`branch_name_formats`",
        "`commit_format`",
        "`on_failure: fix`",
        "`inherit`",
        "`recommended_next_workflow`",
        "ext/ww/git/handlers:is-git-clean",
        "--dry-run",
        "until the operator says `ww done`",
        "each piece with its evidence in one clause",
        "`hotfix/*` branches merged this year",
        "walk through the main lane",
        "in ten lines or fewer",
    ):
        assert piece in description, piece
    # The proposal reads as paragraphs, not one block.
    assert description.count("\n\n") == 5
    apply = _step("ww-suggest", "apply").description
    assert "--for <me or team, as chosen in design> --yes" in apply
    assert "plan --workflow <the main lane>" in apply
    assert '"what an agent gets on the first task"' in apply


def test_the_project_scan_records_how_and_where_commands_run() -> None:
    scan = _step("ww-learn-project", "scan").description
    assert "how and where commands run" in scan
    assert "`docker compose exec app`" in scan
    assert "whether each worktree gets its own environment" in scan
    assert "run the way the scan" in _step("ww-learn-project", "history").description

    (project,) = _step("ww-learn-project", "review").update_document
    assert "how and where commands run" in project.instruction


@pytest.mark.parametrize(
    ("workflow", "step"),
    [
        ("ww-suggest", "propose"),
        ("ww-solve", "propose"),
        ("ww-automate", "propose"),
        ("ww-rules-from-artifacts", "propose"),
    ],
)
def test_every_generated_command_follows_the_projects_convention(
    workflow: str, step: str
) -> None:
    description = _step(workflow, step).description
    assert "worktree" in description
    assert "wrapper" in description
    if workflow != "ww-rules-from-artifacts":
        assert "main checkout's absolute path" in description


def test_the_rule_skill_writes_checks_for_the_step_directory() -> None:
    path = Path(__file__).parents[2] / "src/ww/assets/ww-rule_skill.md"
    skill = " ".join(path.read_text(encoding="utf-8").split())
    assert "the step's directory (the task's worktree when there is one)" in skill
    assert "never an absolute path into the main checkout" in skill


def test_proposals_use_the_step_features_the_work_calls_for() -> None:
    design = _step("ww-suggest", "design").description
    assert "such as manual testing, the step features it" in design
    assert "an `items` step for work that splits into cases" in design
    assert "(11) last" in design
    assert "manual testing" in _step("ww-suggest", "gather").description

    for workflow in ("ww-suggest", "ww-solve"):
        propose = _step(workflow, "propose").description
        for feature in (
            "`items` step",
            "`interactive: true`",
            "`interactive: page` with `choices`",
            "`documents` entry",
            "`item.field.<name>`",
            "`loop` with a `break`",
        ):
            assert feature in propose, (workflow, feature)
    assert "without waiting to be asked" in _step("ww-suggest", "propose").description


def test_the_setup_skill_recommends_what_was_not_learned_yet() -> None:
    text = SKILLS["ww-setup"]

    assert '"not learned yet"' in text
    assert "also when `project.setup.done` is already" in text
    for choice in ('"only me"', '"only my role"', '"only team and company"'):
        assert choice in text


def test_ww_express_infers_the_four_documents_and_confirms_them_once() -> None:
    workflow = builtin_workflow("ww-express")
    assert [step.name for step in workflow.steps] == ["infer", "confirm", "finish"]
    assert workflow.recommended_next_workflow == "ww-suggest"

    infer = _step("ww-express", "infer")
    assert not infer.interactive
    for signal in (
        "{{ww.documents.project}}",
        "`{{ww.executable}} inspect`",
        "`git config user.email`",
        "`git log --author=<their email> -n 300`",
        "`git remote -v`",
        "never look up individual people online",
        "a confidence (high, medium or low)",
        "what could not be inferred",
    ):
        assert signal in infer.description, signal

    confirm = _step("ww-express", "confirm")
    assert confirm.interactive
    assert "until the operator says `ww done`" in confirm.description
    assert "record the conversation once" in confirm.description
    assert "nobody is named without asking" in confirm.description
    updates = {update.name: update.instruction for update in confirm.update_document}
    assert list(updates) == ["me", "myrole", "team", "company"]
    for instruction in updates.values():
        assert REMARK in instruction
        assert (
            "The second line is: Inferred by ww from the repository and confirmed "
            "by the operator on <today's date>." in instruction
        )

    finish = _step("ww-express", "finish").description
    for key in ("me", "myrole", "team", "company"):
        assert f"--set learned.{key}=now" in finish, key
    assert "ww-suggest" in finish


def test_the_setup_skill_offers_express_and_guided() -> None:
    text = SKILLS["ww-setup"]
    assert "- Express: ww learns the project, infers your profile" in text
    assert "Runs `ww-learn-project`, then `ww-express`, then `ww-suggest`." in text
    assert "- Guided: ww learns the project, then short interviews" in text
    assert "Runs `ww-learn-project`, then\n     `ww-learn`, then `ww-suggest`." in text
    assert "Express takes five replies" in text
    assert "inferred from the repository" in SKILLS["ww-refresh"]
