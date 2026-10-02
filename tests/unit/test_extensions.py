# SPDX-License-Identifier: GPL-3.0-or-later
"""The extension contract: references, discovery, and per-extension state."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ww.errors import ConfigurationError
from ww.extensions import (
    Extension,
    ExtensionCommand,
    ExtensionContext,
    ExtensionHandler,
    ExtensionNamespace,
    ExtensionRegistry,
    ExtensionResult,
    ExtensionStore,
    ExtensionVariable,
    is_extension_reference,
    parse_reference,
)
from ww.extensions import registry as registry_module
from ww.project_config import ProjectConfig, ProjectDefinition
from ww.workflow_config import ModeDefinition

EXTENSION_SOURCE = """
from ww.extensions.api import (
    ExtensionContext,
    Extension,
    ExtensionCommand,
    ExtensionHandler,
    ExtensionResult,
    ModeDefinition,
)


def _run(context):
    return ExtensionResult(True, output="ran")


def _show(context):
    return "shown " + " ".join(context.arguments)


EXTENSION = Extension(
    vendor="{vendor}",
    name="{name}",
    version="1.2.3",
    description="Example.",
    handlers=(ExtensionHandler("do-it", _run, "Do it."),),
    modes=(ModeDefinition("careful", ("Go slowly.",)),),
    commands=(ExtensionCommand("show", _show, "Show something."),),
)
"""


def write_extension(root: Path, vendor: str = "acme", name: str = "demo") -> Path:
    directory = root / "ext" / vendor / name
    directory.mkdir(parents=True)
    path = directory / "extension.py"
    path.write_text(EXTENSION_SOURCE.format(vendor=vendor, name=name), encoding="utf-8")
    return path


def test_a_reference_parses_into_its_parts() -> None:
    reference = parse_reference("ext/ww/git/handlers:git-commit")

    assert (reference.vendor, reference.name) == ("ww", "git")
    assert (reference.section, reference.item) == ("handlers", "git-commit")
    assert reference.identifier == "ww/git"
    assert str(reference) == "ext/ww/git/handlers:git-commit"


def test_a_referenced_extension_can_override_a_core_variable(tmp_path: Path) -> None:
    extension = Extension(
        vendor="acme",
        name="demo",
        variables=(
            ExtensionVariable("ww.task.workspace_dir", lambda context: "/chosen/path"),
        ),
    )
    registry = ExtensionRegistry(tmp_path, (extension,))

    values = registry.apply_variable_overrides(
        {"ww.task.workspace_dir": str(tmp_path.resolve())},
        (("acme/demo", None),),
        task_id="TASK-1",
        run_id="01-task",
        workflow="task",
        workflow_values={},
        workspace=None,
    )

    assert values["ww.task.workspace_dir"] == "/chosen/path"


def test_an_extension_cannot_introduce_a_core_variable(tmp_path: Path) -> None:
    extension = Extension(
        vendor="acme",
        name="demo",
        variables=(ExtensionVariable("new_global", lambda context: "value"),),
    )
    registry = ExtensionRegistry(tmp_path, (extension,))

    with pytest.raises(
        ConfigurationError, match="cannot override core variable 'new_global'"
    ):
        registry.apply_variable_overrides(
            {"ww.task.workspace_dir": str(tmp_path.resolve())},
            (("acme/demo", None),),
            task_id="TASK-1",
            run_id="01-task",
            workflow="task",
            workflow_values={},
            workspace=None,
        )


def _listing(root: Path, *identifiers: str) -> None:
    (root / "ww.json").write_text(
        json.dumps({"extensions": {identifier: {} for identifier in identifiers}}),
        encoding="utf-8",
    )


def _namespaced(name: str, namespace: str = "vcs") -> Extension:
    return Extension(
        vendor="acme",
        name=name,
        namespace=ExtensionNamespace(
            namespace,
            (
                ExtensionVariable("branch", lambda context: f"b-{context.task_id}"),
                ExtensionVariable("tag", lambda context: None),
            ),
        ),
    )


def test_a_listed_extension_provides_its_namespace(tmp_path: Path) -> None:
    _listing(tmp_path, "acme/demo")
    registry = ExtensionRegistry(tmp_path, (_namespaced("demo"),))

    names = registry.namespace_variables()
    values = registry.namespace_values(
        names,
        task_id="TASK-1",
        run_id="01-task",
        workflow="task",
        workflow_values={},
        workspace=None,
        project=None,
    )

    assert names == ("ww.vcs.branch", "ww.vcs.tag")
    # ``None`` means not available yet: the name is left out.
    assert values == {"ww.vcs.branch": "b-TASK-1"}


def test_an_unlisted_extension_provides_no_namespace(tmp_path: Path) -> None:
    registry = ExtensionRegistry(tmp_path, (_namespaced("demo"),))

    assert registry.namespace_variables() == ()


def test_two_extensions_may_not_share_a_namespace(tmp_path: Path) -> None:
    _listing(tmp_path, "acme/one", "acme/two")
    registry = ExtensionRegistry(tmp_path, (_namespaced("one"), _namespaced("two")))

    with pytest.raises(ConfigurationError, match="template namespace 'vcs'"):
        registry.namespace_variables()


@pytest.mark.parametrize("name", ["ww", "task", "child", "Git"])
def test_a_reserved_or_malformed_namespace_is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="namespace"):
        ExtensionNamespace(name, (ExtensionVariable("x", lambda context: None),))


@pytest.mark.parametrize(
    "value",
    [
        "ww/git/handlers:git-commit",
        "ext/ww/git:git-commit",
        "ext/ww/git/handlers",
        "ext/WW/git/handlers:git-commit",
        "ext//git/handlers:git-commit",
    ],
)
def test_a_malformed_reference_is_rejected(value: str) -> None:
    with pytest.raises(ConfigurationError) as error:
        parse_reference(value)

    assert "ext/<vendor>/<name>/<section>:<item>" in str(error.value)


def test_an_unknown_section_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as error:
        parse_reference("ext/ww/git/hooks:on-commit")

    assert "unknown extension section 'hooks'" in str(error.value)


def test_anything_ext_prefixed_is_treated_as_a_reference() -> None:
    # A typo must surface as a bad reference, never fall through to a project
    # handler name.
    assert is_extension_reference("ext/ww/git/handlers:x")
    assert is_extension_reference("ext/broken")
    assert not is_extension_reference("git-commit")


def test_a_project_local_extension_is_discovered(tmp_path: Path) -> None:
    write_extension(tmp_path)

    registry = ExtensionRegistry.discover(tmp_path)

    assert [item.identifier for item in registry.extensions] == ["acme/demo", "ww/git"]
    assert registry.handler("ext/acme/demo/handlers:do-it").name == "do-it"
    assert registry.command("acme/demo", "show").name == "show"


def test_a_root_without_local_extensions_discovers_bundled_git(tmp_path: Path) -> None:
    registry = ExtensionRegistry.discover(tmp_path)

    assert [item.identifier for item in registry.extensions] == ["ww/git"]


def test_an_installed_entry_point_is_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = Extension(
        vendor="pip",
        name="thing",
        handlers=(ExtensionHandler("go", lambda context: ExtensionResult(True)),),
    )

    class _Point:
        name = "pip.thing"
        value = "pip_thing:EXTENSION"

        @staticmethod
        def load() -> Extension:
            return extension

    monkeypatch.setattr(registry_module, "entry_points", lambda group: [_Point()])

    registry = ExtensionRegistry.discover(tmp_path)

    assert [item.identifier for item in registry.extensions] == ["pip/thing", "ww/git"]


def test_a_duplicate_identifier_is_an_error(tmp_path: Path) -> None:
    extension = Extension(vendor="acme", name="demo")

    with pytest.raises(ConfigurationError) as error:
        ExtensionRegistry(tmp_path, (extension, extension))

    assert "provided more than once" in str(error.value)


def test_a_project_cannot_shadow_the_bundled_git_extension(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "ext" / "ww" / "git" / "extension.py"
    target = tmp_path / "ext" / "ww" / "git"
    target.mkdir(parents=True)
    (target / "extension.py").write_text(source.read_text(encoding="utf-8"))

    with pytest.raises(ConfigurationError, match="ww/git.*provided more than once"):
        ExtensionRegistry.discover(tmp_path)


def _ww_checkout(root: Path) -> None:
    """Lay ``root`` out as another ww source checkout, such as a dev clone."""
    marker = root / "src" / "ww" / "extensions" / "registry.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("# another ww checkout\n", encoding="utf-8")
    source = Path(__file__).parents[2] / "ext" / "ww" / "git" / "extension.py"
    target = root / "ext" / "ww" / "git"
    target.mkdir(parents=True)
    # A different file from the running install's bundled source.
    (target / "extension.py").write_text(
        source.read_text(encoding="utf-8") + "\n# another version\n"
    )


def test_another_ww_checkout_uses_the_running_installs_bundled_git(
    tmp_path: Path,
) -> None:
    _ww_checkout(tmp_path)

    registry = ExtensionRegistry.discover(tmp_path)

    assert registry.identifiers.count("ww/git") == 1
    assert registry.identity("ww/git").source == "bundled:ww/git"


def test_another_ww_checkout_still_discovers_other_vendors(tmp_path: Path) -> None:
    _ww_checkout(tmp_path)
    write_extension(tmp_path)

    registry = ExtensionRegistry.discover(tmp_path)

    assert registry.identity("acme/demo").source == "project:ext/acme/demo/extension.py"


def test_a_directory_that_disagrees_with_its_extension_is_an_error(
    tmp_path: Path,
) -> None:
    path = write_extension(tmp_path, vendor="acme", name="demo")
    path.write_text(
        EXTENSION_SOURCE.format(vendor="other", name="demo"), encoding="utf-8"
    )

    registry = ExtensionRegistry.discover(tmp_path)

    with pytest.raises(ConfigurationError) as error:
        registry.get("acme/demo")

    assert "declares 'other/demo'" in str(error.value)


def test_a_module_without_an_extension_is_an_error(tmp_path: Path) -> None:
    path = write_extension(tmp_path)
    path.write_text("VALUE = 1\n", encoding="utf-8")

    registry = ExtensionRegistry.discover(tmp_path)

    with pytest.raises(ConfigurationError) as error:
        registry.get("acme/demo")

    assert "must define EXTENSION" in str(error.value)


def test_an_extension_that_fails_to_import_names_the_file(tmp_path: Path) -> None:
    path = write_extension(tmp_path)
    path.write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    registry = ExtensionRegistry.discover(tmp_path)

    with pytest.raises(ConfigurationError) as error:
        registry.get("acme/demo")

    assert "failed to import" in str(error.value)
    assert "boom" in str(error.value)


def test_discovery_does_not_import_an_unused_broken_extension(tmp_path: Path) -> None:
    path = write_extension(tmp_path)
    path.write_text("raise RuntimeError('must stay lazy')\n", encoding="utf-8")

    registry = ExtensionRegistry.discover(tmp_path)

    assert registry.identifiers == ("acme/demo", "ww/git")
    assert registry.get("ww/git").identifier == "ww/git"


def test_duplicate_contribution_names_are_rejected_when_loaded(tmp_path: Path) -> None:
    path = write_extension(tmp_path)
    path.write_text(
        """from ww.extensions.api import Extension, ExtensionHandler, ExtensionResult
