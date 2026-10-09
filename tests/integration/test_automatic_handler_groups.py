# SPDX-License-Identifier: GPL-3.0-or-later
"""Reusable automated handler sequences keep a single step or hook lifecycle."""

from pathlib import Path

import pytest

from tests.workflow_helpers import configured_service
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.service import WorkflowService
from ww.storage import Storage

CATALOG = """handlers:
  - first: ~
    shell: echo first >> order
  - bundle: ~
    handlers:
      - first: ~
      - shell: echo second >> order
      - argv: [sh, -c, 'echo third >> order']
"""


@pytest.mark.parametrize("runtime", ["single", "auto"])
def test_group_runs_in_order_without_agent_completions(
    tmp_path: Path, runtime: str
) -> None:
    service = configured_service(
        tmp_path,
        CATALOG
        + """workflows:
  - name: task
    steps:
      - bundle: ~
      - work: Manual work afterwards.
        kind: prompt
""",
    )
    service.start("task", "T", workflow_runtime=runtime)
    result = service.next("T", caller_role="manager")
    assert result.item_name == "work"
    assert (tmp_path / "order").read_text() == "first\nsecond\nthird\n"
    state, snapshot = service.load("T")
    members = [
        (item, record)
        for item, record in zip(snapshot.plan.items, state.item_executions, strict=True)
        if item.step == "bundle"
    ]
    assert len(members) == 3
    assert all(
        item.owner == "ww" and record.status == "completed" for item, record in members
    )
    assert all(item.step == "bundle" and item.phase == "step" for item, _ in members)


@pytest.mark.parametrize("runtime", ["single", "auto"])
def test_named_automated_group_can_run_as_a_hook(tmp_path: Path, runtime: str) -> None:
    service = configured_service(
        tmp_path,
        CATALOG
        + """hooks:
  before_start_workflow:
    - bundle: ~
workflows:
  - name: task
    steps:
      - work: Manual work.
""",
    )
    service.start("task", "T", workflow_runtime=runtime)
    assert (tmp_path / "order").read_text() == "first\nsecond\nthird\n"
    _, snapshot = service.load("T")
    members = [
        item for item in snapshot.plan.items if item.phase == "before_start_workflow"
    ]
    assert len(members) == 3
    assert all(item.owner == "ww" for item in members)


@pytest.mark.parametrize("runtime", ["single", "auto"])
@pytest.mark.parametrize("catalog", [False, True])
def test_group_completion_hook_runs_after_all_members(
    tmp_path: Path, runtime: str, catalog: bool
) -> None:
    group = """  - checks: ~
    handlers:
      - shell: echo type-check >> order
        on_failure: fix
      - argv: [sh, -c, 'echo eslint >> order']
        on_failure: fix
    hooks:
      before_complete:
        - shell: echo commit >> order
"""
    configuration = (
        "handlers:\n"
        + group
        + "workflows:\n  - name: task\n    steps:\n      - checks: ~\n"
        if catalog
        else "workflows:\n  - name: task\n    steps:\n"
        + "".join("    " + line + "\n" for line in group.splitlines())
    )
    service = configured_service(tmp_path, configuration)
    service.start("task", "T", workflow_runtime=runtime)
    service.next("T", caller_role="manager")
    assert (tmp_path / "order").read_text() == "type-check\neslint\ncommit\n"


def test_nested_groups_and_multiple_reuses_keep_order(tmp_path: Path) -> None:
    service = configured_service(
        tmp_path,
        CATALOG
        + """  - twice: ~
    handlers:
      - bundle: ~
      - handlers:
          - shell: echo fourth >> order
          - handlers:
              - shell: echo fifth >> order
      - bundle: ~
workflows:
  - name: task
    steps:
      - twice: ~
""",
    )
    service.start("task", "T")
    service.next("T")
    assert (tmp_path / "order").read_text().splitlines() == [
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "first",
        "second",
        "third",
    ]


