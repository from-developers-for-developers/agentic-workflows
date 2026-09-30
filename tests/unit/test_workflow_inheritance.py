# SPDX-License-Identifier: GPL-3.0-or-later
"""``inherit``: a workflow that is a complete copy of another."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.plan import compile_workflow_plan

HOTFIX = """hooks:
  before_start_workflow:
    - workflows: [hotfix]
      handlers:
        - prepare: Prepare the branch.
workflows:
  - hotfix: Fix a bug on main.
    restartable: true
    runtime: single
    hooks:
      after_complete:
        - handlers:
            - note: Note what changed.
    steps:
      - investigate: Investigate.
      - fix: Fix.
"""


def _load(tmp_path: Path, text: str):
    path = tmp_path / "ww-agentic-workflows.yaml"
    path.write_text(text, encoding="utf-8")
    return load_configuration(path)


def test_an_heir_copies_steps_hooks_and_settings(tmp_path: Path) -> None:
    configuration = _load(
        tmp_path,
        HOTFIX + "  - bugfix: Fix a bug on dev.\n    inherit: hotfix\n",
    )
    hotfix = configuration.workflows_by_name["hotfix"]
    bugfix = configuration.workflows_by_name["bugfix"]

    assert bugfix.inherits == "hotfix"
    assert bugfix.description == "Fix a bug on dev."
    assert (bugfix.steps, bugfix.hooks) == (hotfix.steps, hotfix.hooks)
    assert (bugfix.restartable, bugfix.runtime) == (True, "single")
    # The global hook written for hotfix runs for its heir as well.
    plan = compile_workflow_plan(configuration, tmp_path, "bugfix", "codex")
    # So does the copied workflow hook, after every step.
    assert [item.name for item in plan.items] == [
        "prepare",
        "init",
        "note",
        "investigate",
        "note",
        "fix",
        "note",
        "update-workflow-summary",
    ]


def test_an_heir_s_own_settings_replace_the_copied_ones(tmp_path: Path) -> None:
    configuration = _load(
        tmp_path,
        HOTFIX
        + "  - bugfix: ~\n    inherit: hotfix\n    runtime: auto\n"
        + "    restartable: false\n",
    )
    bugfix = configuration.workflows_by_name["bugfix"]

    assert bugfix.description == "Fix a bug on main."
    assert (bugfix.runtime, bugfix.restartable) == ("auto", False)


def test_inheritance_chains_and_reaches_every_heir(tmp_path: Path) -> None:
    configuration = _load(
        tmp_path,
        HOTFIX
        + "  - bugfix: ~\n    inherit: hotfix\n"
        + "  - hotfix-docs: ~\n    inherit: bugfix\n",
    )

    assert configuration.workflows_by_name["hotfix-docs"].inherits == "bugfix"
    (prepare,) = configuration.global_hooks
    assert set(prepare.workflows.listed) == {"hotfix", "bugfix", "hotfix-docs"}


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("  - bugfix: ~\n    inherit: nothing\n", "inherits unknown workflow"),
        (
            "  - bugfix: ~\n    inherit: hotfix\n    steps:\n      - x: X.\n",
            "cannot declare steps",
        ),
        (
            "  - a: ~\n    inherit: b\n  - b: ~\n    inherit: a\n",
            "inheritance cycle: a -> b -> a",
        ),
        ("  - bugfix: ~\n    inherit: [hotfix]\n", "inherit must name a workflow"),
    ],
)
def test_invalid_inheritance_is_rejected(
    tmp_path: Path, entry: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _load(tmp_path, HOTFIX + entry)


def test_a_recommendation_is_inherited_unless_cleared(tmp_path: Path) -> None:
    configuration = _load(
        tmp_path,
        HOTFIX.replace(
            "    restartable: true\n",
            "    restartable: true\n    recommended_next_workflow: merge\n",
        )
        + "  - bugfix: ~\n    inherit: hotfix\n"
        + "  - quiet: ~\n    inherit: hotfix\n    recommended_next_workflow: ~\n"
        + "  - merge: Merge.\n    steps:\n      - merge: Merge.\n",
    )

    assert configuration.workflows_by_name["bugfix"].recommended_next_workflow == (
        "merge"
    )
    assert configuration.workflows_by_name["quiet"].recommended_next_workflow is None
    plan = compile_workflow_plan(configuration, tmp_path, "bugfix", "codex")
    assert plan.recommended_next_workflow == "merge"
    assert plan.to_dict()["recommended_next_workflow"] == "merge"


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("recommended_next_workflow: nowhere", "recommends unknown workflow"),
        (
            "recommended_next_workflow: hotfix\n    handoff: true",
            "cannot also recommend",
        ),
    ],
)
def test_an_invalid_recommendation_is_rejected(
    tmp_path: Path, setting: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _load(
            tmp_path,
            HOTFIX.replace("    restartable: true\n", f"    {setting}\n"),
        )