def run(context):
    return ExtensionResult(True)
EXTENSION = Extension(
    vendor="acme", name="demo",
    handlers=(ExtensionHandler("same", run), ExtensionHandler("same", run)),
)
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="names must be unique"):
        ExtensionRegistry.discover(tmp_path).get("acme/demo")


def test_an_incompatible_api_version_is_rejected_when_loaded(tmp_path: Path) -> None:
    path = write_extension(tmp_path)
    source = path.read_text(encoding="utf-8").replace(
        'version="1.2.3",', 'version="1.2.3",\n    api_version=999,'
    )
    path.write_text(source, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="requires API version 999"):
        ExtensionRegistry.discover(tmp_path).get("acme/demo")


def test_a_project_extension_does_not_hide_bundled_extensions(tmp_path: Path) -> None:
    write_extension(tmp_path)
    registry = ExtensionRegistry.discover(tmp_path)

    assert registry.get("ww/git").identifier == "ww/git"


def test_an_unknown_extension_lists_every_available_extension(tmp_path: Path) -> None:
    write_extension(tmp_path)
    registry = ExtensionRegistry.discover(tmp_path)

    with pytest.raises(ConfigurationError) as error:
        registry.get("missing/extension")

    assert "available: acme/demo, ww/git" in str(error.value)


