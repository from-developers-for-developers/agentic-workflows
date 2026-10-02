# SPDX-License-Identifier: GPL-3.0-or-later
"""The command-line interface: parsing, init, and the lifecycle commands."""

import io
import json
import subprocess
from pathlib import Path

import pytest

from ww.builtin_workflows import CATCHALL, builtin_workflow
from ww.cli import build_parser, main
from ww.defaults import DEFAULT_PROJECT_CONFIG_JSON
from ww.storage import Storage
from ww.variables import BRANCH_NAMING_STRATEGY


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _agent_answers(*approve: str) -> str:
    """The answer to init's agent-skill question, for a project with none.

    Directories that already exist are asked about one at a time; every other
    agent is offered in a single comma-separated question. These projects
    start empty, so one answer covers them all however many agents ww knows.
    """
    return (",".join(approve) if approve else "none") + "\n"


_GITIGNORE_WITH_WW = (
    ".ww/*\n!.ww/team.md\n!.ww/company.md\n!.ww/project.md\n"
    "*ww.local.yaml\n*ww.local.json\n"
    "ww-setup.local.yaml\n"
)


def _complete_init(root: Path, task_id: str, capsys) -> None:  # type: ignore[no-untyped-def]
    del root, task_id, capsys


def _project(root: Path) -> None:
    (root / "ww.yaml").write_text(
        """handlers:
  - name: check
    argv: [printf, ok]
workflows:
  - name: task
    hooks:
      after_complete:
        - steps: [init]
          name: check
    steps:
      - name: work
""",
        encoding="utf-8",
    )


def test_plan_cli_renders_json_and_does_not_mutate_project(
    tmp_path: Path, capsys
) -> None:
    _project(tmp_path)

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "plan",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)

    assert [item["name"] for item in output["items"]] == [
        "init",
        "check",
        "work",
        "update-workflow-summary",
    ]
    assert output["items"][1]["owner"] == "ww"
    assert not (tmp_path / ".ww" / "tasks").exists()
    assert not (tmp_path / ".ww/executions.jsonl").exists()


def test_start_child_parser_initializes_json_output() -> None:
    args = build_parser().parse_args(["start-child", "TASK-1", "child-1"])

    assert args.json_output is False


def test_start_cli_persists_an_explicit_branch_naming_strategy(
    tmp_path: Path, capsys
) -> None:
    _project(tmp_path)

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "start",
                "TASK-STRATEGY",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--requirements",
                "Record the task requirements.",
                "--branch-strategy",
                "experiment",
            ]
        )
        == 0
    )
    capsys.readouterr()

    state = Storage(tmp_path).task_persistence.read_execution_state(
        "TASK-STRATEGY", "01-task"
    )
    assert state is not None
    assert dict(state.workflow_values)[BRANCH_NAMING_STRATEGY] == "experiment"
    assert state.item_executions[0].status == "completed"
    assert state.item_executions[0].artifact is not None


def test_plan_cli_markdown_and_error_are_read_only(tmp_path: Path, capsys) -> None:
    _project(tmp_path)

    assert (
        main(
            ["--root", str(tmp_path), "plan", "--workflow", "task", "--agent", "codex"]
        )
        == 0
    )
    assert "# Workflow plan — `task`" in capsys.readouterr().out
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "plan",
                "--workflow",
                "missing",
                "--agent",
                "codex",
            ]
        )
        == 1
    )
    assert "workflow not found" in capsys.readouterr().err
    assert not (tmp_path / ".ww").exists()


def test_lint_cli_validates_configuration_without_creating_runtime_state(
    tmp_path: Path, capsys
) -> None:
    _project(tmp_path)

    assert main(["--root", str(tmp_path), "lint"]) == 0
    assert capsys.readouterr().out == (
        "ww.yaml is valid.\nConfiguration files: ww.yaml\n"
    )
    assert not (tmp_path / ".ww").exists()


def test_lint_cli_reports_configuration_errors_without_creating_runtime_state(
    tmp_path: Path, capsys
) -> None:
    config_file = tmp_path / "ww.yaml"
    config_file.write_text("workflows: invalid\n", encoding="utf-8")

    assert main(["--root", str(tmp_path), "lint"]) == 1
    assert "workflows must be a list" in capsys.readouterr().err
    assert not (tmp_path / ".ww").exists()


