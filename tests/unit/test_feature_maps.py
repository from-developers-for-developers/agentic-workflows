# SPDX-License-Identifier: GPL-3.0-or-later
"""Feature maps: reading a map, its shape, and whether each pointer resolves."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.features import (
    FEATURES_DIRECTORY,
    MAX_ENTRIES,
    TEMPLATE,
    Pointer,
    load_feature_maps,
    pointer_problems,
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