def test_an_unknown_item_lists_what_the_extension_has(tmp_path: Path) -> None:
    write_extension(tmp_path)
    registry = ExtensionRegistry.discover(tmp_path)

    with pytest.raises(ConfigurationError) as error:
        registry.handler("ext/acme/demo/handlers:absent")

    assert "has no handler 'absent'" in str(error.value)
    assert "available: do-it" in str(error.value)


def test_a_section_mismatch_is_rejected(tmp_path: Path) -> None:
    write_extension(tmp_path)
    registry = ExtensionRegistry.discover(tmp_path)

    with pytest.raises(ConfigurationError) as error:
        registry.handler("ext/acme/demo/modes:careful")

    assert "addresses 'modes'" in str(error.value)


def test_extension_modes_are_catalogued_under_their_full_reference(
    tmp_path: Path,
) -> None:
    write_extension(tmp_path)
    write_extension(tmp_path, vendor="other", name="demo")

    modes = ExtensionRegistry.discover(tmp_path).qualified_modes()

    # Two vendors define "careful"; neither shadows the other.
    assert [mode.name for mode in modes] == [
        "ext/acme/demo/modes:careful",
        "ext/other/demo/modes:careful",
        "ext/ww/git/modes:conventional-commits",
    ]
    assert modes[0].description == ("Go slowly.",)


