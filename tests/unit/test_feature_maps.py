# SPDX-License-Identifier: GPL-3.0-or-later
"""Feature maps: reading a map, its shape, and whether each pointer resolves."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.features import (
    FEATURES_DIRECTORY,
    MAX_ENTRIES,
    TEMPLATE,
    MapsReport,
    Pointer,
    TaskEntry,
    load_feature_maps,
    pointer_problems,
    read_maps_report,
    read_task_list,
    record_task,
    task_list_path,
)


def _project(root: Path) -> Path:
    (root / "src" / "app").mkdir(parents=True)
    (root / "src" / "app" / "registry.py").write_text(
        "class Registry:\n    known = registry_names()\n", encoding="utf-8"
    )
    (root / "src" / "app" / "hooks.py").write_text("HOOKS = ()\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text(
        "# Guide\n\n## The registry ##\n\nText.\n", encoding="utf-8"
    )
    (root / FEATURES_DIRECTORY).mkdir(parents=True)
    return root


def _map(root: Path, name: str, text: str) -> None:
    (root / FEATURES_DIRECTORY / f"{name}.yaml").write_text(text, encoding="utf-8")


def _messages(root: Path) -> list[str]:
    return [problem.message for problem in load_feature_maps(root).check()]


def test_a_map_reads_every_field_and_every_pointer_resolves(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _map(
        root,
        "agents",
        """name: agents
summary: Agent adapters behind one registry.
aliases: [adapters, agent registry]
entry: src/app/registry.py#Registry
parts:
  - src/app/
  - src/app/{registry,hooks}.py   # both files
touches: [src/app/hooks.py#HOOKS]
reads: [task state files]
tests: [src/app/*.py]
docs:
  - docs/guide.md#The registry
""",
    )

    maps = load_feature_maps(root)

    assert maps.problems == ()
    assert maps.check() == ()
    agents = maps.get("agents")
    assert agents is not None
    assert agents.summary == "Agent adapters behind one registry."
    assert agents.aliases == ("adapters", "agent registry")
    assert agents.entry == Pointer("src/app/registry.py", "Registry")
    assert [str(pointer) for _, pointer in agents.pointers()] == [
        "src/app/registry.py#Registry",
        "src/app/",
        "src/app/{registry,hooks}.py",
        "src/app/hooks.py#HOOKS",
        "src/app/*.py",
        "docs/guide.md#The registry",
    ]
    assert agents.reads == ("task state files",)


def test_no_directory_means_no_maps(tmp_path: Path) -> None:
    maps = load_feature_maps(tmp_path)

    assert maps.maps == () and maps.check() == ()


@pytest.mark.parametrize(
    ("pointer", "problem"),
    [
        ("src/app/registry.py", None),
        ("src/app", None),
        ("src/app/missing.py", "no such file or directory"),
        ("src/**/*.py", None),
        ("src/app/*.rs", "matches no file"),
        ("src/app/{missing,hooks}.py", None),
        ("src/app/{missing,absent}.py", "matches no file"),
        ("src/app/registry.py#Registry", None),
        ("src/app/registry.py#registry_names", None),
        ("src/app/registry.py#Regist", "does not contain 'Regist'"),
        ("src/app/registry.py#names", "does not contain 'names'"),
        ("docs/guide.md#The registry", None),
        ("docs/guide.md#Guide", None),
        ("docs/guide.md#Text.", "has no heading 'Text.'"),
        ("docs/guide.md#Hooks", "has no heading 'Hooks'"),
        ("src/app#Registry", "an anchor needs a file, and this is a directory"),
        ("src/app/*.py#Registry", "an anchor needs a single file"),
        ("src/app/#Registry", "an anchor needs a single file"),
        ("src/app/registry.py#", "has an empty anchor after #"),
        ("/etc/hosts", "must be a path inside the project"),
        ("../outside.py", "must be a path inside the project"),
        ("#Registry", "names no path"),
    ],
)
def test_a_pointer_resolves_or_says_why_not(
    tmp_path: Path, pointer: str, problem: str | None
) -> None:
    found = pointer_problems(Pointer.parse(pointer), _project(tmp_path))

    if problem is None:
        assert found == ()
    else:
        assert len(found) == 1 and found[0].startswith(problem)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("summary: S.\nparts: [src/app/]\n", "name must be lowercase"),
        ("name: Other\nsummary: S.\nparts: [src/app/]\n", "name must be lowercase"),
        (
            "name: other\nsummary: S.\nparts: [src/app/]\n",
            "name 'other' must match the file name 'feature'",
        ),
        ("name: feature\nparts: [src/app/]\n", "summary must be one line of text"),
        (
            "name: feature\nsummary: |\n  One.\n  Two.\nparts: [src/app/]\n",
            "summary must be one line",
        ),
        ("name: feature\nsummary: S.\n", "parts must list at least one pointer"),
        (
            "name: feature\nsummary: S.\nparts: src/app/\n",
            "parts must be a list of non-empty strings",
        ),
        (
            "name: feature\nsummary: S.\nparts: [src/app/, src/app/]\n",
            "parts repeats src/app/",
        ),
        (
            "name: feature\nsummary: S.\nparts: [src/app/]\nentry: [a, b]\n",
            "entry must be one pointer",
        ),
        (
            "name: feature\nsummary: S.\nparts: [src/app/]\nowner: me\n",
            "unknown field(s): owner",
        ),
        (
            "name: feature\nsummary: S.\nparts: [src/app/*.py#Registry]\n",
            "parts[0] src/app/*.py#Registry: an anchor needs a single file",
        ),
        (
            "name: feature\nsummary: S.\nparts: [src/app/]\nuses: [feature]\n",
            "uses itself",
        ),
        (
            "name: feature\nsummary: S.\nparts: [src/app/]\nuses: [ghost]\n",
            "uses 'ghost', which is no feature map",
        ),
        ("- a list\n", "must hold a mapping of the map's fields"),
        ("name: [unclosed\n", "cannot be read"),
    ],
)
def test_a_malformed_map_is_reported(tmp_path: Path, text: str, problem: str) -> None:
    root = _project(tmp_path)
    _map(root, "feature", text)

    assert any(message.startswith(problem) for message in _messages(root)), _messages(
        root
    )


