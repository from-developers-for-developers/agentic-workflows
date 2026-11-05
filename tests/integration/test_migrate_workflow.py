# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww-migrate``: bringing the configuration up to date with the operator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.builtin_workflows import builtin_workflow, is_builtin
from ww.cli import main
from ww.service import WorkflowService
from ww.storage import Storage

NAME = "ww-migrate"

pytestmark = pytest.mark.usefixtures("shipped_builtins")


def _project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )
    (root / "ww.json").write_text("{}", encoding="utf-8")
    return root


def _description(name: str) -> str:
    step = next(s for s in builtin_workflow(NAME).steps if s.name == name)
    return " ".join(step.description.split())


def test_it_detects_applies_resolves_with_the_operator_and_verifies() -> None:
    workflow = builtin_workflow(NAME)

    assert is_builtin(workflow)
    assert [step.name for step in workflow.steps] == [
        "detect",
        "apply",
        "resolve",
        "verify",
    ]
    assert [step.name for step in workflow.steps if step.interactive] == ["resolve"]
    # It edits the configuration in the checkout and commits nothing.
    assert not workflow.readonly and not workflow.hooks
    assert workflow.runtime == "single" and workflow.restartable
    assert "migrate --json" in _description("detect")
    assert "migrate --check --deprecations" in _description("detect")
    assert "migrate --apply`" in _description("apply")
    resolve = _description("resolve")
    assert "migrate --apply --choose <migration-id>=<option>" in resolve
    assert "The operator decides each one" in resolve
    verify = _description("verify")
    assert "migrate --check --deprecations" in verify and "lint" in verify
    assert "uncommitted for the operator to review and commit" in verify


@pytest.mark.parametrize("agent", ["claudecode", "codex"])
def test_it_plans_and_is_listed_with_ww_own_workflows(
    tmp_path: Path, agent: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path / "project")

    assert main(["--root", str(root), "plan", "-w", NAME, "--agent", agent]) == 0
    capsys.readouterr()
    assert main(["--root", str(root), "workflows"]) == 0
    catalog = json.loads(capsys.readouterr().out)["workflows"]
    assert NAME in [item["name"] for item in catalog]


def test_a_run_starts_with_detection(tmp_path: Path) -> None:
    service = WorkflowService(Storage(_project(tmp_path / "project")))
    service.start(
        NAME, "M-1", agent="codex", workflow_runtime="single", init_artifact="All."
    )

    page = service.next("M-1", caller_role="manager")
    assert page.item_name == "detect"
    assert "migrate --json" in page.action_text