def test_an_extension_name_must_be_a_normalized_segment() -> None:
    with pytest.raises(ValueError) as error:
        Extension(vendor="Acme", name="demo")

    assert "extension vendor 'Acme'" in str(error.value)


def test_a_store_is_namespaced_per_extension(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path, "acme/demo")

    assert store.path("state.jsonl") == (
        tmp_path / ".ww" / "ext" / "acme" / "demo" / "state.jsonl"
    )


@pytest.mark.parametrize("name", ["", ".hidden", "a/b", "..", "a\\b"])
def test_a_store_rejects_anything_but_a_plain_file_name(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(ConfigurationError):
        ExtensionStore(tmp_path, "acme/demo").path(name)


def test_a_store_appends_and_reads_back(tmp_path: Path) -> None:
    store = ExtensionStore(tmp_path, "acme/demo")

    assert store.read_text("log.jsonl") is None
    store.append_line("log.jsonl", "one")
    store.append_line("log.jsonl", "two")
    store.write_text("note.txt", "hello")

    assert store.read_lines("log.jsonl") == ("one", "two")
    assert store.read_text("note.txt") == "hello"


def test_a_store_update_locks_the_complete_read_modify_write(
    tmp_path: Path,
) -> None:
    store = ExtensionStore(tmp_path, "acme/demo")

    def increment(_: int) -> None:
        for _attempt in range(20):

            def update(current: str | None) -> str:
                value = int(current or "0")
                time.sleep(0.001)
                return str(value + 1)

            store.update_text("counter.txt", update)

    with ThreadPoolExecutor(max_workers=8) as executor:
        tuple(executor.map(increment, range(8)))

    assert store.read_text("counter.txt") == "160"


def test_a_store_update_does_not_write_when_the_callback_fails(
    tmp_path: Path,
) -> None:
    store = ExtensionStore(tmp_path, "acme/demo")
    store.write_text("state.txt", "before")

    def fail(_: str | None) -> str:
        raise RuntimeError("no update")

    with pytest.raises(RuntimeError, match="no update"):
        store.update_text("state.txt", fail)

    assert store.read_text("state.txt") == "before"


def test_extension_results_validate_structured_values() -> None:
    result = ExtensionResult(True, output="created", values={"ticket_url": "url"})

    assert dict(result.values) == {"ticket_url": "url"}
    with pytest.raises(TypeError, match="values must be strings"):
        ExtensionResult(True, values={"count": 3})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="failed.*cannot provide"):
        ExtensionResult(False, values={"ticket_url": "url"})


def test_extension_handler_outputs_are_normalized_and_unique() -> None:
    run = lambda context: ExtensionResult(True)  # noqa: E731

    with pytest.raises(ValueError, match="unique"):
        ExtensionHandler("bad", run, outputs=("url", "url"))
    with pytest.raises(ValueError, match="normalized"):
        ExtensionHandler("bad", run, outputs=("not a name",))


def test_extension_handler_validate_is_optional_but_must_be_callable() -> None:
    run = lambda context: ExtensionResult(True)  # noqa: E731

    assert ExtensionHandler("plain", run).validate is None
    assert ExtensionHandler("checked", run, validate=lambda values: None).validate
    with pytest.raises(TypeError, match="validate must be callable"):
        ExtensionHandler("bad", run, validate="yes")  # type: ignore[arg-type]


def test_extension_contribution_shapes_and_names_are_validated() -> None:
    run = lambda context: "ok"  # noqa: E731

    with pytest.raises(TypeError, match="command run must be callable"):
        ExtensionCommand("bad", None)  # type: ignore[arg-type]
    command = ExtensionCommand("same", run)
    with pytest.raises(ValueError, match="commands names must be unique"):
        Extension(vendor="acme", name="demo", commands=(command, command))
    with pytest.raises(TypeError, match="provide must be a tuple"):
        ExtensionHandler(
            "bad",
            lambda context: ExtensionResult(True),
            provide=[],  # type: ignore[arg-type]
        )


