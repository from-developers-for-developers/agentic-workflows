# SPDX-License-Identifier: GPL-3.0-or-later
"""The behavior documentation/examples.md promises, run with fake commands.

Each test takes an example's own YAML from the document, writes it into a
temporary project and drives it through the service. Commands the examples name
(a test runner, a migration, the project's reply script) are replaced by tiny
fakes, so nothing here touches a network or runs the repository's own checks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from tests.example_documents import (
    FILE_MARKER,
    example_configuration,
    example_yaml,
    examples,
)
from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.instructions import Instruction
from ww.items import WorkItem
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-1"
FAKE_RUNNER = (
    "import pathlib, sys; "
    "pathlib.Path('runs').open('a').write('run\\n'); "
    "sys.exit(1 if pathlib.Path('broken').exists() else 0)"
)


def _service(root: Path, configuration: str) -> WorkflowService:
    (root / "ww.yaml").write_text(configuration, encoding="utf-8")
    return WorkflowService(Storage(root))


def _start(service: WorkflowService, workflow: str) -> None:
    start_after_init(service, workflow, TASK, agent="codex", workflow_runtime="single")


def _complete(service: WorkflowService, text: str = "Done.") -> Instruction:
    return service.complete(TASK, artifact=text, summary_for_next=text)


def _example_1(root: Path) -> WorkflowService:
    configuration = example_configuration(1)
    (workflow,) = configuration["workflows"]  # type: ignore[misc]
    verify = next(step for step in workflow["steps"] if "verify" in step)
    verify["argv"] = [sys.executable, "-c", FAKE_RUNNER]
    return _service(root, yaml.safe_dump(configuration, sort_keys=False))


def test_example_1_runs_the_check_itself_and_moves_on(tmp_path: Path) -> None:
    service = _example_1(tmp_path)
    _start(service, "task")
    service.next(TASK)

    page = _complete(service, "Implemented.")

    assert page.item_name == "document"
    assert (tmp_path / "runs").read_text().splitlines() == ["run"]


def test_example_1_hands_a_failing_check_to_an_agent_to_fix(tmp_path: Path) -> None:
    service = _example_1(tmp_path)
    (tmp_path / "broken").touch()
    _start(service, "task")
    service.next(TASK)

    page = _complete(service, "Implemented.")

    assert page.handler_repair is not None
    (tmp_path / "broken").unlink()
    after = _complete(service, "Fixed the failure.")
    assert after.item_name == "document"
    assert (tmp_path / "runs").read_text().splitlines() == ["run", "run"]


def test_example_2_names_the_choices_in_the_instruction(tmp_path: Path) -> None:
    service = _service(tmp_path, example_yaml(2))
    _start(service, "reviewed-change")
    service.next(TASK)

    page = _complete(service, "Implemented.")

    assert page.item_name == "review"
    assert [choice.label for choice in page.choices] == ["approve", "rework"]
    assert page.action_text is not None
    assert '["approve", "rework"]' in page.action_text.replace(",", ", ").replace(
        ",  ", ", "
    )


def test_example_3_gives_every_item_one_lifecycle_stage(tmp_path: Path) -> None:
    service = _service(tmp_path, example_yaml(3))
    _start(service, "migrate-calls")
    service.next(TASK)
    for path in ("src/a.py", "src/b.py"):
        service.add_item(TASK, WorkItem(path, f"Migrate {path}."))
    _complete(service, "Collected.")

    for path in ("src/a.py", "src/b.py"):
        page = service.next(TASK)
        assert page.item_name == "handle-item"
        service.update_item(
            TASK, path, actual_solution="Migrated.", resolved=True, reported=True
        )
        _complete(service, "Migrated.")

    assert service.next(TASK).item_name == "summarize"


# A project-owned fake of the reply script example 4 names: a ledger of replies.
# Given a saved reply ID it updates that reply instead of creating another.
REPLY_SCRIPT = """import json, os, sys
comment, text, reply_id = sys.argv[1:4]
if os.path.exists("remote-down"):
    sys.exit(7)
ledger = json.load(open("ledger.json")) if os.path.exists("ledger.json") else {}
if not reply_id:
    reply_id = "r%d" % (len(ledger) + 1)