def test_artifacts_cli_lists_steps_and_hooks_as_json(tmp_path: Path, capsys) -> None:
    _project(tmp_path)
    common = ["--root", str(tmp_path)]

    assert (
        main(
            [
                *common,
                "start",
                "TASK-ARTIFACTS",
                "-w",
                "task",
                "-a",
                "codex",
                "--requirements",
                "requirements",
            ]
        )
        == 0
    )
    capsys.readouterr()
    _complete_init(tmp_path, "TASK-ARTIFACTS", capsys)

    assert main([*common, "artifacts", "TASK-ARTIFACTS"]) == 0
    artifacts = json.loads(capsys.readouterr().out)

    assert artifacts[0]["step"] == "init"
    assert artifacts[0]["artifact"].endswith("01-task/steps/01-init.md")


def test_workflow_agent_and_runtime_short_options(tmp_path: Path, capsys) -> None:
    _project(tmp_path)

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "plan",
                "-w",
                "task",
                "-a",
                "codex",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["workflow"] == "task"

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "start",
                "TASK-SHORT",
                "-w",
                "task",
                "-a",
                "codex",
                "-r",
                "auto",
                "--requirements",
                "requirements",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Generated by `ww`" in output
    assert "## Manager: dispatch the `work` assignment" in output


def test_catalog_commands_read_the_normalized_configuration(
    tmp_path: Path, capsys
) -> None:
    _project(tmp_path)
    (tmp_path / "ww.yaml").write_text(
        """modes:
  - name: economy
    description: Use fewer tokens.
workflows:
  - name: task
    modes: [economy]
    steps: []
""",
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "modes"]) == 0
    assert json.loads(capsys.readouterr().out)["modes"][0]["name"] == "economy"
    assert main(["--root", str(tmp_path), "workflows"]) == 0
    assert json.loads(capsys.readouterr().out)["workflows"] == [
        {"name": "task", "description": "", "modes": ["economy"], "runtime": None},
        {
            "name": "catchall",
            "description": builtin_workflow(CATCHALL).description,
            "modes": [],
            "runtime": "auto",
        },
    ]


def test_cli_roles_are_parsed_rendered_and_enforced(tmp_path: Path, capsys) -> None:
    _project(tmp_path)
    common = ["--root", str(tmp_path)]

    assert (
        main(
            [
                *common,
                "start",
                "TASK-ROLE",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--requirements",
                "requirements",
                "--role=manager",
                "--json",
            ]
        )
        == 0
    )
    started = json.loads(capsys.readouterr().out)
    assert started["caller_role"] == "manager"
    assert started["next_role"] == "manager"
    assert started["control"] == "handoff_manager"

    state_path = tmp_path / ".ww/tasks/TASK-ROLE/state.json"
    before = state_path.read_text(encoding="utf-8")
    assert main([*common, "next", "TASK-ROLE", "--role", "worker"]) == 1
    assert "manager-role command" in capsys.readouterr().err
    assert state_path.read_text(encoding="utf-8") == before

    with pytest.raises(SystemExit):
        main([*common, "status", "TASK-ROLE", "--role", "invalid"])


def test_lifecycle_commands_start_from_the_compiled_plan(
    tmp_path: Path, capsys
) -> None:
    _project(tmp_path)

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "start",
                "TASK-1",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--runtime",
                "single",
                "--model",
                "gpt",
                "--reasoning",
                "high",
                "--requirements",
                "requirements",
            ]
        )
        == 0
    )
    started = capsys.readouterr().out
    assert "## Manager and worker: dispatch the `work` assignment" in started
    assert "keeps this task's plan and progress" in started
    task = tmp_path / ".ww" / "tasks" / "TASK-1"
    assert (task / "metadata.json").exists()
    assert (task / "state.json").exists()
    assert not (task / "aggregate.json").exists()
    assert not (task / "task.jsonl").exists()
    assert (task / "runs" / "01-task" / "steps" / "01-init.md").exists()


