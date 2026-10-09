# SPDX-License-Identifier: GPL-3.0-or-later
"""Removed syntax fails at the public boundary; incompatible runs stay intact."""

import json
from pathlib import Path

import pytest
import yaml

from tests.workflow_helpers import configured_service
from ww.cli.parser import build_parser
from ww.config import parse_yaml_text
from ww.errors import ConfigurationError, StateError
from ww.project_config import load_project_config
from ww.service import WorkflowService
from ww.storage import Storage


@pytest.mark.parametrize(
    "key,value",
    [
        ("loop", [{"work": "Do it."}]),
        ("break", "Done."),
        ("continue", "More."),
        ("max_rounds", 3),
        ("assignment", "per_round"),
        ("assignment", "per_step"),
    ],
)
@pytest.mark.parametrize(
    "location", ["step", "group", "items", "children", "assessment", "handler", "hook"]
)
def test_removed_settings_are_rejected_at_every_nesting_location(
    key: str, value: object, location: str
) -> None:
    step = {"name": "work", key: value}
    workflow: dict[str, object] = {"name": "task", "steps": [step]}
    document: dict[str, object] = {"workflows": [workflow]}
    if location == "group":
        workflow["steps"] = [{"name": "group", "steps": [step]}]
    elif location in {"items", "children"}:
        workflow["steps"] = [{"name": "collect", location: {"steps": [step]}}]
    elif location == "assessment":
        workflow["steps"] = [
            {
                "assess": {
                    "question": "Proceed?",
                    "outcomes": {"positive": {"steps": [step]}},
                }
            }
        ]
    elif location == "handler":
        document["handlers"] = [step]
        workflow["steps"] = [{"work": None}]
    elif location == "hook":
        workflow["steps"] = [{"name": "develop", "hooks": {"after_complete": [step]}}]
    with pytest.raises(ConfigurationError, match="removed|unknown key"):
        parse_yaml_text(yaml.safe_dump(document, sort_keys=False))


def test_removed_round_limit_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ww.json"
    path.write_text(json.dumps({"limits": {"rounds": 3}}))
    with pytest.raises(ConfigurationError, match="limits.rounds was removed"):
        load_project_config(path)


@pytest.mark.parametrize(
    "argv",
    [
        ["loop", "TASK-1", "--break"],
        ["loop", "TASK-1", "--continue"],
        ["complete", "TASK-1", "--break"],
        ["complete", "TASK-1", "--continue"],
        ["next", "TASK-1", "--max-rounds", "3"],
    ],
)
def test_removed_commands_and_options_are_unavailable(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(argv)
    assert error.value.code == 2


@pytest.mark.parametrize("component", ["snapshot", "state"])
def test_old_run_schema_is_refused_without_mutation(
    tmp_path: Path, component: str
) -> None:
    text = "workflows:\n  - task: ~\n    steps:\n      - work: Do it.\n"
    service = configured_service(tmp_path, text)
    service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    task = tmp_path / ".ww" / "tasks" / "TASK-1"
    path = task / "state.json"
    data = json.loads(path.read_text())
    # Persisted task aggregates own one snapshot and state per run.
    run = data["runs"][0]
    run[component]["schema_version"] -= 1
    path.write_text(json.dumps(data))
    evidence = {
        p.relative_to(task): p.read_bytes() for p in task.rglob("*") if p.is_file()
    }
    restarted = WorkflowService(Storage(tmp_path))
    with pytest.raises(StateError, match="unsupported.*schema.*previous WW build"):
        restarted.status("TASK-1")
    after = {
        p.relative_to(task): p.read_bytes() for p in task.rglob("*") if p.is_file()
    }
    assert after == evidence


def test_force_cannot_end_ordinary_running_work(tmp_path: Path) -> None:
    text = "workflows:\n  - task: ~\n    steps:\n      - work: Do it.\n"
    service = configured_service(tmp_path, text)
    service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    service.next("TASK-1")
    with pytest.raises(StateError, match="not failed or interrupted"):
        service.next("TASK-1", force=True, force_reason="Stop here.")