def test_failure_repairs_group_member_without_replaying_predecessors(
    tmp_path: Path,
) -> None:
    service = configured_service(
        tmp_path,
        """handlers:
  - bundle: ~
    on_failure: fix
    on_failure_instruction: Repair the input.
    handlers:
      - shell: echo first >> order
      - shell: test -e fixed || { echo broken; exit 1; }
      - shell: echo last >> order
workflows:
  - name: task
    steps:
      - bundle: ~
""",
    )
    service.start("task", "T")
    failed = service.next("T")
    assert failed.handler_repair is not None
    assert "Repair the input." in failed.action_text
    assert (tmp_path / "order").read_text() == "first\n"
    service = WorkflowService(Storage(tmp_path))
    (tmp_path / "fixed").touch()
    service.complete("T", artifact="Fixed input.")
    assert (tmp_path / "order").read_text() == "first\nlast\n"


def test_hook_group_members_become_separate_completion_checks(tmp_path: Path) -> None:
    service = configured_service(
        tmp_path,
        """handlers:
  - checks: ~
    handlers:
      - shell: echo first; exit 1
      - shell: echo second; exit 1
workflows:
  - name: task
    steps:
      - work: Manual work.
        hooks:
          before_complete:
            - checks: ~
              on_failure: fix
              on_failure_instruction: Fix both commands.
""",
    )
    service.start("task", "T")
    service.next("T")
    failed = service.complete("T", artifact="Work done.", summary_for_next="Done.")
    assert failed.fix_required is not None
    assert len(failed.fix_required.failures) == 2
    assert all(
        failure.text == "Fix both commands." for failure in failed.fix_required.failures
    )


def test_group_catalog_references_can_resolve_later_declarations(
    tmp_path: Path,
) -> None:
    service = configured_service(
        tmp_path,
        """handlers:
  - bundle: ~
    handlers:
      - later: ~
  - later: ~
    shell: echo later > order
workflows:
  - name: task
    steps:
      - bundle: ~
""",
    )
    service.start("task", "T")
    service.next("T")
    assert (tmp_path / "order").read_text() == "later\n"


@pytest.mark.parametrize(
    "member",
    [
        "kind: prompt\n        name: manual",
        "manual: Think about it.",
        "mcp: github\n        name: manual",
        "shell: 'true'\n        variables: [{message: Agent-provided value}]",
    ],
)
def test_agent_work_is_rejected_even_in_unused_groups(
    tmp_path: Path, member: str
) -> None:
    configured_service(
        tmp_path,
        f"""handlers:
  - bundle: ~
    handlers:
      - {member}
workflows:
  - name: task
    steps:
      - work: Manual work.
""",
    )
    with pytest.raises(ConfigurationError, match="automated|agent input"):
        load_configuration(tmp_path / "ww.yaml")


@pytest.mark.parametrize("entries", ["[]", "null", "bad"])
def test_group_requires_nonempty_list(tmp_path: Path, entries: str) -> None:
    service = configured_service(
        tmp_path,
        (
            f"handlers:\n  - bundle: ~\n    handlers: {entries}\n"
            "workflows:\n  - name: task\n    steps: []\n"
        ),
    )
    with pytest.raises(ConfigurationError, match="non-empty list"):
        service.start("task", "T")


def test_group_cycles_are_rejected(tmp_path: Path) -> None:
    service = configured_service(
        tmp_path,
        """handlers:
  - one: ~
    handlers:
      - two: ~
  - two: ~
    handlers:
      - one: ~
workflows:
  - name: task
    steps: []
""",
    )
    with pytest.raises(ConfigurationError, match="cycle"):
        service.start("task", "T")


@pytest.mark.parametrize(
    "extra",
    [
        "shell: 'true'",
        "steps: [{work: Manual work.}]",
        "steps: [{work: Manual work.}]",
        "variables: [message]",
    ],
)
def test_group_cannot_combine_with_an_action_or_step_container(
    tmp_path: Path, extra: str
) -> None:
    service = configured_service(
        tmp_path,
        f"""handlers:
  - bundle: ~
    handlers:
      - shell: 'true'
    {extra}
workflows:
  - name: task
    steps: []
""",
    )
    with pytest.raises(ConfigurationError, match="combine"):
        service.start("task", "T")