def test_metadata_command_prints_nested_task_metadata_json(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: create
        artifact: false
        saves:
          - metadata.foo.bar.baz.jira_id: The created Jira issue ID.
""",
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "start",
                "TASK-1",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--requirements",
                "requirements",
            ]
        )
        == 0
    )
    capsys.readouterr()
    _complete_init(tmp_path, "TASK-1", capsys)
    assert main(["--root", str(tmp_path), "next", "TASK-1"]) == 0
    instruction = capsys.readouterr().out
    assert "Detect and preserve these task metadata values" in instruction
    assert "The created Jira issue ID." in instruction
    assert '--metadata foo.bar.baz.jira_id="<foo.bar.baz.jira_id>"' in instruction
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "complete",
                "TASK-1",
                "--summary",
                "Metadata saved.",
                "--metadata",
                "foo.bar.baz.jira_id=PROJ-123",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["--root", str(tmp_path), "metadata", "TASK-1"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "foo": {"bar": {"baz": {"jira_id": "PROJ-123"}}},
    }
    task = json.loads((tmp_path / ".ww/tasks/TASK-1/metadata.json").read_text())
    assert task["metadata"] == {"foo": {"bar": {"baz": {"jira_id": "PROJ-123"}}}}


def test_project_metadata_is_saved_and_shared_across_tasks(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: capture
    steps:
      - name: discover
        artifact: false
        saves:
          - project_metadata.environments.staging.url: The shared staging URL.
  - name: consume
    steps:
      - name: deploy
        artifact: false
        description: Deploy to {{ww.project_metadata.environments.staging.url}}.
""",
        encoding="utf-8",
    )
    common = ["--root", str(tmp_path)]

    assert (
        main(
            [
                *common,
                "start",
                "TASK-1",
                "--workflow",
                "capture",
                "--agent",
                "codex",
                "--requirements",
                "requirements",
            ]
        )
        == 0
    )
    capsys.readouterr()
    _complete_init(tmp_path, "TASK-1", capsys)
    assert main([*common, "next", "TASK-1"]) == 0
    instruction = capsys.readouterr().out
    assert "Detect and preserve these project metadata values" in instruction
    assert "project_metadata.environments.staging.url" in instruction
    assert (
        main(
            [
                *common,
                "complete",
                "TASK-1",
                "--summary",
                "Metadata saved.",
                "--metadata",
                "project_metadata.environments.staging.url=https://staging.example.com",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main([*common, "metadata", "--project"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "environments": {"staging": {"url": "https://staging.example.com"}}
    }
    assert json.loads((tmp_path / ".ww/metadata.json").read_text()) == {
        "environments": {"staging": {"url": "https://staging.example.com"}}
    }

    assert (
        main(
            [
                *common,
                "start",
                "TASK-2",
                "--workflow",
                "consume",
                "--agent",
                "codex",
                "--requirements",
                "requirements",
            ]
        )
        == 0
    )
    capsys.readouterr()
    _complete_init(tmp_path, "TASK-2", capsys)
    assert main([*common, "next", "TASK-2"]) == 0
    assert "Deploy to https://staging.example.com." in capsys.readouterr().out

    assert main([*common, "reset", "TASK-1", "--yes"]) == 0
    capsys.readouterr()
    assert main([*common, "metadata", "--project"]) == 0
    assert json.loads(capsys.readouterr().out)["environments"]["staging"]["url"] == (
        "https://staging.example.com"
    )


@pytest.mark.parametrize(
    ("runtime", "role_heading", "delivered_guidance"),
    (
        ("single", "Manager and worker", "not through a subagent"),
        ("auto", "Manager", "Run the displayed manager command yourself"),
    ),
)
def test_start_cli_delivers_runtime_guidance(
    tmp_path: Path,
    capsys,
    runtime: str,
    role_heading: str,
    delivered_guidance: str,
) -> None:
    _project(tmp_path)

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "start",
                f"TASK-{runtime}",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--requirements",
                "requirements",
                "--runtime",
                runtime,
                "--requirements",
                "requirements",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out

    assert f"## {role_heading}: dispatch the `work` assignment" in output
    assert delivered_guidance in output
    assert "Generated by `ww`" in output
    assert "keeps this task's plan and progress" in output


def test_init_creates_an_empty_normalized_workflow_file(tmp_path: Path, capsys) -> None:
    assert main(["--root", str(tmp_path), "init"]) == 0
    output = capsys.readouterr().out
    assert not (tmp_path / ".ww" / "templates").exists()
    assert (tmp_path / "ww.yaml").read_text(encoding="utf-8") == (
        "modes: []\nhandlers: []\nhooks: {}\nworkflows: []\n"
    )
    assert (
        json.loads((tmp_path / "ww.json").read_text())["task_format"] == "TASK-{{uuid}}"
    )
    assert "Create your first workflow" in output
    assert "Define the steps in ww.yaml." in output
    # init configures the standard binary, so the summary names it.
    assert "ww-agentic-workflows workflows" in output
    assert "Optionally keep .ww out of Git" not in output


def test_init_writes_every_setting_with_its_default(tmp_path: Path, capsys) -> None:
    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    capsys.readouterr()

    text = (tmp_path / "ww.json").read_text(encoding="utf-8")
    assert text == DEFAULT_PROJECT_CONFIG_JSON
    assert list(json.loads(text)) == [
        "enabled",
        "runtime",
        "update_check",
        "executable",
        "task_format",
        "limits",
        "agent_hooks",
        "rules",
        "builtins",
        "workflows",
        "projects",
        "extensions",
    ]


def test_init_adds_missing_settings_and_leaves_user_level_ones(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    user = tmp_path / "user"
    user.mkdir()
    (user / "ww.json").write_text('{"runtime": "auto"}', encoding="utf-8")
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(user))
    project = tmp_path / "project"
    project.mkdir()
    (project / "ww.json").write_text(
        '{"limits": {"rounds": 5}, "extensions": {}}', encoding="utf-8"
    )

    assert main(["--root", str(project), "init", "--no-input"]) == 0
    capsys.readouterr()

    settings = json.loads((project / "ww.json").read_text())
    assert settings["limits"] == {"rounds": 5, "fixes": 3}
    assert settings["rules"] == {"scripting": True}
    assert "runtime" not in settings


def test_init_targets_new_directory_and_creates_approved_agent_files(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    class InteractiveInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    config_file = tmp_path / "ww.yaml"
    config_file.write_text("workflows: []\n", encoding="utf-8")
    project = tmp_path / "new-project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        "sys.stdin", InteractiveInput("\nuuid\ny\n" + _agent_answers(".codex"))
    )
    assert main(["init"]) == 0
    output = capsys.readouterr().out
    assert (project / "ww.yaml").is_file()
    assert (project / "ww.json").is_file()
    assert (project / "ww").is_file()
    assert (project / ".codex/skills/ww/SKILL.md").is_file()
    assert not (project / ".claude").exists()
    assert (project / "AGENTS.md").read_text() == "@WW_AGENT_INSTRUCTIONS.md\n"
    assert (project / ".gitignore").read_text() == _GITIGNORE_WITH_WW
    assert "Use Git worktrees?" not in output
    assert "Add exactly .ww/" not in output
    assert "Start a task with:" not in output


def test_init_remembers_agent_answers_and_restores_approved_skill(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    class InteractiveInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(
        "sys.stdin", InteractiveInput("\nuuid\ny\n" + _agent_answers(".codex"))
    )
    assert main(["--root", str(tmp_path), "init"]) == 0
    capsys.readouterr()
    skill = tmp_path / ".codex/skills/ww/SKILL.md"
    skill.unlink()
    (tmp_path / ".claude").mkdir()
    # Empty interactive input fails if any saved choice is asked again.
    monkeypatch.setattr("sys.stdin", InteractiveInput(""))
    assert main(["--root", str(tmp_path), "init"]) == 0
    capsys.readouterr()
    assert skill.is_file()
    assert not (tmp_path / ".claude/skills").exists()
    assert "@WW_AGENT_INSTRUCTIONS.md" in (tmp_path / "CLAUDE.md").read_text()
    choices = json.loads((tmp_path / ".ww/init-choices.json").read_text())
    assert choices["agents"][".codex"] is True
    assert choices["agents"][".claude"] is False


def test_init_does_not_reask_completed_git_and_instruction_setup(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    class InteractiveInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    _git("init", "-q", "-b", "main", ".", cwd=tmp_path)
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "init",
                "--no-input",
                "--no-skills",
                "--link-instructions",
                "--update-gitignore",
                "--no-worktrees",
            ]
        )
        == 0
    )
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", InteractiveInput(""))
    assert main(["--root", str(tmp_path), "init"]) == 0
    output = capsys.readouterr().out
    assert "Add exactly .ww/" not in output
    assert "Add @WW_AGENT_INSTRUCTIONS.md" not in output
    assert (tmp_path / "AGENTS.md").read_text().count("@WW_AGENT_INSTRUCTIONS.md") == 1


def test_init_is_additive_and_restores_missing_files(tmp_path: Path, capsys) -> None:
    (tmp_path / "ww.yaml").write_text(
        "workflows:\n  - name: custom\n    steps: []\n", encoding="utf-8"
    )
    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    capsys.readouterr()
    original = (tmp_path / "ww.yaml").read_text(encoding="utf-8")
    (tmp_path / "ww").unlink()

    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    output = capsys.readouterr().out

    assert (tmp_path / "ww").is_file()
    config_text = (tmp_path / "ww.yaml").read_text(encoding="utf-8")
    assert config_text == original
    assert "Already present and preserved:" in output
    assert "ww" in output


def test_init_enables_git_with_safe_defaults(tmp_path: Path, capsys) -> None:
    _git("init", "-q", "-b", "main", ".", cwd=tmp_path)
    (tmp_path / "seed").write_text("seed\n", encoding="utf-8")
    _git("add", "seed", cwd=tmp_path)
    _git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-qm",
        "seed",
        cwd=tmp_path,
    )
    _git("branch", "master", cwd=tmp_path)

    assert main(["--root", str(tmp_path), "init", "--no-input", "--json"]) == 0
    capsys.readouterr()
    config = json.loads((tmp_path / "ww.json").read_text())
    settings = config["extensions"]["ww/git"]

    assert settings == {
        "commit_format": "{{ww.task.id}}: {{commit_message}}",
        "base_branches": {"default": "master"},
        "separate_branch": True,
        "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
        "worktrees": False,
    }


def test_init_worktree_and_gitignore_choices_are_explicit(
    tmp_path: Path, capsys
) -> None:
    _git("init", "-q", "-b", "main", ".", cwd=tmp_path)

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "init",
                "--no-input",
                "--worktrees",
                "--update-gitignore",
                "--branch-format",
                "bugfix=hotfix/{{ww.task.id}}",
            ]
        )
        == 0
    )
    capsys.readouterr()
    settings = json.loads((tmp_path / "ww.json").read_text())["extensions"]["ww/git"]

    assert (tmp_path / "git-worktrees").is_dir()
    assert settings["worktree_dir"] == "./git-worktrees"
    assert settings["worktree_name_format"] == "{{ww.task.id}}"
    assert settings["branch_name_formats"]["bugfix"] == "hotfix/{{ww.task.id}}"
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == _GITIGNORE_WITH_WW


