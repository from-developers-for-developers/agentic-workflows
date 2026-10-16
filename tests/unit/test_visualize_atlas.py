# SPDX-License-Identifier: GPL-3.0-or-later
"""The Workflows tab's data: each workflow as ww compiles it, every construct."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from ww.storage import Storage
from ww.visualize.atlas import build_atlas
from ww.visualize.project import Project

SHOWCASE = Path(__file__).parents[1] / "fixtures/visualize/showcase"


@pytest.fixture(name="atlas")
def _atlas(tmp_path: Path, shipped_builtins: None) -> dict[str, Any]:
    root = tmp_path / "project"
    shutil.copytree(SHOWCASE, root)
    return build_atlas(Project.load(Storage(root)))


def _workflow(atlas: dict[str, Any], name: str) -> dict[str, Any]:
    return next(entry for entry in atlas["workflows"] if entry["name"] == name)


def _step(steps: list[dict[str, Any]], path: str) -> dict[str, Any]:
    return next(step for step in steps if step["path"] == path)


def test_every_workflow_is_listed_and_the_page_data_is_json(
    atlas: dict[str, Any],
) -> None:
    names = [entry["name"] for entry in atlas["workflows"] if not entry["builtin"]]
    assert names == [
        "build",
        "bugfix",
        "review-loop",
        "polish",
        "address-feedback",
        "triage",
        "release",
        "epic",
    ]
    assert atlas["error"] is None
    assert atlas["files"] == ["ww.yaml"]
    assert all(entry["error"] is None for entry in atlas["workflows"])
    assert any(entry["builtin"] for entry in atlas["workflows"])
    json.dumps(atlas)


def test_steps_carry_their_kind_settings_rules_and_hooks(
    atlas: dict[str, Any],
) -> None:
    build = _workflow(atlas, "build")
    develop = _step(build["steps"], "develop")
    assert develop["kind"] == "manager"
    assert [rule["id"] for rule in develop["rules"]] == [
        "python-architecture/no-print",
        "python-architecture/parse-plan-execute",
        "develop/1",
    ]
    no_print = develop["rules"][0]
    assert no_print["group"] == "python-architecture"
    assert no_print["command"].startswith("grep -n 'print('")
    assert no_print["max_fixes"] == 5
    assert develop["rules"][2]["group"] is None
    assert develop["hooks"] == [
        {
            "name": "develop/true",
            "phase": "before complete",
            "owner": "ww",
            "command": "true",
            "on_failure": "fix",
            "builtin": False,
        }
    ]
    quality = _step(build["steps"], "check-code-quality")
    assert (quality["kind"], quality["model"], quality["reasoning"]) == (
        "worker",
        "cheapest",
        "lowest",
    )
    assert [hook["name"] for hook in build["lanes"]["before_start"]] == [
        "check-environment"
    ]
    assert [
        (hook["name"], hook["builtin"]) for hook in build["lanes"]["before_complete"]
    ] == [("commit", False), ("update-workflow-summary", True)]


def test_inherited_steps_and_a_lane_s_hooks_are_resolved(
    atlas: dict[str, Any],
) -> None:
    bugfix = _workflow(atlas, "bugfix")
    assert bugfix["inherits"] == "build"
    assert bugfix["recommended_next"] == "build"
    assert [step["path"] for step in bugfix["steps"]] == [
        "develop",
        "check-code-quality",
    ]
    polish = _workflow(atlas, "polish")
    assert polish["hooks_from"] == "build"
    assert [hook["name"] for hook in polish["lanes"]["before_start"]] == [
        "check-environment"
    ]


def test_a_goto_back_and_a_rule_selection(atlas: dict[str, Any]) -> None:
    loop = _workflow(atlas, "review-loop")
    assert (loop["runtime"], loop["modes"]) == ("auto", ["economy"])
    plan = _step(loop["steps"], "plan")
    assert plan["selects_rules_for"] == ["develop"]
    develop = _step(loop["steps"], "develop")
    assert develop["rules_chosen_by"] == "plan"
    assert develop["explicit"] is True
    assert develop["artifact_from"] == "plan"
    review = _step(loop["steps"], "review")
    assert (review["profile"], review["subagents"]) == ("code-reviewer", False)
    assessment = review["assessment"]
    assert assessment["attached"] is True
    positive = next(o for o in assessment["outcomes"] if o["label"] == "positive")
    assert positive["goto"] == {
        "target": "develop",
        "detail": "Fix what {{ww.goto.from}} found.",
        "direction": "back",
        "skipped": 0,
    }
    assert _step(loop["steps"], "release-notes")["saves"] == ["metadata.release_notes"]


def test_a_repeating_assessment_keeps_its_outcome_steps(
    atlas: dict[str, Any],
) -> None:
    review = _workflow(atlas, "polish")["steps"][0]
    assessment = review["assessment"]
    assert assessment["repeat_until"] == "negative"
    outcomes = {
        o["label"]: [step["path"] for step in o["steps"]]
        for o in assessment["outcomes"]
    }
    assert outcomes == {
        "positive": ["code-review/positive/fix"],
        "mixed": ["code-review/mixed/investigate"],
    }


def test_items_interactive_steps_and_their_saves(atlas: dict[str, Any]) -> None:
    feedback = _workflow(atlas, "address-feedback")["steps"][0]
    assert feedback["kind"] == "items"
    assert feedback["items"]["description"] == (
        "Keep related comments as separate obligations."
    )
    steps = feedback["items"]["steps"]
    assert [step["name"] for step in steps] == ["bootstrap", "confirm", "send-replies"]
    assert steps[0]["saves"] == ["item.field.comment_id"]
    assert steps[1]["interactive"] == "page"
    assert [choice["label"] for choice in steps[1]["choices"]] == ["accept", "rework"]


def test_custom_outcomes_stop_jump_ahead_and_hand_off(atlas: dict[str, Any]) -> None:
    triage = _workflow(atlas, "triage")
    discuss, classify, spec, route = triage["steps"]
    assert discuss["interactive"] == "conversation"
    assert discuss["variables"] == ["next_workflow"]
    outcomes = {o["label"]: o for o in classify["assessment"]["outcomes"]}
    assert outcomes["duplicate"]["stop"] is True
    assert outcomes["trivial"]["goto"] == {
        "target": "route",
        "detail": None,
        "direction": "forward",
        "skipped": 1,
    }
    assert spec["saves"] == ["documents.spec"]
    assert (route["kind"], route["handoff_to"]) == ("handoff", "{{next_workflow}}")
    assert triage["restartable"] is True


def test_automatic_steps_show_their_commands_and_group_members(
    atlas: dict[str, Any],
) -> None:
    release = _workflow(atlas, "release")
    assert release["manual"] is True
    build_all, bump, pull_request, announce = release["steps"]
    assert build_all["kind"] == "auto"
    assert [member["command"] for member in build_all["members"]] == [
        "npm run build",
        "test -d dist",
    ]
    assert (build_all["on_failure"], build_all["max_fixes"]) == ("fix", 3)
    assert bump["saves"] == ["metadata.version"]
    assert pull_request["members"][0]["command"] == "gh pr create --fill"
    assert announce["hooks"][0]["phase"] == "after complete"


def test_children_stages_and_a_standalone_gate(atlas: dict[str, Any]) -> None:
    epic = _workflow(atlas, "epic")
    split, gate, _ = epic["steps"]
    assert gate["name"] == "assess"
    assert split["kind"] == "children"
    stages = split["children"]["steps"]
    assert [(stage["name"], stage["kind"]) for stage in stages] == [
        ("refine", "manager"),
        ("implement", "workflow"),
        ("land", "worker"),
    ]
    assert stages[1]["workflow"] == "build"
    assert gate["kind"] == "assess"
    assert gate["assessment"]["attached"] is False


def test_the_library_lists_handlers_rule_groups_and_settings(
    atlas: dict[str, Any],
) -> None:
    library = atlas["library"]
    handlers = {entry["name"]: entry for entry in library["handlers"]}
    assert handlers["build-all"]["kind"] == "group"
    assert handlers["build-all"]["members"] == ["build-frontend", "inline-argv"]
    assert handlers["build-frontend"]["command"] == "npm run build"
    assert handlers["check-code-quality"]["used_by"] == ["build", "bugfix"]
    groups = {group["name"]: group for group in library["rules"]["groups"]}
    assert set(groups) == {"python-architecture", "review-standards"}
    assert groups["python-architecture"]["rules"][0]["source"] == (
        "rules/python/no-print.md"
    )
    assert library["settings"]["limits"]["gotos"] == 5
    assert "spec" in {doc["name"] for doc in library["documents"]}


def test_a_configuration_that_does_not_load_says_why(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text("workflows: 3\n", encoding="utf-8")

    atlas = build_atlas(Project.load(Storage(tmp_path)))

    assert atlas["workflows"] == []
    assert atlas["error"]
