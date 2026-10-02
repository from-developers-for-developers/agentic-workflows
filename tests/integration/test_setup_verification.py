# SPDX-License-Identifier: GPL-3.0-or-later
"""The suggested setup runs checks and assigns only failures back to a worker."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from ww.service import WorkflowService
from ww.storage import Storage


@pytest.mark.parametrize(
    ("workflow", "step"),
    [
        ("feature", "implement"),
        ("hotfix", "fix"),
        ("bugfix", "fix"),
        ("merge-to-dev", "merge"),
    ],
)
@pytest.mark.parametrize("failing", [False, True])
def test_suggested_checks_run_once_without_a_verification_assignment(
    tmp_path: Path, workflow: str, step: str, failing: bool
) -> None:
    examples = Path(__file__).parents[2] / "documentation/examples.md"
    section = examples.read_text(encoding="utf-8").split("## 18.", 1)[1]
    fragment = section.split("```yaml\n", 1)[1].split("```", 1)[0]
    setup = yaml.safe_load(fragment)
    # Exercise the shipped example's check wiring without its Git lifecycle
    # or Node dependency. The substitute commands record every invocation.
    setup["hooks"].pop("before_start_workflow")
    setup["hooks"].pop("before_complete_workflow")
    for handler in setup["handlers"]:
        name = handler["name"]
        handler["argv"] = [
            sys.executable,
            "-c",
            "import pathlib, sys; "
            "p = pathlib.Path('checks.txt'); "
            "p.open('a').write(sys.argv[1] + '\\n'); "
            "broken = pathlib.Path('broken').exists() and sys.argv[1] == 'run-tests'; "
            "print('test failure' if broken else 'passed'); "
            "sys.exit(1 if broken else 0)",
            name,
        ]
    (tmp_path / "ww.yaml").write_text(
        yaml.safe_dump(setup, sort_keys=False), encoding="utf-8"
    )
    service = WorkflowService(Storage(tmp_path))
    service.start(workflow, "TASK-1", agent="codex", init_artifact="Change code.")
    page = service.next("TASK-1")
    if page.item_name == "investigate":
        service.complete("TASK-1", artifact="Investigated.", summary_for_next="Ready.")
        page = service.next("TASK-1")
    assert page.item_name == step
    assert all(rule.has_command for rule in page.rules)
    assert [rule.id for rule in page.rules] == [
        f"{step}/lint",
        f"{step}/typecheck",
        f"{step}/run-tests",
    ]
    log = tmp_path / "checks.txt"
    assert not log.exists()
    if failing:
        (tmp_path / "broken").touch()

    result = service.complete("TASK-1", artifact="Changed.", summary_for_next="Done.")
    assert log.read_text(encoding="utf-8").splitlines() == [
        "lint",
        "typecheck",
        "run-tests",
    ]
    if failing:
        assert result.item_name == step
        assert result.fix_required is not None
        assert [failure.id for failure in result.fix_required.failures] == [
            f"{step}/run-tests"
        ]
        assert "test failure" in result.fix_required.failures[0].output
        (tmp_path / "broken").unlink()
        result = service.complete("TASK-1", artifact="Fixed.", summary_for_next="Done.")
        assert log.read_text(encoding="utf-8").splitlines() == [
            "lint",
            "typecheck",
            "run-tests",
            "lint",
            "typecheck",
            "run-tests",
        ]
    assert result.fix_required is None
    assert result.item_name not in {step, "lint", "typecheck", "run-tests"}