def test_init_interactive_wizard_collects_project_choices(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    class InteractiveInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    _git("init", "-q", "-b", "main", ".", cwd=tmp_path)
    (tmp_path / "ww.yaml").write_text(
        "workflows:\n  - name: bugfix\n    steps: []\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "sys.stdin",
        InteractiveInput(
            "\ndigit\ny\n\nhotfix/{{ww.task.id}}\ny\ny\n" + _agent_answers()
        ),
    )

    assert main(["--root", str(tmp_path), "init"]) == 0
    capsys.readouterr()
    project = json.loads((tmp_path / "ww.json").read_text())
    settings = project["extensions"]["ww/git"]

    assert project["task_format"] == "TASK-{{digit}}"
    assert settings["branch_name_formats"]["bugfix"] == "hotfix/{{ww.task.id}}"
    assert settings["worktrees"] is True
    assert (tmp_path / "git-worktrees").is_dir()
    assert (tmp_path / ".gitignore").read_text() == _GITIGNORE_WITH_WW


def test_init_completes_an_existing_enabled_worktree_config(
    tmp_path: Path, capsys
) -> None:
    _git("init", "-q", "-b", "main", ".", cwd=tmp_path)
    (tmp_path / "ww.json").write_text(
        json.dumps({"extensions": {"ww/git": {"worktrees": True}}}),
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "init", "--no-input"]) == 0
    capsys.readouterr()
    settings = json.loads((tmp_path / "ww.json").read_text())["extensions"]["ww/git"]

    assert settings["worktree_dir"] == "./git-worktrees"
    assert settings["worktree_name_format"] == "{{ww.task.id}}"
    assert (tmp_path / "git-worktrees").is_dir()