def test_a_map_over_the_entry_limit_is_reported(tmp_path: Path) -> None:
    root = _project(tmp_path)
    parts = "".join(
        "  - src/app/registry.py\n" if i == 0 else f"  - src/app/x{i}/\n"
        for i in range(MAX_ENTRIES + 1)
    )
    _map(root, "feature", f"name: feature\nsummary: S.\nparts:\n{parts}")

    shape = [problem.message for problem in load_feature_maps(root).problems]

    assert (
        f"holds {MAX_ENTRIES + 1} entries, more than {MAX_ENTRIES}: split the feature"
        in shape
    )


def test_an_alias_naming_another_map_is_reported(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _map(root, "agents", "name: agents\nsummary: S.\nparts: [src/app/]\n")
    _map(
        root,
        "hooks",
        "name: hooks\nsummary: S.\nparts: [src/app/hooks.py]\naliases: [Agents]\n",
    )
    _map(
        root,
        "registry",
        "name: registry\nsummary: S.\nparts: [src/app/]\naliases: [lookup]\n",
    )
    _map(
        root,
        "search",
        "name: search\nsummary: S.\nparts: [src/app/]\naliases: [Lookup]\n",
    )

    messages = _messages(root)

    assert "alias 'Agents' also names feature map 'agents'" in messages
    assert "alias 'Lookup' also names feature map 'registry'" in messages


def test_a_map_that_does_not_read_leaves_the_others(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _map(root, "agents", "name: agents\nsummary: S.\nparts: [src/app/]\n")
    _map(root, "broken", "name: [unclosed\n")

    maps = load_feature_maps(root)

    assert [feature.name for feature in maps.maps] == ["agents"]
    assert [problem.file for problem in maps.problems] == [".ww/features/broken.yaml"]


def test_the_template_is_a_valid_map_shape(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _map(root, "example", TEMPLATE)
    _map(root, "other-map", "name: other-map\nsummary: S.\nparts: [src/app/]\n")

    shape = [problem.message for problem in load_feature_maps(root).problems]

    assert shape == []


def test_a_map_naming_another_is_keyed_by_its_file_and_shadows_nothing(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _map(root, "agents", "name: agents\nsummary: The real one.\nparts: [src/app/]\n")
    _map(root, "hooks", "name: agents\nsummary: Misnamed.\nparts: [src/app/]\n")

    maps = load_feature_maps(root)

    assert [feature.name for feature in maps.maps] == ["agents", "hooks"]
    agents = maps.get("agents")
    assert agents is not None and agents.summary == "The real one."
    assert "name 'agents' must match the file name 'hooks'" in [
        problem.message for problem in maps.problems
    ]


@pytest.mark.parametrize(
    ("pointer", "path", "covered"),
    [
        ("src/app/registry.py", "src/app/registry.py", True),
        ("src/app/registry.py#Registry", "src/app/registry.py", True),
        ("src/app/registry.py", "src/app/registry.pyc", False),
        ("src/app", "src/app/deep/gone.py", True),
        ("src/app/", "src/app/hooks.py", True),
        ("src/app", "src/application.py", False),
        ("src/app/*.py", "src/app/new.py", True),
        ("src/app/*.py", "src/app/deep/new.py", False),
        ("src/**/*.py", "src/app/deep/new.py", True),
        ("src/*", "src/app/deep/new.py", True),
        ("src/app/{registry,hooks}.py", "src/app/hooks.py", True),
        ("src/app/{registry,hooks}.py", "src/app/other.py", False),
    ],
)
def test_a_pointer_covers_a_path_from_the_path_alone(
    pointer: str, path: str, covered: bool
) -> None:
    assert Pointer.parse(pointer).covers(path) is covered


def _two_maps(root: Path) -> None:
    _map(
        root,
        "agents",
        "name: agents\nsummary: Agent adapters behind one registry.\n"
        "aliases: [Adapters]\nparts: [src/app/registry.py#Registry]\n"
        "docs: [docs/guide.md#The registry]\n",
    )
    _map(
        root,
        "hooks",
        "name: hooks\nsummary: Hook protocols agents answer.\n"
        "parts: [src/app/hooks.py]\n",
    )


def test_coverage_names_the_affected_maps_and_the_unmapped_files(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    _two_maps(root)

    coverage = load_feature_maps(root).coverage(
        [
            "docs/guide.md",
            "src/app/new.py",
            ".ww/features/agents.yaml",
            ".ww/features/agents.tasks",
        ]
    )

    assert [feature.name for feature in coverage.affected] == ["agents"]
    assert coverage.unmapped == ("src/app/new.py",)


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("agents", ["agents", "hooks"]),
        ("adapters", ["agents"]),
        ("HOOKS", ["hooks"]),
        ("hook protocol", ["hooks"]),
        ("agent", ["agents", "hooks"]),
        ("registry", ["agents"]),
        ("src/app/hooks.py", ["hooks"]),
        ("src/app/registry.py#Registry", ["agents"]),
        ("src/app", ["agents", "hooks"]),
        ("docs/guide.md", ["agents"]),
        ("src/elsewhere.py", []),
        ("nothing here", []),
    ],
)
def test_lookup_finds_maps_by_name_alias_words_or_path(
    tmp_path: Path, query: str, found: list[str]
) -> None:
    root = _project(tmp_path)
    _two_maps(root)

    assert [feature.name for feature in load_feature_maps(root).lookup(query)] == found


def test_a_task_list_records_each_task_once_on_one_line(tmp_path: Path) -> None:
    root = _project(tmp_path)

    assert record_task(root, "agents", "TASK-1", "Add the\nregistry") is True
    assert record_task(root, "agents", "TASK-2", "Add hooks") is True
    assert record_task(root, "agents", "TASK-1", "Again") is False

    assert task_list_path(root, "agents").read_text("utf-8") == (
        "TASK-1  Add the registry\nTASK-2  Add hooks\n"
    )
    # A union merge can repeat a line; the first one stands.
    with task_list_path(root, "agents").open("a", encoding="utf-8") as file:
        file.write("TASK-1  Add the registry\nTASK-3\n")
    assert read_task_list(root, "agents") == (
        TaskEntry("TASK-1", "Add the registry"),
        TaskEntry("TASK-2", "Add hooks"),
        TaskEntry("TASK-3", ""),
    )
    assert read_task_list(root, "hooks") == ()


@pytest.mark.parametrize(
    ("artifact", "report"),
    [
        ("# Research\n\nFound it.\n", None),
        (
            "# Research\n\n## Feature maps\nUsed: agents, `usage`\n"
            "Gaps: agents map misses src/cli/audit.py\n\n## Next\nmore\n",
            MapsReport(("agents", "usage"), ("agents map misses src/cli/audit.py",)),
        ),
        (
            "## Feature maps\n\nUsed: none\nGaps:\n- usage lacks .ww/usage/\n"
            "- hooks summary is outdated\n",
            MapsReport((), ("usage lacks .ww/usage/", "hooks summary is outdated")),
        ),
        ("### Feature Maps ###\nused: agents\ngaps: none\n", MapsReport(("agents",))),
        ("## Feature maps\n", MapsReport()),
    ],
)
def test_a_maps_report_is_read_from_the_artifact_section(
    artifact: str, report: MapsReport | None
) -> None:
    assert read_maps_report(artifact) == report