ledger[reply_id] = {"comment": comment, "text": text}
json.dump(ledger, open("ledger.json", "w"))
print(reply_id)
"""


def _example_4(root: Path) -> WorkflowService:
    """Run example 4 up to the report pass's collection step."""
    (root / "scripts").mkdir()
    (root / "scripts/reply-to-comment.py").write_text(REPLY_SCRIPT, encoding="utf-8")
    service = _service(root, example_yaml(4))
    _start(service, "review-comments")
    service.next(TASK)
    for number in (1, 2):
        service.add_item(
            TASK,
            WorkItem(
                f"c{number}",
                f"Comment {number}",
                fields=(("comment_id", str(100 + number)),),
            ),
        )
    _complete(service, "Collected.")
    # One batch analysis, then a quick checkpoint per item.
    service.next(TASK)
    for number in (1, 2):
        service.update_item(TASK, f"c{number}", processed_item="Shared cause.")
    _complete(service, "Analyzed together.")
    service.next(TASK)
    _complete(service, "Reused the items.")
    for _ in (1, 2):
        assert service.next(TASK).item_name == "analyze"
        _complete(service, "Confirmed.")
    # One batch fix, resolution recorded per item.
    assert service.next(TASK).item_name == "fix-together"
    for number in (1, 2):
        service.update_item(
            TASK, f"c{number}", actual_solution=f"fix {number}", resolved=True
        )
    _complete(service, "Fixed together.")
    service.next(TASK)
    return service


def _items(service: WorkflowService) -> dict[str, WorkItem]:
    run = service.load(TASK)[0].run_id
    return {item.id: item for item in service.tasks.read_items(TASK, run)}


def test_example_4_reports_every_comment_with_its_own_automatic_reply(
    tmp_path: Path,
) -> None:
    service = _example_4(tmp_path)

    _complete(service, "Reused the items.")

    items = _items(service)
    assert [items[i].field("reply_id") for i in ("c1", "c2")] == ["r1", "r2"]
    assert all(item.reported for item in items.values())
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger == {
        "r1": {"comment": "101", "text": "fix 1"},
        "r2": {"comment": "102", "text": "fix 2"},
    }


def test_example_4_reports_nothing_when_the_script_fails_and_retries_cleanly(
    tmp_path: Path,
) -> None:
    service = _example_4(tmp_path)
    (tmp_path / "remote-down").touch()

    _complete(service, "Reused the items.")

    assert service.load(TASK)[0].status == "failed"
    assert not any(item.reported for item in _items(service).values())
    (tmp_path / "remote-down").unlink()
    service.next(TASK, retry=True)
    assert all(item.reported for item in _items(service).values())
    assert len(json.loads((tmp_path / "ledger.json").read_text())) == 2


def _assessed(root: Path, workflow: str) -> WorkflowService:
    """Run example 5's workflow up to the chosen-outcome page of its assessment."""
    (root / "scripts").mkdir()
    (root / "scripts/migrate.py").write_text(
        "open('migrated', 'w').write('yes')\n", encoding="utf-8"
    )
    service = _service(root, example_yaml(5))
    _start(service, workflow)
    service.next(TASK)
    page = _complete(service, "Worked.")
    if page.choosing_outcome_of != "assess":
        page = _complete(service, "Assessed.")
    assert page.choosing_outcome_of == "assess"
    return service


def test_example_5_compact_gate_continues_on_positive(tmp_path: Path) -> None:
    service = _assessed(tmp_path, "gate")

    assert service.next(TASK, outcome="positive").item_name == "review"


def test_example_5_compact_gate_ends_the_workflow_on_negative(tmp_path: Path) -> None:
    service = _assessed(tmp_path, "gate")

    assert service.next(TASK, outcome="negative").status == "completed"


def test_example_5_flattened_branches_run_and_an_undeclared_one_continues(
    tmp_path: Path,
) -> None:
    service = _assessed(tmp_path, "branches")

    assert service.next(TASK, outcome="positive").item_name == "changelog"


def test_example_5_custom_labels_run_their_direct_handlers(tmp_path: Path) -> None:
    service = _assessed(tmp_path, "migrate")

    page = service.next(TASK, outcome="clean")

    assert (tmp_path / "migrated").read_text() == "yes"
    assert page.item_name == "update-workflow-summary"


def test_example_5_custom_label_can_hand_work_to_an_agent(tmp_path: Path) -> None:
    service = _assessed(tmp_path, "migrate")

    page = service.next(TASK, outcome="needs-fixes")

    assert page.item_name == "needs-fixes"
    assert not (tmp_path / "migrated").exists()


def test_example_6_prefers_local_then_project_then_global(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    user, project = tmp_path / "user", tmp_path / "project"
    user.mkdir()
    project.mkdir()
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(user))
    for kind, text in dict(examples())["6"]:
        marker = FILE_MARKER.match(text)
        assert kind == "yaml" and marker is not None
        name = marker.group(1)
        target = user / "ww.yaml" if name.startswith("~") else project / name
        target.write_text(text[marker.end() :], encoding="utf-8")

    assert main(["--root", str(project), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    found = {
        w["name"]: (w["source_level"], w["description"]) for w in report["workflows"]
    }
    assert found["review"][0] == "local"
    assert found["task"] == (
        "project",
        "Implement a change the way this project does.",
    )
    assert found["standup"][0] == "global"
    assert main(["--root", str(project), "discover"]) == 0
    listed = [
        line.split(" — ")[0]
        for line in capsys.readouterr().out.splitlines()
        if " — [" in line
    ]
    assert listed == ["- review", "- task", "- standup"]