def test_fail_cli_records_agent_functional_error(tmp_path: Path, capsys) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: create-pr
        mcp: github
        description: Create a pull request.
""",
        encoding="utf-8",
    )
    secret = "SYNTHETIC_SECRET_123"

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "start",
                "TASK-1",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--requirements",
                "requirements",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "next", "TASK-1", "--json"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "fail",
                "TASK-1",
                "--error",
                secret,
                "--json",
            ]
        )
        == 1
    )
    output = json.loads(capsys.readouterr().out)

    assert output["status"] == "failed"
    assert secret in output["error"]
    records = [
        json.loads(line)
        for line in (tmp_path / ".ww/executions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failed_invocation = records[-2:]
    assert len({record["invocation_id"] for record in failed_invocation}) == 1
    assert all(secret not in json.dumps(record) for record in failed_invocation)
    assert "<redacted>" in failed_invocation[0]["invocation"]
    assert failed_invocation[-1]["error_code"] == "command_failed"


def test_failed_instruction_returns_exit_code_one(tmp_path: Path, capsys) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: reject
    argv: ["false"]
hooks:
  after_complete:
    - steps: [init]
      name: reject
workflows:
  - name: task
    steps: []
""",
        encoding="utf-8",
    )
    common = ["--root", str(tmp_path)]
    result = main(
        [
            *common,
            "start",
            "TASK-FAIL",
            "-w",
            "task",
            "-a",
            "codex",
            "--requirements",
            "requirements",
        ]
    )
    output = capsys.readouterr().out

    assert result == 1
    assert "### Error" in output
    assert "Completion recorded successfully by `ww`" not in output


