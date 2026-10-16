# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww visualize``: the Tasks data, the artifacts it serves, and the server."""

from __future__ import annotations

import importlib
import json
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ww.service import WorkflowService
from ww.storage import Storage
from ww.visualize.project import Project
from ww.visualize.server import VisualizerServer
from ww.visualize.tasks import artifact_text, task_detail, task_list
from ww.visualize.watch import Watcher

# A judged rule, a checked rule scoped to Markdown, and a ``fix`` hook.
WORKFLOWS = """rules:
  docs: [rules/docs/]
workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        rules:
          - Keep the public CLI unchanged.
        hooks:
          before_complete:
            - argv: [sh, -c, "test ! -e broken"]
              on_failure: fix
      - check: Check it.
"""

HEADER_RULE = """---
paths: ["*.md"]
check:
  shell: grep -L foo $WW_STEP_CHANGED_FILES || true
  assert: [empty]
---
Include "foo" in every Markdown file you change.
"""


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(name="root")
def _root(tmp_path: Path) -> Path:
    rules = tmp_path / "rules/docs"
    rules.mkdir(parents=True)
    (rules / "header.md").write_text(HEADER_RULE, encoding="utf-8")
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (tmp_path / "README.md").write_text("foo seed\n", encoding="utf-8")
    _git("init", "-q", "-b", "main", ".", cwd=tmp_path)
    _git("config", "user.email", "t@e.st", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    (tmp_path / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "seed", cwd=tmp_path)
    return tmp_path


def _developed_once_rejected(root: Path) -> WorkflowService:
    """Develop is rejected once, fixed, verified and accepted."""
    service = WorkflowService(Storage(root))
    service.start("task", "TASK-1", agent="codex", init_artifact="Do it.")
    assert service.next("TASK-1").item_name == "develop"
    (root / "notes.md").write_text("no marker here\n", encoding="utf-8")
    (root / "broken").write_text("", encoding="utf-8")
    service.complete("TASK-1", artifact="First try.", summary_for_next="Done.")
    (root / "notes.md").write_text("foo now\n", encoding="utf-8")
    (root / "broken").unlink()
    held = service.complete("TASK-1", artifact="Fixed.", summary_for_next="Done.")
    assert held.item_name == "develop-verify-1"
    service.complete(
        "TASK-1",
        artifact="Verified.",
        rule_results=(
            json.dumps(
                {
                    "id": "develop/1",
                    "status": "judged",
                    "verdict": "pass",
                    "checked": "c",
                }
            ),
        ),
    )
    return service


def _develop(detail: dict[str, Any]) -> dict[str, Any]:
    return next(
        item
        for item in detail["runs"][0]["items"]
        if item["name"] == "develop" and item["phase"] == "step"
    )


def test_a_finished_step_shows_how_each_rule_worked(root: Path) -> None:
    _developed_once_rejected(root)

    detail = task_detail(Project.load(Storage(root)), "TASK-1")

    rules = _develop(detail)["rules"]
    assert [entry["id"] for entry in rules["applied"]] == [
        "docs/header",
        "develop/1",
        "develop/sh",
    ]
    assert [(entry["id"], entry["status"]) for entry in rules["outcomes"]] == [
        ("docs/header", "passed"),
        ("develop/1", "verified pass"),
        ("develop/sh", "passed"),
    ]
    assert rules["fix_attempts"] == 1
    assert [bool(report["results"]) for report in rules["reports"]] == [True, True]
    failed = [
        result["id"]
        for result in rules["reports"][0]["results"]
        if result["status"] == "failed"
    ]
    assert failed == ["docs/header", "develop/sh"]
    verifier = next(
        item for item in detail["runs"][0]["items"] if item["kind"] == "verification"
    )
    assert verifier["verifies"] == "develop"
    assert [rule["id"] for rule in verifier["rules"]["verifies"]] == ["develop/1"]
    json.dumps(detail)


def test_the_list_names_the_task_its_status_and_current_step(root: Path) -> None:
    _developed_once_rejected(root)

    listed = task_list(Project.load(Storage(root)))["tasks"]

    assert [
        (entry["id"], entry["status"], entry["workflows"], entry["current_step"])
        for entry in listed
    ] == [("TASK-1", "in_progress", ["task"], "check")]


def test_an_unreadable_task_is_listed_with_its_reason(root: Path) -> None:
    _developed_once_rejected(root)
    broken = root / ".ww/tasks/TASK-2"
    broken.mkdir(parents=True)
    (broken / "state.json").write_text('{"schema_version": 99}', encoding="utf-8")

    project = Project.load(Storage(root))
    listed = {entry["id"]: entry for entry in task_list(project)["tasks"]}

    assert listed["TASK-2"]["status"] == "unreadable"
    assert listed["TASK-2"]["summary"]
    assert task_detail(project, "TASK-2")["status"] == "unreadable"


def test_only_a_run_s_own_artifacts_are_served(root: Path) -> None:
    _developed_once_rejected(root)
    project = Project.load(Storage(root))
    reference = _develop(task_detail(project, "TASK-1"))["artifact"]

    text = artifact_text(project, "TASK-1", "01-task", reference)

    assert "## Rules" in text
    for other in ("ww.yaml", "../ww.yaml", str(root / "ww.yaml")):
        with pytest.raises(Exception, match="is not an artifact"):
            artifact_text(project, "TASK-1", "01-task", other)


@pytest.fixture(name="server")
def _server(root: Path) -> Iterator[VisualizerServer]:
    storage = Storage(root)
    watcher = Watcher(lambda: Project.load(storage), interval=3600)
    server = VisualizerServer(0, storage, watcher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        watcher.stop()
        server.shutdown()
        server.server_close()


def _get(server: VisualizerServer, path: str) -> tuple[int, bytes]:
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_the_server_answers_the_page_and_its_data(server: VisualizerServer) -> None:
    _developed_once_rejected(server.storage.root)

    status, page = _get(server, "/")
    assert status == 200
    assert b"<title>ww</title>" in page
    status, body = _get(server, "/api/atlas")
    assert status == 200
    assert [entry["name"] for entry in json.loads(body)["workflows"]] == ["task"]
    status, body = _get(server, "/api/tasks")
    assert [entry["id"] for entry in json.loads(body)["tasks"]] == ["TASK-1"]
    status, body = _get(server, "/api/task?id=TASK-1")
    assert json.loads(body)["runs"][0]["workflow"] == "task"
    status, body = _get(server, "/api/artifact?task=TASK-1&run=01-task&ref=ww.yaml")
    assert status == 400
    assert "is not an artifact" in json.loads(body)["error"]
    assert _get(server, "/api/task")[0] == 400
    assert _get(server, "/nowhere")[0] == 404


def test_a_request_naming_another_host_is_refused(server: VisualizerServer) -> None:
    port = server.server_address[1]
    for host, expected in (
        (f"attacker.example:{port}", 403),
        ("127.0.0.1:1", 403),
        ("", 403),
        (f"localhost:{port}", 200),
        (f"127.0.0.1:{port}", 200),
    ):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/tasks", headers={"Host": host}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        assert (host, status) == (host, expected)


def test_a_watched_change_is_announced_and_refreshes_the_data(
    server: VisualizerServer,
) -> None:
    root = server.storage.root
    url = f"http://127.0.0.1:{server.server_address[1]}/api/events"
    events: list[str] = []
    connected = threading.Event()

    def listen() -> None:
        with urllib.request.urlopen(url, timeout=10) as stream:
            for raw in stream:
                line = raw.decode("utf-8").strip()
                if line.startswith("event: hello"):
                    connected.set()
                if (
                    line.startswith("data:")
                    and connected.is_set()
                    and events is not None
                ):
                    events.append(line)
                if len(events) == 2:
                    return

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    assert connected.wait(10)
    before = json.loads(_get(server, "/api/atlas")[1])["workflows"][0]["description"]
    (root / "ww.yaml").write_text(
        WORKFLOWS.replace(
            "- name: task\n", "- name: task\n    description: Renamed.\n"
        ),
        encoding="utf-8",
    )
    server.watcher.poll()
    listener.join(10)

    assert json.loads(events[-1].removeprefix("data: ")) == {
        "config": True,
        "tasks": False,
    }
    after = json.loads(_get(server, "/api/atlas")[1])["workflows"][0]["description"]
    assert (before, after) == ("", "Renamed.")


def test_the_command_serves_the_checkout_until_interrupted(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = importlib.import_module("ww.cli.main")
    seen: dict[str, Any] = {}

    def serve(storage: Storage, **options: Any) -> None:
        seen.update(options, config_root=storage.config_root)
        options["ready"]("http://127.0.0.1:1/")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "serve_visualizer", serve)

    assert (
        cli.main(["--root", str(root), "visualize", "--port", "38001", "--no-open"])
        == 0
    )

    output = capsys.readouterr().out
    assert "ww visualize: http://127.0.0.1:1/ (Ctrl+C to stop)" in output
    assert "ww visualize stopped." in output
    assert seen["port"] == 38001
    assert seen["open_browser"] is None
    assert seen["config_root"] == root
    assert not (root / ".ww/executions.jsonl").exists()
