# SPDX-License-Identifier: GPL-3.0-or-later
"""Composing ww-agentic-workflows.yaml from its imports and configuration levels."""

import re
from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.config.composition import compose_configuration
from ww.config_files import machine_directory
from ww.core_workflows import CATCHALL
from ww.errors import ConfigurationError
from ww.project_config import compose_settings, load_project_config

_WORKFLOW = """workflows:
  - task: Repo task.
    steps:
      - work: Do the work.
"""


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "machine"
    directory.mkdir()
    monkeypatch.setenv("WW_MACHINE_CONFIG_DIR", str(directory))
    return directory


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    return directory


# Imports within one level


def test_a_single_file_passes_through_unchanged(repo: Path) -> None:
    text = "# a comment kept verbatim\n" + _WORKFLOW
    composed = compose_configuration(_write(repo / "ww-agentic-workflows.yaml", text))

    assert composed.text == text
    assert composed.sources == ("ww-agentic-workflows.yaml",)
    assert composed.overrides == ()


def test_the_root_overrides_the_last_import_which_overrides_the_first(
    repo: Path,
) -> None:
    _write(repo / "first.yaml", "banner: first\n")
    _write(repo / "second.yaml", "banner: second\n")
    root = _write(
        repo / "ww-agentic-workflows.yaml",
        "imports: [first.yaml, second.yaml]\n" + _WORKFLOW,
    )
    assert compose_configuration(root).raw["banner"] == "second"

    _write(root, "imports: [first.yaml, second.yaml]\nbanner: root\n")
    composed = compose_configuration(root)

    assert composed.raw["banner"] == "root"
    assert [override.notice for override in composed.overrides] == [
        "banner from first.yaml is overridden by second.yaml.",
        "banner from second.yaml is overridden by ww-agentic-workflows.yaml.",
    ]


@pytest.mark.parametrize("where", ["root", "import", "machine", "local"])
def test_task_format_in_any_yaml_file_points_at_the_settings_file(
    machine: Path, repo: Path, where: str
) -> None:
    files = {
        "root": repo / "ww-agentic-workflows.yaml",
        "import": repo / "ids.yaml",
        "machine": machine / "ww-agentic-workflows.machine.yaml",
        "local": repo / "ww-agentic-workflows.local.yaml",
    }
    _write(files[where], "task_format: X-{digit}\n")
    if where != "root":
        _write(
            repo / "ww-agentic-workflows.yaml",
            ("imports: [ids.yaml]\n" if where == "import" else "") + _WORKFLOW,
        )

    with pytest.raises(
        ConfigurationError,
        match=r"task_format in \S*"
        + re.escape(files[where].name)
        + r" now lives in ww-agentic-workflows.json",
    ):
        load_configuration(repo / "ww-agentic-workflows.yaml")


def test_named_entries_are_replaced_in_place_by_name(repo: Path) -> None:
    _write(
        repo / "base.yaml",
        """modes:
  - economy: Base economy.
  - name: careful
    description: Base careful.
workflows:
  - task: Base task.
    steps:
      - work: Base work.
  - other: Base other.
    steps:
      - work: Other work.
""",
    )
    root = _write(
        repo / "ww-agentic-workflows.yaml",
        """imports: [base.yaml]
modes:
  - name: economy
    description: Root economy.
  - careful: Root careful.
workflows:
  - task: Root task.
    steps:
      - work: Root work.
""",
    )

    raw = compose_configuration(root).raw

    assert raw["modes"] == [
        {"name": "economy", "description": "Root economy."},
        {"careful": "Root careful."},
    ]
    assert [next(iter(entry)) for entry in raw["workflows"]] == ["task", "other"]
    assert raw["workflows"][0]["task"] == "Root task."


def test_a_handler_reusing_an_overridden_handler_still_resolves(repo: Path) -> None:
    _write(
        repo / "base.yaml",
        """handlers:
  - name: tests
    argv: [echo, base]
  - quality:
      steps:
        - tests: ~
""",
    )
    root = _write(
        repo / "ww-agentic-workflows.yaml",
        """imports: [base.yaml]
handlers:
  - name: tests
    argv: [echo, root]
"""
        + _WORKFLOW,
    )

    handlers = compose_configuration(root).raw["handlers"]
    configuration = load_configuration(root)

    assert handlers[0] == {"name": "tests", "argv": ["echo", "root"]}
    assert [handler.name for handler in configuration.handlers] == [
        "tests",
        "quality",
    ]