def test_complete_on_a_failed_task_returns_the_saved_failure_instruction(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        description: Complete the work.
""",
        encoding="utf-8",
    )
    common = ["--root", str(tmp_path)]
    assert (
        main(
            [
                *common,
                "start",
                "TASK-FAILED",
                "-w",
                "task",
                "-a",
                "codex",
                "--requirements",
                "requirements",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*common, "next", "TASK-FAILED"]) == 0
    capsys.readouterr()
    assert (
        main([*common, "fail", "TASK-FAILED", "--error", "Work could not finish"]) == 1
    )
    capsys.readouterr()

    assert main([*common, "complete", "TASK-FAILED", "--artifact", "ignored"]) == 1
    output = capsys.readouterr()

    assert "# TASK-FAILED · task" in output.out
    assert "Work could not finish" in output.out
    assert output.err == ""


def test_force_next_requires_explicit_operator_confirmation(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: failed-work
        description: Complete the work.
      - name: later-work
        description: Continue after the failed work.
""",
        encoding="utf-8",
    )
    common = ["--root", str(tmp_path)]
    assert (
        main(
            [
                *common,
                "start",
                "TASK-FORCE",
                "-w",
                "task",
                "-a",
                "codex",
                "--requirements",
                "requirements",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*common, "next", "TASK-FORCE"]) == 0
    capsys.readouterr()
    assert main([*common, "fail", "TASK-FORCE", "--error", "outside issue"]) == 1
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "no")
    assert (
        main(
            [
                *common,
                "next",
                "TASK-FORCE",
                "--force",
                "--reason",
                "Outside issue resolved manually",
            ]
        )
        == 1
    )
    cancelled = capsys.readouterr()
    assert prompts == ["Proceed with force? [y/N] "]
    assert "`ww next --force` will skip" in cancelled.err
    assert "Force cancelled: explicit confirmation is required." in cancelled.err

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert (
        main(
            [
                *common,
                "next",
                "TASK-FORCE",
                "--force",
                "--reason",
                "Outside issue resolved manually",
            ]
        )
        == 0
    )
    assert "later-work" in capsys.readouterr().out


