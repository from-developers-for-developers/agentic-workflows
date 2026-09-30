# SPDX-License-Identifier: GPL-3.0-or-later
"""Configured projects: task working directories across several repositories."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.errors import ConfigurationError, StateError
from ww.service import WorkflowService
from ww.storage import Storage

PROJECTS = [
    {"name": "backend", "path": "./backend", "description": "Python API service."},
    {"name": "frontend", "path": "./frontend"},
]
PROJECTS_AS_JSON = [{"description": "", **project} for project in PROJECTS]
WORKFLOWS = """handlers:
  - name: where
    argv: [pwd]
workflows:
  - name: feature
    steps:
      - develop: Implement it.
        hooks:
          after_complete:
            - name: where
  - name: triage
    steps:
      - choose: Choose the follow-up workflow.
        provide:
          - name: workflow
        hooks:
          after_complete:
            - workflow: "{{workflow}}"
  - name: parent
    steps:
      - split: Split the work.
        children:
          workflow: feature
"""


def _workspace(tmp_path: Path, git: bool = False) -> Path:
    for name in ("backend", "frontend"):
        repo = tmp_path / name
        repo.mkdir()
        if git:
            _git(repo, "init", "-q", "-b", "main", ".")
            _git(repo, "config", "user.email", "t@e.st")
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "commit.gpgsign", "false")
            (repo / "README.md").write_text("seed\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "seed")
    (tmp_path / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (tmp_path / "ww-agentic-workflows.json").write_text(
        json.dumps({"enabled": True, "projects": PROJECTS, "extensions": {}}),
        encoding="utf-8",
    )
    return tmp_path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _stdout(service: WorkflowService, task_id: str) -> str:
    reference = next(
        entry["command_output"]
        for entry in service.artifacts(task_id)
        if entry.get("stream") == "stdout"
    )
    return service.tasks.read_command_output(reference).strip()


def test_projects_are_parsed_and_listed_by_discover(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path)

    assert main(["--root", str(root), "discover"]) == 0
    text = capsys.readouterr().out
    assert main(["--root", str(root), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert "- `backend` at `./backend` — Python API service." in text
    assert "- `frontend` at `./frontend`" in text
    assert "- `--project`: `backend`, `frontend`. Omit it to work in the root." in text
    assert report["projects"] == [
        {
            "name": "backend",
            "path": "./backend",
            "description": "Python API service.",
            "branch_strategies": [],
            "task_format": None,
        },
        {
            "name": "frontend",
            "path": "./frontend",
            "description": "",
            "branch_strategies": [],
            "task_format": None,
        },
    ]
    assert "may carry an `extensions` section" in text


def test_discover_without_projects_shows_no_project_option(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "discover"]) == 0
    text = capsys.readouterr().out

    assert "## Projects" not in text
    assert "`--project`" not in text


@pytest.mark.parametrize(
    ("projects", "message"),
    [
        ({}, "projects must be a list"),
        (["backend"], "must be an object"),
        ([{"name": "backend"}], "path must be a non-empty string"),
        ([{"name": "Bad Name", "path": "./x"}], "normalized name"),
        ([{"name": "a", "path": "./x", "cwd": "./x"}], "unknown key"),
        ([{"name": "a", "path": "./x", "description": 3}], "description must be"),
        (
            [{"name": "a", "path": "./x"}, {"name": "a", "path": "./y"}],
            "duplicate name 'a'",
        ),
    ],
)
def test_invalid_projects_are_rejected(
    tmp_path: Path, projects: object, message: str
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )
    (tmp_path / "ww-agentic-workflows.json").write_text(
        json.dumps({"projects": projects}), encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match=message):
        WorkflowService(Storage(tmp_path)).start("task", "T", agent="codex")


def test_a_task_started_in_a_project_works_there(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    service = WorkflowService(Storage(root))

    start_after_init(service, "feature", "T1", agent="codex", project="backend")
    develop = service.next("T1")
    service.complete("T1", artifact="done", summary_for_next="Done.")

    assert develop.working_directory == str((root / "backend").resolve())
    assert _stdout(service, "T1") == str((root / "backend").resolve())
    state = service.tasks.read_execution_state("T1", "01-feature")
    assert state is not None
    assert dict(state.workflow_values)["__project"] == "backend"
    assert not (root / ".ww/tasks/T1").exists() or (root / ".ww").exists()


def test_a_task_without_a_project_still_works_in_the_root(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    service = WorkflowService(Storage(root))

    start_after_init(service, "feature", "T1", agent="codex")
    service.next("T1")
    service.complete("T1", artifact="done", summary_for_next="Done.")

    assert _stdout(service, "T1") == str(root.resolve())


def test_unknown_or_missing_project_directories_are_rejected(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "frontend").rmdir()
    service = WorkflowService(Storage(root))

    with pytest.raises(StateError, match="unknown project 'api'; configured projects"):
        service.start("feature", "T1", agent="codex", project="api")
    with pytest.raises(StateError, match="directory does not exist"):
        service.start("feature", "T1", agent="codex", project="frontend")
    assert not service.tasks.task_exists("T1")

    (root / "ww-agentic-workflows.json").write_text("{}", encoding="utf-8")
    with pytest.raises(StateError, match="no projects are configured"):
        WorkflowService(Storage(root)).start(
            "feature", "T2", agent="codex", project="x"
        )


def test_a_handoff_successor_keeps_the_project(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    service = WorkflowService(Storage(root))
    start_after_init(service, "triage", "T1", agent="codex", project="frontend")
    service.next("T1")

    successor = service.complete(
        "T1",
        variables=(("workflow", "feature"),),
        artifact="chosen",
        summary_for_next="Done.",
    )

    assert successor.workflow == "feature"
    state = service.tasks.read_execution_state("T1", "02-feature")
    assert state is not None
    assert state.working_directory == "frontend"
    assert dict(state.workflow_values)["__project"] == "frontend"


def test_update_child_moves_a_pending_child_to_another_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path)
    service = WorkflowService(Storage(root))
    start_after_init(service, "parent", "P", agent="codex")
    service.next("P")
    service.add_child("P", "web", "Web part", project="backend")
    service.complete("P", artifact="split", summary_for_next="Done.")
    service.next("P")

    arguments = ["--root", str(root), "update-child", "P", "web"]
    assert main([*arguments, "--project", "frontend", "--text", "Web app"]) == 0
    updated = json.loads(capsys.readouterr().out)
    assert (updated["project"], updated["description"]) == ("frontend", "Web app")

    service.start_child("P", "web")
    web = service.tasks.read_execution_state("P/web", "01-feature")
    assert web is not None
    assert web.working_directory == "frontend"
    assert main([*arguments, "--text", "Too late"]) == 1
    assert "child 'web' is in_progress" in capsys.readouterr().err


def test_children_run_in_their_own_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path)
    service = WorkflowService(Storage(root))
    start_after_init(service, "parent", "P", agent="codex")
    split = service.next("P")
    assert "--project <project>" in (split.action_text or "")
    assert "Configured projects: `backend`, `frontend`." in (split.action_text or "")

    assert (
        main(
            [
                "--root",
                str(root),
                "add-child",
                "P",
                "--id",
                "api",
                "--description",
                "API part",
                "--project",
                "backend",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["project"] == "backend"
    service.add_child("P", "web", "Web part")
    with pytest.raises(StateError, match="unknown project"):
        service.add_child("P", "bad", "Bad part", project="nope")
    service.complete("P", artifact="split", summary_for_next="Done.")
    service.next("P")

    service.start_child("P", "api")
    api = service.tasks.read_execution_state("P/api", "01-feature")
    assert api is not None
    assert api.working_directory == "backend"
    service.next("P/api")
    service.complete("P/api", artifact="done", summary_for_next="Done.")
    assert _stdout(service, "P/api") == str((root / "backend").resolve())
    service.next("P/api")
    service.complete(
        "P/api",
        variables=(("summary", "api done"),),
        summary_for_next="Done.",
    )

    service.start_child("P", "web")
    web = service.tasks.read_execution_state("P/web", "01-feature")
    assert web is not None
    assert web.working_directory is None


def test_git_handlers_act_on_each_projects_repository(tmp_path: Path) -> None:
    root = _workspace(tmp_path, git=True)
    _git(root / "frontend", "branch", "-m", "main", "master")
    feature = (
        "workflows:\n  - name: feature\n    steps:\n      - develop: Implement it.\n"
    )
    git_hooks = (
        "hooks:\n  before_start_workflow:\n    - workflows: [feature]\n"
        "      handlers:\n        - ext/ww/git/handlers:is-git-clean: ~\n"
        "        - ext/ww/git/handlers:start-task-branch: ~\n"
        "  before_complete_workflow:\n    - workflows: [feature]\n"
        "      handlers:\n        - ext/ww/git/handlers:git-commit: ~\n"
        "        - ext/ww/git/handlers:return-to-base-branch: ~\n"
    )
    (root / "ww-agentic-workflows.yaml").write_text(
        WORKFLOWS.replace(feature, git_hooks + feature), encoding="utf-8"
    )
    (root / "ww-agentic-workflows.json").write_text(
        json.dumps(
            {
                "projects": PROJECTS,
                "extensions": {
                    "ww/git": {
                        "use_separate_branch": True,
                        "base_branches": {"default": "main"},
                        "branch_name_formats": {"default": "feature/{{task_id}}"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    # The frontend repository states its own conventions; the rest of its file
    # describes it as a ww root of its own and is ignored here.
    _commit_settings(
        root / "frontend",
        {
            "enabled": False,
            "extensions": {
                "ww/git": {
                    "base_branches": {"default": "master"},
                    "commit_format": "[{{task_id}}] {{commit_message}}",
                }
            },
        },
    )
    service = WorkflowService(Storage(root))

    def finish(task: str, message: str) -> None:
        instruction = service.status(task)
        while instruction.status not in {"completed", "failed"}:
            assert instruction.error is None, instruction.error
            if instruction.item_status == "pending":
                instruction = service.next(task)
                continue
            values = tuple(
                (value.name, message if value.name == "commit_message" else "done")
                for value in instruction.required_values
            )
            instruction = service.complete(
                task,
                variables=values,
                artifact=None if instruction.status == "awaiting_input" else "x",
                summary_for_next="Done.",
            )
        assert instruction.status == "completed"

    service.start("feature", "F1", agent="codex", project="frontend")
    assert _git(root / "frontend", "rev-parse", "--abbrev-ref", "HEAD") == "feature/f1"
    assert _git(root / "backend", "rev-parse", "--abbrev-ref", "HEAD") == "main"
    service.next("F1")
    (root / "frontend/f.txt").write_text("x\n", encoding="utf-8")
    finish("F1", "Frontend change")
    assert (
        _git(root / "frontend", "log", "--format=%s", "feature/f1").splitlines()[0]
        == "[F1] Frontend change"
    )
    assert (
        _git(root / "frontend", "merge-base", "--is-ancestor", "master", "feature/f1")
        == ""
    )
    assert _git(root / "frontend", "rev-parse", "--abbrev-ref", "HEAD") == "master"

    start_after_init(service, "parent", "P", agent="codex")
    service.next("P")
    service.add_child("P", "api", "Backend part", project="backend")
    service.complete("P", artifact="split", summary_for_next="Done.")
    service.next("P")
    service.start_child("P", "api")
    assert (
        _git(root / "backend", "rev-parse", "--abbrev-ref", "HEAD") == "feature/p/api"
    )
    service.next("P/api")
    (root / "backend/b.txt").write_text("y\n", encoding="utf-8")
    finish("P/api", "Backend part")
    assert (
        _git(root / "backend", "log", "--format=%s", "feature/p/api").splitlines()[0]
        == "P/api: Backend part"
    )
    assert _git(root / "backend", "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert service.status("P").status == "completed"
    assert not (root / ".git").exists()


def test_projects_catalog_and_variables(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path)
    (root / "ww-agentic-workflows.yaml").write_text(
        WORKFLOWS.replace(
            "      - develop: Implement it.\n",
            "      - develop: Implement {{__project}} in {{__project_dir}} "
            "among {{__projects}}.\n",
        ),
        encoding="utf-8",
    )

    assert main(["--root", str(root), "projects"]) == 0
    assert json.loads(capsys.readouterr().out) == {"projects": PROJECTS_AS_JSON}

    service = WorkflowService(Storage(root))
    start_after_init(service, "feature", "T1", agent="codex", project="frontend")
    assert service.next("T1").action_text == (
        f"Implement frontend in {(root / 'frontend').resolve()} among backend,frontend."
    )
    start_after_init(service, "feature", "T2", agent="codex")
    assert service.next("T2").action_text == "Implement  in  among backend,frontend."


# A project's own extension settings


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _commit_settings(repo: Path, payload: object) -> None:
    """Give a project repository its own settings file, committed like any."""
    _write_json(repo / "ww-agentic-workflows.json", payload)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "Add ww settings")


ROOT_GIT_SETTINGS = {
    "use_separate_branch": True,
    "base_branches": {"default": "main"},
    "branch_name_formats": {"default": "feature/{{task_id}}"},
    "commit_format": "{{task_id}}: {{commit_message}}",
}


def test_extension_items_freeze_the_settings_of_the_directory_they_act_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path)
    (root / "ww-agentic-workflows.yaml").write_text(
        WORKFLOWS.replace(
            "          after_complete:\n            - name: where\n",
            "          after_complete:\n            - name: where\n"
            "            - ext/ww/git/handlers:git-commit: ~\n"
            "            - ext/ww/git/handlers:git-commit: ~\n"
            "              workdir: root\n"
            "            - ext/ww/git/handlers:git-commit: ~\n"
            "              workdir: project\n",
        ),
        encoding="utf-8",
    )
    _write_json(
        root / "ww-agentic-workflows.json",
        {"projects": PROJECTS, "extensions": {"ww/git": ROOT_GIT_SETTINGS}},
    )
    _write_json(
        root / "backend" / "ww-agentic-workflows.json",
        {
            "extensions": {
                "ww/git": {"commit_format": "[{{task_id}}] {{commit_message}}"}
            }
        },
    )
    service = WorkflowService(Storage(root))

    def formats(task_id: str) -> dict[str, str]:
        snapshot = service.tasks.read_plan_snapshot(task_id, "01-feature")
        assert snapshot is not None
        return {
            item.workdir: item.operation.payload.settings["commit_format"]
            for item in snapshot.plan.items
            if item.kind == "extension"
        }

    start_after_init(service, "feature", "T1", agent="codex", project="backend")
    assert formats("T1") == {
        "task": "[{{task_id}}] {{commit_message}}",
        "project": "[{{task_id}}] {{commit_message}}",
        "root": "{{task_id}}: {{commit_message}}",
    }
    # The rest of the root's section is kept: the project stated only what differs.
    snapshot = service.tasks.read_plan_snapshot("T1", "01-feature")
    assert snapshot is not None
    settings = next(
        item.operation.payload.settings
        for item in snapshot.plan.items
        if item.kind == "extension" and item.workdir == "task"
    )
    assert settings["branch_name_formats"] == {"default": "feature/{{task_id}}"}

    start_after_init(service, "feature", "T2", agent="codex", project="frontend")
    assert set(formats("T2").values()) == {"{{task_id}}: {{commit_message}}"}
    start_after_init(service, "feature", "T3", agent="codex")
    assert set(formats("T3").values()) == {"{{task_id}}: {{commit_message}}"}

    # ``plan --project`` compiles the plan a task in that project would get.
    assert main(
        ["--root", str(root), "plan", "-w", "feature", "-a", "codex", "--json",
         "--project", "backend"]
    ) == 0
    assert "[{{task_id}}] {{commit_message}}" in capsys.readouterr().out
    assert main(
        ["--root", str(root), "plan", "-w", "feature", "-a", "codex", "--json"]
    ) == 0
    assert "[{{task_id}}] {{commit_message}}" not in capsys.readouterr().out


def test_a_projects_worktree_settings_open_its_worktree_beside_the_project(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path, git=True)
    (root / "ww-agentic-workflows.yaml").write_text(
        "hooks:\n  before_start_workflow:\n    - workflows: [feature]\n"
        "      handlers:\n        - ext/ww/git/handlers:is-git-clean: ~\n"
        "        - ext/ww/git/handlers:start-task-branch: ~\n"
        "        - ext/ww/git/handlers:create-worktree: ~\n" + WORKFLOWS,
        encoding="utf-8",
    )
    _write_json(
        root / "ww-agentic-workflows.json",
        {"projects": PROJECTS, "extensions": {"ww/git": ROOT_GIT_SETTINGS}},
    )
    _commit_settings(
        root / "backend",
        {
            "extensions": {
                "ww/git": {"worktrees": True, "worktree_dir": "../backend-wt"}
            }
        },
    )
    service = WorkflowService(Storage(root))

    start_after_init(service, "feature", "B1", agent="codex", project="backend")
    service.next("B1")
    service.complete("B1", artifact="done", summary_for_next="Done.")
    worktree = (root / "backend-wt").resolve()
    assert _stdout(service, "B1").startswith(str(worktree))
    assert str(worktree) in _git(root / "backend", "worktree", "list")
    assert _git(root / "backend", "rev-parse", "--abbrev-ref", "HEAD") == "main"

    # The frontend carries no settings of its own, so the root's apply: a
    # branch in the checkout itself and no worktree.
    start_after_init(service, "feature", "F1", agent="codex", project="frontend")
    service.next("F1")
    service.complete("F1", artifact="done", summary_for_next="Done.")
    assert _stdout(service, "F1") == str((root / "frontend").resolve())
    assert len(_git(root / "frontend", "worktree", "list").splitlines()) == 1
    assert _git(root / "frontend", "rev-parse", "--abbrev-ref", "HEAD") == "feature/f1"


def test_lint_plan_and_the_settings_command_show_a_projects_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path)
    _write_json(
        root / "ww-agentic-workflows.json",
        {"projects": PROJECTS, "extensions": {"ww/git": ROOT_GIT_SETTINGS}},
    )
    _write_json(
        root / "backend" / "ww-agentic-workflows.json",
        {
            "extensions": {
                "ww/git": {"commit_format": "[{{task_id}}] {{commit_message}}"}
            }
        },
    )
    _write_json(
        root / "backend" / "ww-agentic-workflows.local.json",
        {"extensions": {"ww/git": {"worktrees": True, "worktree_dir": "../wt"}}},
    )

    assert main(["--root", str(root), "lint"]) == 0
    assert capsys.readouterr().out == (
        "ww-agentic-workflows.yaml is valid.\n"
        "Configuration files: ww-agentic-workflows.yaml, ww-agentic-workflows.json\n"
        "Project backend extension settings: backend/ww-agentic-workflows.json, "
        "backend/ww-agentic-workflows.local.json\n"
    )

    assert main(["--root", str(root), "plan", "-w", "feature", "-a", "codex"]) == 0
    assert capsys.readouterr().out.endswith(
        "Configuration files: ww-agentic-workflows.yaml, ww-agentic-workflows.json\n"
        "Project backend extension settings: backend/ww-agentic-workflows.json, "
        "backend/ww-agentic-workflows.local.json\n"
    )

    assert main(["--root", str(root), "extension", "ww/git", "settings"]) == 0
    assert json.loads(capsys.readouterr().out)["commit_format"] == (
        "{{task_id}}: {{commit_message}}"
    )
    assert (
        main(
            ["--root", str(root), "extension", "ww/git", "settings",
             "--project", "backend"]
        )
        == 0
    )
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["commit_format"] == "[{{task_id}}] {{commit_message}}"
    assert resolved["worktrees"] is True
    assert resolved["worktree_dir"] == "../wt"
    assert resolved["branch_name_formats"] == {"default": "feature/{{task_id}}"}

    _write_json(
        root / "backend" / "ww-agentic-workflows.json",
        {"extensions": {"acme/nope": {}}},
    )
    assert main(["--root", str(root), "lint"]) != 0
    assert (
        "backend/ww-agentic-workflows.json (project 'backend') configures unknown "
        "extension 'acme/nope'"
    ) in capsys.readouterr().err


# A project's own task ID format


def test_generated_ids_follow_the_projects_task_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path)
    _write_json(
        root / "ww-agentic-workflows.json",
        {"projects": PROJECTS, "task_format": "ROOT-{digit}", "extensions": {}},
    )
    _write_json(
        root / "backend" / "ww-agentic-workflows.json", {"task_format": "BE-{digit}"}
    )
    service = WorkflowService(Storage(root))

    assert start_after_init(service, "feature", None, agent="codex").task_id == "ROOT-1"
    assert (
        start_after_init(
            service, "feature", None, agent="codex", project="backend"
        ).task_id
        == "BE-1"
    )
    assert (
        start_after_init(
            service, "feature", None, agent="codex", project="backend"
        ).task_id
        == "BE-2"
    )
    # The frontend carries no format of its own, so the root's applies.
    assert (
        start_after_init(
            service, "feature", None, agent="codex", project="frontend"
        ).task_id
        == "ROOT-2"
    )

    start_after_init(service, "parent", "P", agent="codex")
    service.next("P")
    assert service.add_child("P", None, "Backend part", project="backend").id == "BE-1"
    assert service.add_child("P", None, "Web part", project="frontend").id == "ROOT-1"
    assert service.add_child("P", None, "Root part").id == "ROOT-2"

    assert main(["--root", str(root), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [(entry["name"], entry["task_format"]) for entry in report["projects"]] == [
        ("backend", "BE-{digit}"),
        ("frontend", None),
    ]
    assert main(["--root", str(root), "discover"]) == 0
    text = capsys.readouterr().out
    expected = (
        "- `backend` at `./backend` — Python API service. Generated task IDs there "
        "follow `BE-{digit}`."
    )
    assert expected in text
    assert "- `frontend` at `./frontend`\n" in text


def test_a_project_may_require_explicit_ids_while_the_root_generates_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _workspace(tmp_path)
    _write_json(
        root / "ww-agentic-workflows.json",
        {"projects": PROJECTS, "task_format": "ROOT-{digit}", "extensions": {}},
    )
    _write_json(
        root / "backend" / "ww-agentic-workflows.json", {"task_format": "explicit"}
    )
    service = WorkflowService(Storage(root))

    assert start_after_init(service, "feature", None, agent="codex").task_id == "ROOT-1"
    with pytest.raises(StateError, match="requires an explicit task ID"):
        service.start("feature", None, agent="codex", project="backend")
    assert (
        start_after_init(
            service, "feature", "BE-7", agent="codex", project="backend"
        ).task_id
        == "BE-7"
    )

    assert main(["--root", str(root), "discover"]) == 0
    text = capsys.readouterr().out
    assert (
        "- `backend` at `./backend` — Python API service. Tasks there require an "
        "explicit ID."
        in text
    )
    # The root's guidance still describes the root: generated IDs are fine there.
    assert "ww never generates one here" not in text