def test_profiles_merge_by_key_and_hooks_run_in_file_order(repo: Path) -> None:
    _write(
        repo / "base.yaml",
        """profiles:
  fast: Base fast.
  slow: Base slow.
hooks:
  after_complete:
    - name: base-hook
      argv: [echo, base]
""",
    )
    root = _write(
        repo / "ww-agentic-workflows.yaml",
        """imports: [base.yaml]
profiles:
  fast: Root fast.
hooks:
  after_complete:
    - name: root-hook
      argv: [echo, root]
"""
        + _WORKFLOW,
    )

    raw = compose_configuration(root).raw

    assert raw["profiles"] == {"fast": "Root fast.", "slow": "Base slow."}
    assert [hook["name"] for hook in raw["hooks"]["after_complete"]] == [
        "base-hook",
        "root-hook",
    ]


def test_a_name_repeated_within_one_file_is_still_a_duplicate(repo: Path) -> None:
    _write(
        repo / "base.yaml",
        """workflows:
  - task: First.
    steps:
      - work: Work.
  - task: Second.
    steps:
      - work: Work.
""",
    )
    root = _write(repo / "ww-agentic-workflows.yaml", "imports: [base.yaml]\n")

    with pytest.raises(ConfigurationError, match="duplicate workflow name"):
        load_configuration(root)


@pytest.mark.parametrize(
    ("root", "files", "message"),
    [
        (
            "banner: x\nimports: [a.yaml]\n",
            {"a.yaml": "modes: []\n"},
            "imports must come before every key but extends",
        ),
        ("imports: a.yaml\n", {}, "must be a list of file paths"),
        ("imports: ['']\n", {}, "must be a non-empty file path"),
        ("imports: [missing.yaml]\n", {}, "imported file not found: missing.yaml"),
        (
            "imports: [a.yaml, ./a.yaml]\n",
            {"a.yaml": "modes: []\n"},
            "which is already a configuration file or an import",
        ),
        (
            "imports: [ww-agentic-workflows.yaml]\n",
            {},
            "which is already a configuration file or an import",
        ),
        (
            "imports: [a.yaml]\n",
            {"a.yaml": "imports: [b.yaml]\n", "b.yaml": "modes: []\n"},
            "a.yaml cannot import other files",
        ),
        (
            "imports: [a.yaml]\n",
            {"a.yaml": "- a list\n"},
            "a.yaml must contain a mapping",
        ),
        ("imports: [a.yaml]\n", {"a.yaml": "modes: [\n"}, "invalid YAML in a.yaml"),
    ],
)
def test_invalid_imports_are_reported_by_file(
    repo: Path, root: str, files: dict[str, str], message: str
) -> None:
    for name, content in files.items():
        _write(repo / name, content)
    path = _write(repo / "ww-agentic-workflows.yaml", root)

    with pytest.raises(ConfigurationError, match=message):
        compose_configuration(path)


# Configuration levels


def test_the_machine_directory_follows_the_variable_then_xdg_then_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WW_MACHINE_CONFIG_DIR", str(tmp_path / "explicit"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert machine_directory() == tmp_path / "explicit"

    monkeypatch.delenv("WW_MACHINE_CONFIG_DIR")
    assert machine_directory() == tmp_path / "xdg" / "ww-agentic-workflows"

    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert machine_directory() == (
        tmp_path / "home" / ".config" / "ww-agentic-workflows"
    )


def test_levels_fold_from_machine_to_local_with_imports_first(
    machine: Path, repo: Path
) -> None:
    _write(machine / "lib" / "modes.yaml", "modes:\n  - economy: Machine economy.\n")
    _write(
        machine / "ww-agentic-workflows.machine.yaml",
        """imports: [lib/modes.yaml]
workflows:
  - machine-only: From the machine.
    steps:
      - work: Machine work.
""",
    )
    root = _write(repo / "ww-agentic-workflows.yaml", _WORKFLOW)
    _write(repo / "mine.yaml", "modes:\n  - economy: Local economy.\n")
    _write(repo / "ww-agentic-workflows.local.yaml", "imports: [mine.yaml]\n")

    composed = compose_configuration(root)

    assert composed.sources == (
        str(machine / "lib" / "modes.yaml"),
        str(machine / "ww-agentic-workflows.machine.yaml"),
        "ww-agentic-workflows.yaml",
        "mine.yaml",
        "ww-agentic-workflows.local.yaml",
    )
    assert composed.raw["modes"] == [{"economy": "Local economy."}]
    assert [next(iter(entry)) for entry in composed.raw["workflows"]] == [
        "machine-only",
        "task",
    ]
    configuration = load_configuration(root)
    assert [workflow.name for workflow in configuration.workflows] == [
        "machine-only",
        "task",
        CATCHALL,
    ]