def test_an_agent_confirms_a_force_with_yes_and_the_audit_says_so(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: failed-work
        description: Complete the work.
      - name: later-work
        description: Continue after the failed work.
""",
        encoding="utf-8",
    )
    common = ["--root", str(tmp_path)]
    start = ["start", "TASK-YES", "-w", "task", "-a", "codex"]
    assert main([*common, *start, "--requirements", "requirements"]) == 0
    assert main([*common, "next", "TASK-YES"]) == 0
    assert main([*common, "fail", "TASK-YES", "--error", "outside issue"]) == 1
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    force = ["next", "TASK-YES", "--force", "--reason", "Fixed by hand"]

    # No terminal, no --yes: refused at once, nothing read from stdin.
    assert main([*common, *force]) == 1
    refused = capsys.readouterr().err
    assert "force needs the operator's confirmation" in refused
    assert "rerun with --yes only once they have agreed" in refused

    assert main([*common, *force, "--yes"]) == 0
    assert "Confirmed with --yes" in capsys.readouterr().err
    records = [
        json.loads(line)
        for line in (tmp_path / ".ww/executions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    forced = [record for record in records if "--force" in record["invocation"]]
    assert forced[-1]["confirmation"] == "--yes"
    assert "--yes" in forced[-1]["invocation"]


def test_current_reformatted_project_file_compiles_without_running_handlers(
    capsys,
) -> None:
    root = Path(__file__).parents[2]

    assert (
        main(
            [
                "--root",
                str(root),
                "plan",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    items = output["items"]
    assert [(item["name"], item["owner"], item["phase"]) for item in items[:4]] == [
        ("is-git-clean", "ww", "before_start_workflow"),
        ("start-task-branch", "ww", "before_start_workflow"),
        ("create-worktree", "ww", "before_start_workflow"),
        ("init", "agent", "step"),
    ]
    assert [
        (item["name"], item["owner"])
        for item in items
        if item["phase"] == "before_complete_workflow"
    ] == [
        ("update-documentation", "agent"),
        ("update-agent-instructions", "agent"),
        ("git-commit", "ww"),
        ("update-changelog", "agent"),
        ("git-commit", "ww"),
        ("update-workflow-summary", "agent"),
    ]


def test_cli_discovers_the_primary_project_from_a_linked_worktree(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    _git("init", "-q", "-b", "main", ".", cwd=project)
    _git("config", "user.email", "t@e.st", cwd=project)
    _git("config", "user.name", "Test", cwd=project)
    _git("add", "ww.yaml", cwd=project)
    _git("commit", "-qm", "seed", cwd=project)
    linked = tmp_path / "linked-worktree"
    _git("worktree", "add", "-q", "-b", "linked", str(linked), cwd=project)

    assert (
        main(
            [
                "--root",
                str(project),
                "start",
                "TASK-1",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--requirements",
                "requirements",
            ]
        )
        == 0
    )
    capsys.readouterr()
    monkeypatch.chdir(linked)

    assert main(["status", "TASK-1", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {
        "task_id": "TASK-1",
        "workflow": "task",
        "step": "work",
        "step_state": "pending",
        "runtime": "single",
        "agent": "codex",
        "model": "auto",
        "reasoning": "auto",
    }

    assert main(["instruction", "TASK-1", "--json"]) == 0
    instruction = json.loads(capsys.readouterr().out)
    assert instruction["item_status"] == "pending"
    assert "continuation_command" in instruction


def test_cleanup_cli_removes_inactive_lock_files(tmp_path: Path, capsys) -> None:
    _project(tmp_path)
    locks = Storage(tmp_path).locks
    target = tmp_path / ".ww" / "tasks" / "TASK-1"
    with locks.lock(target):
        pass

    assert locks.lock_path(target).exists()
    assert main(["--root", str(tmp_path), "cleanup", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["removed_locks"] >= 1
    assert not locks.lock_path(target).exists()


def test_extension_discovery_error_handled_by_cli_boundary(
    tmp_path: Path, capsys
) -> None:
    _project(tmp_path)
    bad_ext_dir = tmp_path / "ext" / "INVALID_VENDOR" / "demo"
    bad_ext_dir.mkdir(parents=True)
    (bad_ext_dir / "extension.py").write_text("# invalid\n", encoding="utf-8")

    assert main(["--root", str(tmp_path), "workflows"]) == 1
    err = capsys.readouterr().err
    assert "ww error:" in err
    assert "invalid extension identifier in discovery metadata" in err


def test_the_flags_parse() -> None:
    parser = build_parser()

    start = parser.parse_args(
        [
            "start",
            "-w",
            "task",
            "-a",
            "codex",
            "--requirements",
            "Do it.",
            "--branch-strategy",
            "hotfix",
        ]
    )
    interact = parser.parse_args(
        ["interact", "T-1", "--operator-said", "yes", "--agent-said", "ok", "--end"]
    )
    child = parser.parse_args(["start-child", "T-1", "a"])
    forced = parser.parse_args(["next", "T-1", "--force", "--reason", "Skip."])

    assert (start.requirements, start.branch_naming_strategy) == ("Do it.", "hotfix")
    assert (interact.operator, interact.agent, interact.end_interaction) == (
        "yes",
        "ok",
        True,
    )
    assert (child.parent_task_id, child.child_id) == ("T-1", "a")
    assert forced.force_reason == "Skip."
