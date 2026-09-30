# SPDX-License-Identifier: GPL-3.0-or-later
"""``discover``, the project ``enabled`` switch, and the shipped ww skill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.cli import main
from ww.defaults import AGENT_INSTRUCTIONS, SKILLS
from ww.errors import ConfigurationError, StateError
from ww.extensions import Extension, ExtensionRegistry
from ww.project_config import ProjectConfig
from ww.service import WorkflowService
from ww.storage import Storage

WW_SKILL = SKILLS["ww"]
WORKFLOWS = """modes:
  - name: economy
    description:
      - Use as few tokens as possible.
      - Prefer short answers.
workflows:
  - name: task
    description: Implement a change.
    modes: [economy]
    steps:
      - work: Work.
  - name: bugfix
    steps:
      - fix: Fix.
"""


def _project(tmp_path: Path, config: dict[str, object] | None = None) -> Path:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (tmp_path / "ww-agentic-workflows.json").write_text(
        json.dumps(config or {}), encoding="utf-8"
    )
    return tmp_path


def _discover(root: Path, capsys: pytest.CaptureFixture[str], *flags: str) -> str:
    assert main(["--root", str(root), "discover", *flags]) == 0
    return capsys.readouterr().out


def test_discover_lists_choices_options_and_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(
        tmp_path,
        {
            "extensions": {
                "ww/git": {
                    "branch_name_formats": {
                        "default": "feature/{{task_id}}",
                        "bugfix": "hotfix/{{task_id}}",
                    }
                }
            }
        },
    )

    output = _discover(root, capsys)

    assert output.startswith("# ww discover\n\nww is enabled for this project.")
    for expected in (
        "- `task` — Implement a change. Default modes: `economy`.",
        "- `bugfix` — No description.",
        "- `economy` — Use as few tokens as possible. Prefer short answers.",
        "- `ext/ww/git/modes:conventional-commits` — Write commit messages",
        "- `single` (project default) — One session plays both manager and worker",
        "- `auto` — The manager delegates each assignment to a worker agent",
        "- `manager` — Runs start and next",
        "- `worker` — Performs one assignment",
        "`claudecode`",
        "or `custom:<name>`.",
        "- `--runtime` (`-r`): `single` or `auto`. Steps requesting a specific "
        "worker are honoured only under `--runtime auto`; `single` records them "
        "and performs the step in this session. Omitted, ww uses the workflow's "
        "own runtime if it declares one, then the project default, `single`.",
        "- `--model`, `--reasoning`: Optional.",
        "- `--branch-strategy`: `default`, `bugfix`.",
        "./ww start <TASK-ID> --workflow <workflow> --agent <agent> "
        '--init-artifact "<the user\'s requirements, normalized>" --role manager',
        "./ww instruction <task-id> --role manager",
        "./ww status <task-id>",
        "./ww plan --workflow <workflow> --agent <agent>",
    ):
        assert expected in output, expected


def test_discover_json_carries_the_same_choices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = json.loads(_discover(_project(tmp_path), capsys, "--json"))

    assert report["enabled"] is True
    assert report["workflows"] == [
        {
            "name": "task",
            "description": "Implement a change.",
            "default_modes": ["economy"],
            "runtime": None,
            "inherits": None,
            "recommended_next_workflow": None,
            "delegation_requests": [],
        },
        {
            "name": "bugfix",
            "description": "",
            "default_modes": [],
            "runtime": None,
            "inherits": None,
            "recommended_next_workflow": None,
            "delegation_requests": [],
        },
    ]
    assert [runtime["name"] for runtime in report["runtimes"]] == ["single", "auto"]
    assert [runtime["default"] for runtime in report["runtimes"]] == [True, False]
    assert [role["name"] for role in report["roles"]] == ["manager", "worker"]
    assert report["branch_strategies"] == []
    assert set(report["commands"]) == {"start", "instruction", "status", "plan"}


def test_discover_without_branch_strategies_says_to_omit_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _discover(_project(tmp_path), capsys)

    assert "- `--branch-strategy`: no extension defines branch strategies" in output


def test_a_disabled_project_tells_agents_not_to_use_ww(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, {"enabled": False})
    # The workflow file is not even read once ww is disabled.
    (root / "ww-agentic-workflows.yaml").write_text("not: [valid", encoding="utf-8")

    output = _discover(root, capsys)
    report = json.loads(_discover(root, capsys, "--json"))

    assert output.startswith("# ww discover\n\n**ww is disabled for this project.**")
    assert "Do not use ww for this work" in output
    assert '"enabled": false' in output
    assert report == {"enabled": False, "message": report["message"]}
    assert "Do not use ww for this work" in report["message"]


def test_a_disabled_project_refuses_to_start(tmp_path: Path) -> None:
    root = _project(tmp_path, {"enabled": False})
    service = WorkflowService(Storage(root))

    with pytest.raises(StateError, match="ww is disabled for this project"):
        service.start("task", "TASK-1", agent="codex")
    assert not service.tasks.task_exists("TASK-1")


def test_discover_is_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)

    _discover(root, capsys)

    assert not (root / ".ww").exists()


def test_branch_strategies_come_only_from_configured_extensions(
    tmp_path: Path,
) -> None:
    def failing(_settings: object) -> tuple[str, ...]:
        raise RuntimeError("boom")

    naming = Extension("acme", "naming", branch_strategies=lambda s: ("a", "b"))
    broken = Extension("acme", "broken", branch_strategies=failing)
    quiet = Extension("acme", "quiet", branch_strategies=lambda s: ("never",))

    registry = ExtensionRegistry(
        tmp_path,
        (naming, quiet),
        ProjectConfig(extensions={"acme/naming": {"x": 1}}),
    )
    assert registry.branch_strategies() == ("a", "b")

    registry = ExtensionRegistry(
        tmp_path, (broken,), ProjectConfig(extensions={"acme/broken": {"x": 1}})
    )
    with pytest.raises(ConfigurationError, match="failed to report branch strategies"):
        registry.branch_strategies()
    with pytest.raises(TypeError, match="branch_strategies must be callable"):
        Extension("acme", "bad", branch_strategies="nope")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The shipped skill and init
# --------------------------------------------------------------------------- #


def test_the_skill_and_instructions_send_agents_to_discover() -> None:
    assert WW_SKILL.startswith("---\nname: ww\ndescription: ")
    assert "./ww discover" in WW_SKILL
    assert "./ww discover" in AGENT_INSTRUCTIONS
    assert len(AGENT_INSTRUCTIONS.splitlines()) < 50


def test_init_installs_the_skill_into_existing_agent_directories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    kept = tmp_path / ".codex/skills/ww/SKILL.md"
    kept.parent.mkdir(parents=True)
    kept.write_text("custom", encoding="utf-8")

    assert main(["--root", str(tmp_path), "init", "--no-input", "--skills"]) == 0
    output = capsys.readouterr().out

    assert (tmp_path / ".claude/skills/ww/SKILL.md").read_text(encoding="utf-8") == (
        WW_SKILL
    )
    assert kept.read_text(encoding="utf-8") == "custom"
    assert not (tmp_path / ".gemini").exists()
    assert (tmp_path / ".claude/skills/noww/SKILL.md").read_text(
        encoding="utf-8"
    ) == SKILLS["noww"]
    # A directory set up before noww existed gains it; its ww skill is kept.
    assert (tmp_path / ".codex/skills/noww/SKILL.md").is_file()
    assert ".claude/skills/ww/SKILL.md" in output
    assert "install the ww, noww and ww-rule skills" not in output


def test_init_without_skills_suggests_installing_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".claude").mkdir()

    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    output = capsys.readouterr().out

    assert not (tmp_path / ".claude/skills").exists()
    assert (
        "Optionally install the ww, noww and ww-rule skills with `init --skills` "
        "for: "
        ".claude." in output
    )
    config = json.loads((tmp_path / "ww-agentic-workflows.json").read_text())
    assert config["enabled"] is True


def test_init_asks_before_installing_each_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ww.cli.initialization import _skill_installs

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        # Answer by what is being asked, not by position: the order and the
        # number of agent directories both change as integrations are added.
        prompts.append(prompt)
        if ".claude/skills?" in prompt:
            return ""  # accept the default, which is yes for a directory here
        if "Create which?" in prompt:
            return "none"
        return "n"

    monkeypatch.setattr("builtins.input", answer)

    paths = _skill_installs(Storage(tmp_path), None, interactive=True)

    assert paths == ((".claude", "ww"), (".claude", "noww"), (".claude", "ww-rule"))
    # A directory that exists is asked about on its own...
    question = "Install the ww, noww and ww-rule skills into {}/skills? [Y/n]: "
    assert question.format(".claude") in prompts
    assert question.format(".cursor") in prompts
    # ...and every agent without one shares a single question.
    assert "Create which? (comma-separated, or none): " in prompts
    assert len(prompts) == 3
    assert _skill_installs(Storage(tmp_path), False, interactive=True) == ()


def test_init_offers_the_absent_agent_directories_in_one_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ww.cli.initialization import _known_agent_directories, _skill_installs

    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return ".codex, .claude"

    monkeypatch.setattr("builtins.input", answer)

    paths = _skill_installs(Storage(tmp_path), None, interactive=True)

    # One question, not one per agent ww knows about.
    assert len(prompts) == 1
    assert len(_known_agent_directories()) > 1
    assert paths == (
        (".codex", "ww"),
        (".codex", "noww"),
        (".codex", "ww-rule"),
        (".claude", "ww"),
        (".claude", "noww"),
        (".claude", "ww-rule"),
    )


def test_declining_every_agent_directory_takes_one_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ww.cli.initialization import _skill_installs

    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", answer)

    assert _skill_installs(Storage(tmp_path), None, interactive=True) == ()
    assert len(prompts) == 1


def _answers(monkeypatch: pytest.MonkeyPatch, reply: str) -> list[str]:
    """Answer every init question with ``reply`` and record the questions."""
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return reply

    monkeypatch.setattr("builtins.input", answer)
    return prompts


@pytest.mark.parametrize(("reply", "installed"), [("", True), ("n", False)])
def test_a_newly_bundled_skill_is_offered_once_into_the_chosen_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: str, installed: bool
) -> None:
    from ww.cli.initialization import _skill_installs

    (tmp_path / ".claude").mkdir()
    # Yes for the existing .claude directory, none of the absent ones.
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: "none" if "Create which?" in prompt else "y",
    )
    bundled = tuple((".claude", name) for name in SKILLS)
    assert _skill_installs(Storage(tmp_path), None, interactive=True) == bundled

    # A later ww version bundles another skill.
    monkeypatch.setitem(SKILLS, "extra", "---\nname: extra\n---\n")
    prompts = _answers(monkeypatch, reply)
    installs = _skill_installs(Storage(tmp_path), None, interactive=True)

    # One question, about the skill only: the directories are remembered.
    assert prompts == ["ww now ships the `extra` skill. Install into .claude? [Y/n]: "]
    assert ((".claude", "extra") in installs) is installed
    # Either answer is remembered, so the next run asks nothing.
    prompts = _answers(monkeypatch, "y")
    assert _skill_installs(Storage(tmp_path), None, interactive=True) == installs
    assert prompts == []


def test_a_project_set_up_before_skills_were_remembered_is_asked_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ww.cli.initialization import _known_agent_directories, _skill_installs

    (tmp_path / ".claude/skills/ww").mkdir(parents=True)
    (tmp_path / ".claude/skills/ww/SKILL.md").write_text("ww", encoding="utf-8")
    choices = tmp_path / ".ww/init-choices.json"
    choices.parent.mkdir()
    agents = {name: name == ".claude" for name in _known_agent_directories()}
    choices.write_text(json.dumps({"agents": agents}), encoding="utf-8")
    monkeypatch.setitem(SKILLS, "extra", "---\nname: extra\n---\n")

    prompts = _answers(monkeypatch, "")
    installs = _skill_installs(Storage(tmp_path), None, interactive=True)

    # The installed ww skill counts as accepted; only the new one is asked.
    # The installed ww skill counts as accepted; only the others are asked.
    new = [name for name in SKILLS if name != "ww"]
    label = ", ".join(f"`{name}`" for name in new)
    plural = "s" if len(new) > 1 else ""
    assert prompts == [
        f"ww now ships the {label} skill{plural}. Install into .claude? [Y/n]: "
    ]
    assert installs == tuple((".claude", name) for name in SKILLS)


def test_the_permission_notice_is_shown_once_and_next_steps_until_a_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init = ["--root", str(tmp_path), "init", "--no-input"]

    assert main(init) == 0
    first = capsys.readouterr().out
    assert "ACTION NEEDED" in first
    assert "Next steps" in first

    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )
    assert main(init) == 0
    second = capsys.readouterr().out
    assert "ACTION NEEDED" not in second
    assert "Next steps" not in second
    # The documentation links always stay.
    assert "Documentation" in second
    assert "documentation/specification.md" in second


def test_discover_tells_agents_to_use_the_ticket_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _discover(_project(tmp_path), capsys)

    assert "To start a task:" in output
    assert "use that key as <TASK-ID> so the task matches the issue" in output
    assert "omit `<TASK-ID>` to let ww assign one" not in output
    assert "use that key as the task ID" in WW_SKILL
    assert "start the task\nunder that key" in AGENT_INSTRUCTIONS


def test_explicit_task_format_requires_an_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, {"task_format": "explicit"})
    (root / "ww-agentic-workflows.yaml").write_text(
        WORKFLOWS
        + """  - name: tracked
    steps:
      - create: Create the issue and return its key.
        provide:
          - task_id: The issue key.
      - work: Work.
  - name: parent
    steps:
      - split: Split.
        children: ~
      - execute:
        workflow_per_child: task
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(root))

    assert "This project requires an explicit <TASK-ID>" in _discover(root, capsys)
    with pytest.raises(StateError, match="requires an explicit task ID"):
        service.start("task", None, agent="codex")
    assert service.start("task", "PROJ-7", agent="codex").task_id == "PROJ-7"
    request = service.start("tracked", None, agent="codex")
    assert request.task_id.startswith("REQUEST-")
    service.start("parent", "P", agent="codex")
    service.next("P")
    with pytest.raises(StateError, match="requires an explicit task ID"):
        service.add_child("P", None, "Part")
    assert service.add_child("P", "PROJ-8", "Part").id == "PROJ-8"


