# SPDX-License-Identifier: GPL-3.0-or-later
"""ww.json: loading, validation, and per-extension isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.errors import ConfigurationError
from ww.project_config import (
    Limits,
    ProjectConfig,
    ProjectDefinition,
    load_project_config,
    load_project_settings,
    overlay_settings,
)


def write(root: Path, payload: object) -> Path:
    path = root / "ww.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_an_absent_file_yields_defaults(tmp_path: Path) -> None:
    config = load_project_config(tmp_path / "ww.json")

    assert config.extensions == {}
    assert config.settings_for("ww/git") == {}
    assert config.limits == Limits(rounds=3, fixes=3)
    assert config.rule_approval == "operator"


@pytest.mark.parametrize("value", ["operator", "check", "auto"])
def test_the_rule_approval_loads_from_project_config(
    tmp_path: Path, value: str
) -> None:
    path = write(tmp_path, {"rules": {"approval": value}})

    assert load_project_config(path).rule_approval == value


def test_the_fix_limit_loads_from_project_config(tmp_path: Path) -> None:
    path = write(tmp_path, {"limits": {"fixes": 5}})

    assert load_project_config(path).limits == Limits(rounds=3, fixes=5)


def test_global_loop_limit_loads_from_project_config(tmp_path: Path) -> None:
    path = write(tmp_path, {"limits": {"rounds": 7}})

    assert load_project_config(path).limits == Limits(rounds=7, fixes=3)


def test_extension_settings_load(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        {"extensions": {"ww/git": {"base_branches": {"default": "master"}}}},
    )

    config = load_project_config(path)

    assert config.settings_for("ww/git") == {"base_branches": {"default": "master"}}


def test_a_bare_name_resolves_to_the_extension(tmp_path: Path) -> None:
    path = write(
        tmp_path, {"extensions": {"git": {"base_branches": {"default": "master"}}}}
    )

    assert load_project_config(path).settings_for("ww/git") == {
        "base_branches": {"default": "master"}
    }


def test_an_extension_sees_only_its_own_section(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        {"extensions": {"ww/git": {"a": 1}, "acme/notes": {"b": 2}}},
    )

    config = load_project_config(path)

    assert config.settings_for("ww/git") == {"a": 1}
    assert config.settings_for("acme/notes") == {"b": 2}


def test_extension_settings_are_returned_as_an_owned_deep_copy(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        {"extensions": {"ww/git": {"nested": {"branches": ["main"]}}}},
    )
    config = load_project_config(path)

    settings = config.settings_for("ww/git")
    settings["nested"]["branches"].append("changed")

    assert config.settings_for("ww/git") == {"nested": {"branches": ["main"]}}


def test_a_section_naming_no_installed_extension_is_an_error() -> None:
    config = ProjectConfig(extensions={"ww/absent": {}})

    with pytest.raises(ConfigurationError) as error:
        config.validate_against(("ww/git",))

    # A block that silently applies to nothing looks configured and is not.
    assert "configures unknown extension 'ww/absent'" in str(error.value)
    assert "installed: ww/git" in str(error.value)


def test_a_bare_name_matching_two_vendors_is_ambiguous() -> None:
    config = ProjectConfig(extensions={"git": {}})

    with pytest.raises(ConfigurationError) as error:
        config.validate_against(("ww/git", "acme/git"))

    assert "is ambiguous" in str(error.value)
    assert "acme/git, ww/git" in str(error.value)


def test_a_bare_name_matching_one_vendor_validates() -> None:
    ProjectConfig(extensions={"git": {}}).validate_against(("ww/git",))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "must contain a JSON object"),
        ({"nope": 1}, "unknown key(s): nope"),
        ({"enabled": "yes"}, 'enabled must be true, false, or "on_request"'),
        ({"extensions": []}, "extensions must be an object"),
        ({"extensions": {"ww/git": 5}}, "must be an object of settings"),
        ({"max_rounds": 3}, "unknown key(s): max_rounds"),
        ({"limits": 3}, "limits must be an object"),
        ({"limits": {"max_fixes": 3}}, "limits has unknown key(s): max_fixes"),
        ({"limits": {"rounds": 0}}, "limits.rounds must be a positive integer"),
        ({"limits": {"rounds": True}}, "limits.rounds must be a positive integer"),
        ({"limits": {"rounds": "3"}}, "limits.rounds must be a positive integer"),
        ({"limits": {"fixes": 0}}, "limits.fixes must be a positive integer"),
        ({"limits": {"fixes": False}}, "limits.fixes must be a positive integer"),
        ({"rules": "auto"}, "rules must be an object"),
        ({"rules": {"approve": "auto"}}, "rules has unknown key(s): approve"),
        (
            {"rules": {"approval": "never"}},
            'rules.approval must be one of: "operator", "check", "auto"',
        ),
    ],
)
def test_a_malformed_file_is_rejected(
    tmp_path: Path, payload: object, message: str
) -> None:
    path = write(tmp_path, payload)

    with pytest.raises(ConfigurationError) as error:
        load_project_config(path)

    assert message in str(error.value)


def test_invalid_json_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "ww.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(ConfigurationError) as error:
        load_project_config(path)

    assert "ww.json" in str(error.value)


@pytest.mark.parametrize("enabled", [True, False])
def test_enabled_is_read_and_defaults_to_true(tmp_path: Path, enabled: bool) -> None:
    assert load_project_config(write(tmp_path, {"enabled": enabled})).enabled is enabled
    assert load_project_config(write(tmp_path, {})).enabled is True
    assert load_project_config(tmp_path / "absent.json").enabled is True


@pytest.mark.parametrize(
    ("enabled", "disabled", "on_request"),
    [(True, False, False), (False, True, False), ("on_request", False, True)],
)
def test_enabled_takes_three_values_read_through_properties(
    tmp_path: Path, enabled: object, disabled: bool, on_request: bool
) -> None:
    config = load_project_config(write(tmp_path, {"enabled": enabled}))

    assert config.enabled == enabled
    assert config.disabled is disabled
    assert config.on_request is on_request


def test_builtin_workflows_are_enabled_unless_switched_off(tmp_path: Path) -> None:
    assert load_project_config(tmp_path / "absent.json").workflow_enabled("catchall")
    config = load_project_config(
        write(tmp_path, {"workflows": {"catchall": {"enabled": False}}})
    )

    assert not config.workflow_enabled("catchall")


@pytest.mark.parametrize(
    ("workflows", "message"),
    [
        ([], "workflows must be an object"),
        ({"task": {"enabled": False}}, "unknown name.*built-in workflows: catchall"),
        ({"catchall": False}, "workflows.catchall must be an object"),
        ({"catchall": {"model": "x"}}, "unknown key"),
        ({"catchall": {"enabled": "no"}}, "enabled must be true or false"),
    ],
)
def test_the_builtin_workflow_switches_are_validated(
    tmp_path: Path, workflows: object, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_project_config(write(tmp_path, {"workflows": workflows}))


# A configured project's own settings files


def _workspace(
    tmp_path: Path, repo: object | None = None, local: object | None = None
) -> ProjectDefinition:
    directory = tmp_path / "backend"
    directory.mkdir()
    for name, payload in (
        ("ww.json", repo),
        ("ww.local.json", local),
    ):
        if payload is not None:
            (directory / name).write_text(
                payload if isinstance(payload, str) else json.dumps(payload),
                encoding="utf-8",
            )
    return ProjectDefinition("backend", "./backend")


def test_a_project_without_settings_files_has_no_sections(tmp_path: Path) -> None:
    loaded = load_project_settings(tmp_path, _workspace(tmp_path))

    assert loaded.project == "backend"
    assert loaded.sources == ()
    assert loaded.sections.settings_for("ww/git") == {}


def test_only_the_extensions_section_of_a_project_file_is_read(
    tmp_path: Path,
) -> None:
    project = _workspace(
        tmp_path,
        {
            "enabled": False,
            "runtime": "auto",
            "executable": "elsewhere",
            "projects": [{"name": "nested", "path": "./nested"}],
            "workflows": {"catchall": {"enabled": False}},
            "something_new": True,
            "extensions": {
                "ww/git": {"commit_format": "[{{ww.task.id}}] {{commit_message}}"}
            },
        },
    )

    loaded = load_project_settings(tmp_path, project)

    assert loaded.sources == (tmp_path / "backend" / "ww.json",)
    assert loaded.sections.sections == {
        "ww/git": {"commit_format": "[{{ww.task.id}}] {{commit_message}}"}
    }


def test_a_projects_local_file_extends_its_repo_file(tmp_path: Path) -> None:
    project = _workspace(
        tmp_path,
        {"extensions": {"ww/git": {"worktrees": True, "worktree_dir": "./wt"}}},
        {"extensions": {"ww/git": {"worktree_dir": "../elsewhere"}, "git": {}}},
    )

    loaded = load_project_settings(tmp_path, project)

    assert loaded.sources == (
        tmp_path / "backend" / "ww.json",
        tmp_path / "backend" / "ww.local.json",
    )
    assert loaded.sections.settings_for("ww/git") == {
        "worktrees": True,
        "worktree_dir": "../elsewhere",
    }
    assert loaded.sections.source == (
        "backend/ww.json + backend/ww.local.json"
        " (project 'backend')"
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("[]", "backend/ww.json must contain a JSON object"),
        ("{not json", "invalid .*backend/ww.json"),
        (
            {"extensions": []},
            r"backend/ww.json \(project 'backend'\)\.extensions "
            "must be an object",
        ),
        (
            {"extensions": {"ww/git": "yes"}},
            r"\(project 'backend'\)\.extensions\['ww/git'\] must be an object",
        ),
    ],
)
def test_invalid_project_files_name_the_project(
    tmp_path: Path, payload: object, message: str
) -> None:
    project = _workspace(tmp_path, payload)

    with pytest.raises(ConfigurationError, match=message):
        load_project_settings(tmp_path, project)


def test_project_sections_are_validated_against_installed_extensions(
    tmp_path: Path,
) -> None:
    project = _workspace(tmp_path, {"extensions": {"acme/nope": {}}})
    loaded = load_project_settings(tmp_path, project)

    with pytest.raises(
        ConfigurationError,
        match=r"backend/ww.json \(project 'backend'\) configures "
        "unknown extension 'acme/nope'",
    ):
        loaded.sections.validate_against(("ww/git",))


def test_project_validation_names_the_file_that_holds_the_section(
    tmp_path: Path,
) -> None:
    project = _workspace(
        tmp_path,
        {"extensions": {"ww/git": {"worktrees": False}}},
        {"extensions": {"acme/nope": {}}},
    )
    loaded = load_project_settings(tmp_path, project)

    with pytest.raises(
        ConfigurationError,
        match=r"backend/ww.local.json \(project 'backend'\) "
        "configures unknown extension 'acme/nope'",
    ):
        loaded.validate_against(("ww/git",))


def test_overlay_settings_merges_nested_objects_and_replaces_other_values() -> None:
    base = {
        "commit_format": "{{ww.task.id}}: {{commit_message}}",
        "base_branches": {"default": "dev", "hotfix": "main"},
        "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
        "worktrees": True,
    }

    merged = overlay_settings(
        base,
        {"base_branches": {"default": "master"}, "worktrees": False, "extra": [1]},
    )

    assert merged == {
        "commit_format": "{{ww.task.id}}: {{commit_message}}",
        "base_branches": {"default": "master", "hotfix": "main"},
        "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
        "worktrees": False,
        "extra": [1],
    }
    assert base["base_branches"] == {"default": "dev", "hotfix": "main"}


def test_project_settings_require_a_configured_project(tmp_path: Path) -> None:
    config = load_project_config(
        write(tmp_path, {"projects": [{"name": "backend", "path": "./backend"}]})
    )
    (tmp_path / "backend").mkdir()

    assert config.project_settings(tmp_path, "backend").sources == ()
    with pytest.raises(
        ConfigurationError, match="unknown project 'web'; configured projects: backend"
    ):
        config.project_settings(tmp_path, "web")
    with pytest.raises(ConfigurationError, match="no projects are configured"):
        ProjectConfig().project_settings(tmp_path, "web")


# task_format


@pytest.mark.parametrize(
    "value", ["WORK-{{timestamp}}-{{digit}}", "TASK-{{uuid}}", "explicit", "PLAIN"]
)
def test_task_format_loads_from_the_settings_file(tmp_path: Path, value: str) -> None:
    assert load_project_config(write(tmp_path, {"task_format": value})).task_format == (
        value
    )


def test_task_format_is_absent_by_default(tmp_path: Path) -> None:
    assert load_project_config(write(tmp_path, {})).task_format is None
    assert ProjectConfig().task_format is None


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "task_format must be a non-empty string"),
        (7, "task_format must be a non-empty string"),
        (
            "WORK-{{random}}",
            r"task_format has unknown placeholder\(s\): \{\{random\}\}",
        ),
        ("WORK-{random}", "task_format has invalid placeholders"),
        ("WORK-{digit", "task_format has invalid placeholders"),
        ("WORK-}digit{", "task_format has invalid placeholders"),
    ],
)
def test_task_format_is_validated(tmp_path: Path, value: object, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_project_config(write(tmp_path, {"task_format": value}))


def test_a_lower_settings_level_replaces_task_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(user))
    (user / "ww.json").write_text(
        json.dumps({"task_format": "M-{{digit}}"}), encoding="utf-8"
    )
    path = write(tmp_path, {"enabled": True})
    assert load_project_config(path).task_format == "M-{{digit}}"

    (tmp_path / "ww.local.json").write_text(
        json.dumps({"task_format": "L-{{digit}}"}), encoding="utf-8"
    )
    assert load_project_config(path).task_format == "L-{{digit}}"


def test_a_project_file_may_carry_its_own_task_format(tmp_path: Path) -> None:
    project = _workspace(
        tmp_path,
        {"task_format": "BE-{{digit}}", "enabled": False},
        {"task_format": "explicit"},
    )

    assert load_project_settings(tmp_path, project).task_format == "explicit"
    (tmp_path / "backend" / "ww.local.json").unlink()
    assert load_project_settings(tmp_path, project).task_format == "BE-{{digit}}"
    workspace_without_files = _workspace_without_files(tmp_path)
    assert load_project_settings(tmp_path, workspace_without_files).task_format is None


def _workspace_without_files(tmp_path: Path) -> ProjectDefinition:
    (tmp_path / "frontend").mkdir(exist_ok=True)
    return ProjectDefinition("frontend", "./frontend")


def test_an_invalid_project_task_format_names_the_project(tmp_path: Path) -> None:
    project = _workspace(tmp_path, {"task_format": "BE-{{nope}}"})

    with pytest.raises(
        ConfigurationError,
        match=r"backend/ww.json \(project 'backend'\)\.task_format "
        r"has unknown placeholder\(s\): \{\{nope\}\}",
    ):
        load_project_settings(tmp_path, project)


def _settings(tmp_path: Path, settings: dict[str, object]) -> Path:
    path = tmp_path / "ww.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    return path


def test_json_limits_are_read(tmp_path: Path) -> None:
    settings = _settings(tmp_path, {"limits": {"rounds": 4}})
    assert load_project_config(settings).limits.rounds == 4


def test_task_format_placeholders_take_double_braces(tmp_path: Path) -> None:
    config = load_project_config(_settings(tmp_path, {"task_format": "T-{{digit}}"}))

    assert config.task_format == "T-{{digit}}"
    with pytest.raises(ConfigurationError, match="has invalid placeholders"):
        load_project_config(_settings(tmp_path, {"task_format": "T-{digit}"}))
