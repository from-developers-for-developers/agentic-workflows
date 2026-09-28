# SPDX-License-Identifier: GPL-3.0-or-later
"""ww-agentic-workflows.json: loading, validation, and per-extension isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.errors import ConfigurationError
from ww.project_config import ProjectConfig, load_project_config


def write(root: Path, payload: object) -> Path:
    path = root / "ww-agentic-workflows.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_an_absent_file_yields_defaults(tmp_path: Path) -> None:
    config = load_project_config(tmp_path / "ww-agentic-workflows.json")

    assert config.extensions == {}
    assert config.settings_for("ww/git") == {}
    assert config.loop_max_times == 3


def test_global_loop_limit_loads_from_project_config(tmp_path: Path) -> None:
    path = write(tmp_path, {"loop_max_times": 7})

    assert load_project_config(path).loop_max_times == 7


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
        ({"enabled": "yes"}, "enabled must be true or false"),
        ({"extensions": []}, "extensions must be an object"),
        ({"extensions": {"ww/git": 5}}, "must be an object of settings"),
        ({"loop_max_times": 0}, "loop_max_times must be a positive integer"),
        ({"loop_max_times": True}, "loop_max_times must be a positive integer"),
        ({"loop_max_times": "3"}, "loop_max_times must be a positive integer"),
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
    path = tmp_path / "ww-agentic-workflows.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(ConfigurationError) as error:
        load_project_config(path)

    assert "ww-agentic-workflows.json" in str(error.value)


@pytest.mark.parametrize("enabled", [True, False])
def test_enabled_is_read_and_defaults_to_true(tmp_path: Path, enabled: bool) -> None:
    assert load_project_config(write(tmp_path, {"enabled": enabled})).enabled is enabled
    assert load_project_config(write(tmp_path, {})).enabled is True
    assert load_project_config(tmp_path / "absent.json").enabled is True


def test_core_workflows_are_enabled_unless_switched_off(tmp_path: Path) -> None:
    assert load_project_config(tmp_path / "absent.json").workflow_enabled("catchall")
    config = load_project_config(
        write(tmp_path, {"workflows": {"catchall": {"enabled": False}}})
    )

    assert not config.workflow_enabled("catchall")


@pytest.mark.parametrize(
    ("workflows", "message"),
    [
        ([], "workflows must be an object"),
        ({"task": {"enabled": False}}, "unknown name.*core workflows: catchall"),
        ({"catchall": False}, "workflows.catchall must be an object"),
        ({"catchall": {"model": "x"}}, "unknown key"),
        ({"catchall": {"enabled": "no"}}, "enabled must be true or false"),
    ],
)
def test_the_core_workflow_switches_are_validated(
    tmp_path: Path, workflows: object, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_project_config(write(tmp_path, {"workflows": workflows}))