def test_the_registry_hands_an_extension_its_own_store(tmp_path: Path) -> None:
    write_extension(tmp_path)

    store = ExtensionRegistry.discover(tmp_path).store("acme/demo")

    assert store.directory == tmp_path / ".ww" / "ext" / "acme" / "demo"


def test_commands_are_not_addressable_from_a_workflow(tmp_path: Path) -> None:
    write_extension(tmp_path)
    registry = ExtensionRegistry.discover(tmp_path)

    with pytest.raises(ConfigurationError) as error:
        registry.handler("ext/acme/demo/commands:show")

    assert "unknown extension section 'commands'" in str(error.value)


def test_the_bundled_git_extension_declares_its_contributions() -> None:
    root = Path(__file__).parents[2]

    registry = ExtensionRegistry.discover(root)
    git = registry.get("ww/git")

    assert sorted(git.handlers_by_name) == [
        "create-worktree",
        "git-commit",
        "is-git-clean",
        "merge-branch",
        "remove-task-worktree",
        "return-to-base-branch",
        "start-task-branch",
    ]
    assert sorted(git.modes_by_name) == ["conventional-commits"]
    assert sorted(git.commands_by_name) == ["branches", "commits", "settings"]
    assert [variable.name for variable in git.variables] == ["ww.task.workspace_dir"]
    assert git.handlers_by_name["git-commit"].provide[0].name == "commit_message"
    assert git.handlers_by_name["merge-branch"].arguments == ("branch", "message")
    assert isinstance(git.modes_by_name["conventional-commits"], ModeDefinition)
    assert isinstance(git.commands_by_name["commits"], ExtensionCommand)
    assert isinstance(git.handlers_by_name["git-commit"], ExtensionHandler)


def test_reserved_paths_come_only_from_configured_extensions(tmp_path: Path) -> None:
    def claim(context: ExtensionContext) -> tuple[Path, ...]:
        return (context.root / "claims" / f"{context.task_id}-{context.workflow}",)

    configured = Extension(vendor="acme", name="claims", reserved_paths=claim)
    idle = Extension(vendor="acme", name="idle", reserved_paths=claim)
    registry = ExtensionRegistry(
        tmp_path,
        (configured, idle),
        config=ProjectConfig(extensions={"acme/claims": {"enabled": True}}),
    )

    assert registry.reserved_paths("T-1", "task") == (tmp_path / "claims" / "T-1-task",)
    with pytest.raises(TypeError, match="reserved_paths must be callable"):
        Extension(vendor="acme", name="bad", reserved_paths="not callable")  # type: ignore[arg-type]


def test_task_claims_and_forgetting_reach_listed_extensions_and_stores(
    tmp_path: Path,
) -> None:
    forgotten: list[tuple[str, str | None]] = []

    def holds(context: ExtensionContext) -> bool:
        return context.task_id == "T-1"

    def forget(context: ExtensionContext) -> None:
        forgotten.append((context.store.identifier, context.task_id))

    listed = Extension(
        vendor="acme", name="listed", claims_task=holds, forget_task=forget
    )
    stored = Extension(vendor="acme", name="stored", forget_task=forget)
    idle = Extension(vendor="acme", name="idle", claims_task=holds, forget_task=forget)
    registry = ExtensionRegistry(
        tmp_path,
        (listed, stored, idle),
        # An empty section still lists the extension.
        config=ProjectConfig(extensions={"acme/listed": {}}),
    )
    # A handler referenced without a section still leaves a store behind.
    registry.store("acme/stored").append_line("records.jsonl", "{}")

    assert registry.claims_task("T-1", "task")
    assert not registry.claims_task("T-2", "task")
    registry.forget_task("T-1")
    assert sorted(forgotten) == [("acme/listed", "T-1"), ("acme/stored", "T-1")]
    for hook in ("claims_task", "forget_task"):
        with pytest.raises(TypeError, match=f"{hook} must be callable"):
            Extension(vendor="acme", name="bad", **{hook: "not callable"})  # type: ignore[arg-type]


# A configured project's own extension settings