def test_the_project_may_choose_the_default_runtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, {"runtime": "auto"})
    service = WorkflowService(Storage(root))

    text = _discover(root, capsys)
    report = json.loads(_discover(root, capsys, "--json"))
    assert "- `auto` (project default) — " in text
    assert "then the project default, `auto`." in text
    assert [r["default"] for r in report["runtimes"]] == [False, True]

    defaulted = service.start("task", "T1", agent="codex")
    explicit = service.start("task", "T2", agent="codex", workflow_runtime="single")
    assert defaulted.workflow_runtime == "auto"
    assert explicit.workflow_runtime == "single"
    assert (
        main(
            [
                "--root",
                str(root),
                "start",
                "T3",
                "-w",
                "task",
                "-a",
                "codex",
                "--init-artifact",
                "x",
                "--role",
                "manager",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["workflow_runtime"] == "auto"


def test_the_configured_runtime_must_exist(tmp_path: Path) -> None:
    root = _project(tmp_path, {"runtime": "cluster"})

    with pytest.raises(
        ConfigurationError, match="runtime must be one of: single, auto"
    ):
        WorkflowService(Storage(root)).start("task", "T1", agent="codex")


def _runtime_advice_project(tmp_path: Path) -> Path:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """
workflows:
  - name: plain
    description: Nothing is requested.
    steps:
      - develop: Implement it.

  - name: reviewed
    description: Cheap triage, careful review.
    steps:
      - triage: Sort the reports.
        model: cheapest
      - review: Review it properly.
        reasoning: high
""",
        encoding="utf-8",
    )
    return tmp_path


def test_discover_points_a_workflow_that_requests_workers_at_the_auto_runtime(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _discover(_runtime_advice_project(tmp_path), capsys)

    assert "`triage`, `review`" in output
    assert "start it with `--runtime auto` so those requests apply" in output
    # The workflow that asks for nothing is left alone.
    plain = next(line for line in output.splitlines() if line.startswith("- `plain`"))
    assert "auto" not in plain


def test_discover_asks_for_a_deliberate_runtime_instead_of_the_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _discover(_runtime_advice_project(tmp_path), capsys)

    assert "Choose deliberately rather than defaulting." in output
    # The old wording told the agent to omit the flag, which always meant single.
    assert "omit it for the default" not in output
    assert "honoured only under `--runtime auto`" in output


def test_discover_reports_worker_requests_as_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = json.loads(_discover(_runtime_advice_project(tmp_path), capsys, "--json"))

    requests = {
        workflow["name"]: workflow["delegation_requests"]
        for workflow in report["workflows"]
    }
    assert requests == {"plain": [], "reviewed": ["triage", "review"]}


def test_discover_sets_the_catchall_apart_with_when_to_use_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = _discover(_project(tmp_path), capsys)

    workflows, rest = output.split("## Changes no workflow covers", 1)
    assert "`catchall`" not in workflows
    assert rest.lstrip().startswith("- `catchall` — ")
    for rule in (
        "about to change files",
        "read-only work need no task",
        "Do not start it directly",
        "before a task ww has never seen is created",
        "./ww lookup [<task>] --agent <agent>",
    ):
        assert rule in rest
    report = json.loads(_discover(tmp_path, capsys, "--json"))
    assert report["catchall"]["name"] == "catchall"
    assert "catchall" not in [workflow["name"] for workflow in report["workflows"]]


def test_discover_omits_a_switched_off_catchall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, {"workflows": {"catchall": {"enabled": False}}})

    assert "catchall" not in _discover(root, capsys)


def test_the_noww_skill_only_turns_ww_off() -> None:
    noww = SKILLS["noww"]

    assert noww.startswith("---\nname: noww\ndescription: ")
    assert "Do not use ww" in noww
    assert "catchall" in noww


def test_the_ww_rule_skill_carries_the_judgment_and_writes_through_the_cli() -> None:
    skill = SKILLS["ww-rule"]

    assert skill.startswith("---\nname: ww-rule\ndescription: ")
    for duty in (
        "./ww rules --json",
        "./ww discover",
        "never invent one",
        "atomic obligations",
        "amendment",
        "match count",
        "one imperative sentence",
        "Confirm once",
        "./ww rules promote <check>",
        "Write only through the CLI",
        "./ww lint",
        "/ww-rule split <file>",
        "/ww-rule from-review",
    ):
        assert duty in skill, duty
    for write in ("add", "add --group", "edit", "move", "filter", "promote"):
        assert f"./ww rules {write} " in skill, write


def test_init_offers_the_ww_rule_skill_once_to_a_project_set_up_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ww.cli.initialization import _known_agent_directories, _skill_installs

    (tmp_path / ".claude/skills/ww").mkdir(parents=True)
    (tmp_path / ".claude/skills/ww/SKILL.md").write_text("ww", encoding="utf-8")
    choices = tmp_path / ".ww/init-choices.json"
    choices.parent.mkdir()
    agents = {name: name == ".claude" for name in _known_agent_directories()}
    choices.write_text(
        json.dumps({"agents": agents, "skills": {"ww": True, "noww": True}}),
        encoding="utf-8",
    )
    prompts = _answers(monkeypatch, "")

    installs = _skill_installs(Storage(tmp_path), None, interactive=True)

    assert prompts == [
        "ww now ships the `ww-rule` skill. Install into .claude? [Y/n]: "
    ]
    assert (".claude", "ww-rule") in installs
    prompts.clear()
    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    assert prompts == []
    skill = tmp_path / ".claude/skills/ww-rule/SKILL.md"
    assert skill.read_text(encoding="utf-8") == SKILLS["ww-rule"]
    assert (tmp_path / ".claude/skills/ww/SKILL.md").read_text(encoding="utf-8") == "ww"