@pytest.mark.parametrize("where", ["root", "import"])
def test_extends_false_leaves_out_the_levels_above(
    machine: Path, repo: Path, where: str
) -> None:
    _write(machine / "ww-agentic-workflows.machine.yaml", "banner: machine\n")
    if where == "root":
        root = _write(
            repo / "ww-agentic-workflows.yaml",
            "extends: false\n" + _WORKFLOW,
        )
    else:
        _write(repo / "own.yaml", "extends: false\n")
        root = _write(
            repo / "ww-agentic-workflows.yaml",
            "imports: [own.yaml]\n" + _WORKFLOW,
        )

    composed = compose_configuration(root)

    assert "banner" not in composed.raw
    assert "extends" not in composed.raw
    assert composed.ignored == (str(machine / "ww-agentic-workflows.machine.yaml"),)
    assert composed.notices[0] == (
        f"{machine / 'ww-agentic-workflows.machine.yaml'} is not applied: "
        "a lower level sets extends: false."
    )


def test_extends_true_changes_nothing(machine: Path, repo: Path) -> None:
    _write(machine / "ww-agentic-workflows.machine.yaml", "banner: machine\n")
    root = _write(repo / "ww-agentic-workflows.yaml", "extends: true\n" + _WORKFLOW)

    composed = compose_configuration(root)

    assert composed.raw["banner"] == "machine"
    assert composed.ignored == ()


def test_a_standalone_repo_file_may_say_extends(repo: Path) -> None:
    root = _write(repo / "ww-agentic-workflows.yaml", "extends: false\n" + _WORKFLOW)

    assert load_configuration(root).workflows[0].name == "task"


def test_extends_must_be_a_boolean(repo: Path) -> None:
    root = _write(repo / "ww-agentic-workflows.yaml", "extends: nope\n" + _WORKFLOW)

    with pytest.raises(
        ConfigurationError,
        match="ww-agentic-workflows.yaml: extends must be true or false",
    ):
        compose_configuration(root)


def test_the_repo_file_stays_required(machine: Path, repo: Path) -> None:
    _write(machine / "ww-agentic-workflows.machine.yaml", _WORKFLOW)
    _write(repo / "ww-agentic-workflows.local.yaml", _WORKFLOW)

    with pytest.raises(ConfigurationError, match="workflow configuration not found"):
        load_configuration(repo / "ww-agentic-workflows.yaml")


def test_a_file_cannot_be_imported_by_two_levels(machine: Path, repo: Path) -> None:
    shared = _write(repo / "shared.yaml", "modes: []\n")
    _write(machine / "ww-agentic-workflows.machine.yaml", f"imports: [{shared}]\n")
    root = _write(
        repo / "ww-agentic-workflows.yaml",
        "imports: [shared.yaml]\n" + _WORKFLOW,
    )

    with pytest.raises(ConfigurationError, match="already a configuration file"):
        compose_configuration(root)


def test_settings_deep_merge_across_levels(machine: Path, repo: Path) -> None:
    _write(
        machine / "ww-agentic-workflows.machine.json",
        '{"max_rounds": 5, "extensions": {"ww/git": '
        '{"worktrees": true, "base_branches": {"default": "main"}}}, '
        '"projects": [{"name": "a", "path": "./a"}]}',
    )
    repo_file = _write(
        repo / "ww-agentic-workflows.json",
        '{"extensions": {"ww/git": {"worktrees": false}}}',
    )
    _write(
        repo / "ww-agentic-workflows.local.json",
        '{"projects": [{"name": "b", "path": "./b"}]}',
    )

    raw, sources = compose_settings(repo_file)

    assert raw == {
        "max_rounds": 5,
        "extensions": {
            "ww/git": {"worktrees": False, "base_branches": {"default": "main"}}
        },
        "projects": [{"name": "b", "path": "./b"}],
    }
    assert sources == (
        machine / "ww-agentic-workflows.machine.json",
        repo_file,
        repo / "ww-agentic-workflows.local.json",
    )
    settings = load_project_config(repo_file)
    assert settings.max_rounds == 5
    assert [project.name for project in settings.projects] == ["b"]


def test_settings_apply_from_the_machine_without_a_repo_file(
    machine: Path, repo: Path
) -> None:
    _write(machine / "ww-agentic-workflows.machine.json", '{"max_rounds": 7}')

    assert load_project_config(repo / "ww-agentic-workflows.json").max_rounds == 7


def test_settings_errors_name_the_files_read(machine: Path, repo: Path) -> None:
    _write(machine / "ww-agentic-workflows.machine.json", '{"max_rounds": 0}')
    repo_file = _write(repo / "ww-agentic-workflows.json", "{}")

    with pytest.raises(ConfigurationError) as error:
        load_project_config(repo_file)

    message = str(error.value)
    assert "ww-agentic-workflows.machine.json + " in message
    assert "max_rounds must be a positive integer" in message


def test_an_invalid_settings_level_is_reported_by_path(
    machine: Path, repo: Path
) -> None:
    _write(machine / "ww-agentic-workflows.machine.json", "[]")

    with pytest.raises(ConfigurationError, match="must contain a JSON object"):
        load_project_config(repo / "ww-agentic-workflows.json")
