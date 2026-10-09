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
    path = tmp_path / "ww.yaml"
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


def test_explicit_visibility_inherits_through_workflows_and_structures(
    tmp_path: Path,
) -> None:
    configuration = _load(
        tmp_path,
        """workflows:
  - base: ~
    explicit: true
    steps:
      - group: ~
        steps:
          - inherited: Inherit visibility.
          - private: Keep default visibility.
            explicit: false
      - repeat: ~
        steps:
          - repeated: Inherit from group.
      - collect: Collect items.
        items:
          steps:
            - process: Process each item.
              explicit: false
  - child: ~
    inherit: base
    explicit: false
""",
    )

    base = configuration.workflows_by_name["base"]
    child = configuration.workflows_by_name["child"]
    assert base.explicit is True
    assert child.explicit is False
    group, repeat, collect = base.steps
    assert group.explicit is True
    assert [step.explicit for step in group.child_steps] == [True, False]
    assert repeat.child_steps[0].explicit is True
    assert collect.items is not None
    assert collect.items.steps[0].explicit is False

    plan = compile_workflow_plan(configuration, tmp_path, "base", "codex")
    assert {item.step: item.explicit for item in plan.items if item.phase == "step"}[
        "group/private"
    ] is False


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


def test_an_unknown_recommendation_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="recommends unknown workflow"):
        _load(
            tmp_path,
            HOTFIX.replace(
                "    restartable: true\n", "    recommended_next_workflow: nowhere\n"
            ),
        )


def test_an_heir_of_a_handoff_workflow_hands_off(tmp_path: Path) -> None:
    text = """workflows:
  - name: choose
    steps:
      - name: pick
        variables:
          - name: workflow
        hooks:
          after_complete:
            - handoff_to: "{{workflow}}"
  - target: Work.
    steps:
      - work: Work.
  - name: again
    inherit: choose
"""
    configuration = _load(tmp_path, text)
    assert configuration.workflows_by_name["again"].hands_off
    assert not configuration.workflows_by_name["target"].hands_off

    with pytest.raises(ConfigurationError, match="cannot also recommend"):
        _load(tmp_path, text + "    recommended_next_workflow: target\n")


def test_manual_is_read_but_never_inherited(tmp_path: Path) -> None:
    configuration = _load(
        tmp_path,
        "workflows:\n"
        "  - name: secret\n    manual: true\n    steps:\n      - a: A.\n"
        "  - name: copy\n    inherit: secret\n"
        "  - name: both\n    inherit: secret\n    manual: true\n",
    )

    by_name = configuration.workflows_by_name
    assert by_name["secret"].manual is True
    assert by_name["copy"].manual is False
    assert by_name["both"].manual is True


def test_manual_must_be_a_boolean(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="manual must be true or false"):
        _load(
            tmp_path,
            "workflows:\n  - name: x\n    manual: yes please\n    steps:\n"
            "      - a: A.\n",
        )