def _project_registry(
    tmp_path: Path, *extensions: Extension, root_settings: dict[str, object]
) -> ExtensionRegistry:
    (tmp_path / "backend").mkdir(exist_ok=True)
    return ExtensionRegistry(
        tmp_path,
        extensions,
        ProjectConfig(
            extensions=dict(root_settings),
            projects=(ProjectDefinition("backend", "./backend"),),
        ),
    )


def _write_project_settings(tmp_path: Path, payload: object) -> None:
    (tmp_path / "backend" / "ww.json").write_text(json.dumps(payload), encoding="utf-8")


def test_a_projects_settings_apply_over_the_roots(tmp_path: Path) -> None:
    demo = Extension(vendor="acme", name="demo")
    registry = _project_registry(
        tmp_path,
        demo,
        root_settings={"acme/demo": {"a": 1, "nested": {"x": 1, "y": 2}}},
    )
    _write_project_settings(
        tmp_path,
        {
            "enabled": False,
            "extensions": {"demo": {"b": 2, "nested": {"y": 3}}},
        },
    )

    assert registry.settings("acme/demo") == {"a": 1, "nested": {"x": 1, "y": 2}}
    assert registry.settings("acme/demo", "backend") == {
        "a": 1,
        "b": 2,
        "nested": {"x": 1, "y": 3},
    }
    assert registry.project_settings("backend").sources == (
        tmp_path / "backend" / "ww.json",
    )
    with pytest.raises(ConfigurationError, match="unknown project 'web'"):
        registry.settings("acme/demo", "web")


def test_a_project_without_settings_keeps_the_roots(tmp_path: Path) -> None:
    demo = Extension(vendor="acme", name="demo")
    registry = _project_registry(tmp_path, demo, root_settings={"acme/demo": {"a": 1}})

    assert registry.settings("acme/demo", "backend") == {"a": 1}
    assert registry.project_settings("backend").sources == ()


def test_a_project_section_naming_an_unknown_extension_names_the_project(
    tmp_path: Path,
) -> None:
    demo = Extension(vendor="acme", name="demo")
    registry = _project_registry(tmp_path, demo, root_settings={})
    _write_project_settings(tmp_path, {"extensions": {"acme/nope": {"a": 1}}})

    with pytest.raises(
        ConfigurationError,
        match=r"backend/ww.json \(project 'backend'\) configures "
        "unknown extension 'acme/nope'; installed: acme/demo",
    ):
        registry.settings("acme/demo", "backend")
    # The root's own settings stay usable: the project is consulted only when named.
    assert registry.settings("acme/demo") == {}


def test_branch_strategies_and_reserved_paths_follow_the_project(
    tmp_path: Path,
) -> None:
    def claim(context: ExtensionContext) -> tuple[Path, ...]:
        assert context.workspace == (tmp_path / "backend").resolve()
        return ((context.workspace or context.root) / str(context.config["dir"]),)

    naming = Extension(
        "acme",
        "naming",
        branch_strategies=lambda settings: tuple(settings["strategies"]),
        reserved_paths=claim,
    )
    registry = _project_registry(tmp_path, naming, root_settings={})
    _write_project_settings(
        tmp_path,
        {
            "extensions": {
                "acme/naming": {"strategies": ["main", "hotfix"], "dir": "wt"}
            }
        },
    )

    # Configured only in the project: consulted there, idle at the root.
    assert registry.configured() == ()
    assert registry.configured("backend") == ("acme/naming",)
    assert registry.branch_strategies() == ()
    assert registry.branch_strategies("backend") == ("main", "hotfix")
    assert registry.reserved_paths("T-1", "task") == ()
    assert registry.reserved_paths("T-1", "task", "backend") == (
        (tmp_path / "backend").resolve() / "wt",
    )


def test_task_format_follows_the_project_when_it_sets_one(tmp_path: Path) -> None:
    registry = _project_registry(tmp_path, root_settings={})
    registry._config = ProjectConfig(
        projects=(ProjectDefinition("backend", "./backend"),),
        task_format="ROOT-{{digit}}",
    )

    assert registry.task_format() == "ROOT-{{digit}}"
    assert registry.task_format("backend") == "ROOT-{{digit}}"
    _write_project_settings(tmp_path, {"task_format": "BE-{{digit}}"})
    registry = ExtensionRegistry(tmp_path, (), registry.config)
    assert registry.task_format("backend") == "BE-{{digit}}"
    assert registry.task_format() == "ROOT-{{digit}}"
    with pytest.raises(ConfigurationError, match="unknown project 'web'"):
        registry.task_format("web")
