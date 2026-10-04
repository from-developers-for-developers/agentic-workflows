# SPDX-License-Identifier: GPL-3.0-or-later
"""Interactive steps: a recorded, ended conversation with the operator."""

from __future__ import annotations

import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.errors import StateError
from ww.items import WorkItem
from ww.operator_ui import OperatorPageResult, run_operator_page, server
from ww.operator_ui.sheet import Answer, AnswerSheet
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """profiles:
  architect: Think in components.
workflows:
  - name: task
    steps:
      - name: discuss
        description: Agree the architecture with the operator.
        profile: architect
        interactive: true
      - name: build
        description: Build it.
      - name: confirm
        description: Show the result to the operator.
        interactive: true
"""


def _service(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    return WorkflowService(Storage(tmp_path))


def test_an_interactive_step_is_held_by_the_manager_and_gated_on_the_record(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime="auto",
        init_artifact="Do it.",
        caller_role="manager",
    )
    md = MarkdownOutputAdapter()

    discuss = service.next("TASK-1", caller_role="manager")
    assert (discuss.item_name, discuss.interactive, discuss.role) == (
        "discuss",
        True,
        "manager",
    )
    assert discuss.requested_profile is None
    rendered = md.render_instruction(discuss)
    assert "### Worker bootstrap" not in rendered
    assert "because a delegated worker cannot talk to them" in rendered
    assert "### Interaction with the operator" in rendered
    assert "clear contextual completion" in rendered
    assert "`done`, `I'm done`, `looks good, continue`" in rendered
    assert "ask naturally whether they want to continue or finish" in rendered
    assert "`Done for today` means pause" in rendered
    assert "ww done" not in rendered
    assert "Record nothing while you talk." in rendered
    assert (
        "./ww interact TASK-1 --role manager --transcript - --end <<'EOF'\n"
        "Agent: <what you said, verbatim>\n" in rendered
    )
    assert "--operator-said" not in rendered
    assert "Nothing is recorded yet." in rendered

    # No completion, and no ending, before anything was recorded.
    with pytest.raises(StateError, match="'discuss' is interactive"):
        service.complete("TASK-1", artifact="x", summary_for_next="x")
    with pytest.raises(StateError, match="nothing was recorded"):
        service.interact("TASK-1", end=True, caller_role="manager")
    with pytest.raises(
        StateError, match="needs --transcript, --operator-said, --agent-said"
    ):
        service.interact("TASK-1", caller_role="manager")

    service.interact(
        "TASK-1",
        agent="Proposed a listener that dispatches a queued job per upload.",
        caller_role="manager",
    )
    open_page = service.interact(
        "TASK-1",
        operator="Fine, but the upload must never wait on image work.",
        caller_role="manager",
    )
    assert open_page.interaction_entries == 2
    assert "2 entries recorded so far" in md.render_instruction(open_page)
    ended = service.interact("TASK-1", end=True, caller_role="manager")
    assert ended.interaction_ended is True
    assert "has ended this interaction; complete the step now" in (
        md.render_instruction(ended)
    )
    with pytest.raises(StateError, match="has ended; complete the step"):
        service.interact("TASK-1", operator="more", caller_role="manager")

    build = service.complete(
        "TASK-1",
        artifact="Agreed: queue-based resize.",
        summary_for_next="Queue-based resize agreed with the operator.",
        caller_role="manager",
    )
    assert build.item_name == "build"
    assert "### Interaction with the operator" not in md.render_instruction(build)

    # The record is one append-only file per task, each entry naming its step.
    text = (tmp_path / ".ww/tasks/TASK-1/interactions.md").read_text(encoding="utf-8")
    assert text.startswith("# TASK-1 — interactions with the operator\n")
    assert "· 01-task · discuss · agent\n\nProposed a listener" in text
    assert "· 01-task · discuss · operator\n\nFine, but the upload" in text
    assert "· 01-task · discuss · end\n\nThe operator ended the interaction." in text
    assert service.interactions_text("TASK-1") == text

    # A plain step cannot be recorded against.
    service.next("TASK-1", caller_role="manager")
    with pytest.raises(StateError, match="'build' is not interactive"):
        service.interact("TASK-1", operator="hello", caller_role="manager")


def test_the_cli_records_and_prints_interactions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    service.start("task", "TASK-2", agent="codex", init_artifact="Do it.")
    service.next("TASK-2")
    root = ["--root", str(tmp_path)]

    record = [*root, "interact", "TASK-2", "--role", "worker"]
    assert main([*record, "--operator-said", "Go ahead."]) == 0
    assert main([*record, "--end"]) == 0
    capsys.readouterr()
    assert main([*root, "interactions", "TASK-2"]) == 0
    out = capsys.readouterr().out
    assert "· 01-task · discuss · operator\n\nGo ahead." in out
    assert out.rstrip().endswith("The operator ended the interaction.")


def test_a_transcript_records_both_sides_in_one_call(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.start("task", "TASK-4", agent="codex", init_artifact="Do it.")
    service.next("TASK-4")

    for wrong, match in (
        ("Hello.\nAgent: Hi.", "the transcript starts with 'Hello.'"),
        ("\n  \n", "the transcript has no entries"),
        ("Operator:\n", "the transcript has no entries"),
    ):
        with pytest.raises(StateError, match=match):
            service.interact("TASK-4", transcript=wrong)
    with pytest.raises(StateError, match="leave out --operator-said"):
        service.interact("TASK-4", transcript="Agent: Hi.", operator="Hi.")

    ended = service.interact(
        "TASK-4",
        transcript=(
            "\n"
            "Agent: Shall the upload wait on resizing?\n"
            "**Operator:** No.\n"
            "It must never wait.\n"
            "\n"
            "agent: Then a queue.\n"
            "**Agent**:\n"
            "OPERATOR:   Agreed.\n"
        ),
        end=True,
    )

    assert (ended.interaction_entries, ended.interaction_ended) == (4, True)
    entries = service.interactions.entries("TASK-4")
    assert [(entry.speaker, entry.text) for entry in entries] == [
        ("agent", "Shall the upload wait on resizing?"),
        ("operator", "No.\nIt must never wait."),
        ("agent", "Then a queue."),
        ("operator", "Agreed."),
        ("end", "The operator ended the interaction."),
    ]
    assert len({entry.at for entry in entries}) == 1


def test_the_cli_reads_a_transcript_from_stdin_or_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = _service(tmp_path)
    service.start("task", "TASK-5", agent="codex", init_artifact="Do it.")
    service.next("TASK-5")
    record = ["--root", str(tmp_path), "interact", "TASK-5", "--role", "worker"]

    monkeypatch.setattr("sys.stdin", io.StringIO("Agent: Ready?\nOperator: Go.\n"))
    assert main([*record, "--transcript", "-"]) == 0
    capsys.readouterr()
    assert main([*record, "--transcript", "-", "--agent-said", "x"]) != 0
    assert "leave out --operator-said" in capsys.readouterr().err
    later = tmp_path / "later.txt"
    later.write_text("Agent: Done?\nOperator: Done.\n", encoding="utf-8")
    assert main([*record, "--transcript", str(later), "--end"]) == 0

    speakers = [entry.speaker for entry in service.interactions.entries("TASK-5")]
    assert speakers == ["agent", "operator", "agent", "operator", "end"]
    assert main([*record, "--transcript", str(tmp_path / "missing.txt")]) != 0


CHOICES = """workflows:
  - name: manual
    steps:
      - name: verify
        description: Show the test case to the operator.
        interactive: true
        choices:
          - pass: The test case passed.
          - fail: The test case failed; no comment.
          - fail and give comment: The test case failed; the operator explains why.
"""


@pytest.mark.parametrize(
    ("agent", "mechanism"),
    [
        ("claudecode", "`AskUserQuestion` tool"),
        ("codex", "host's available question-tool schema"),
        ("gemini", "`ask_user` tool"),
        ("cursor", "`AskQuestion` tool"),
        ("antigravity", "`ask_question` tool"),
        ("grok", "`ask_user_question` tool"),
        ("kimi", "numbered list in your reply"),
    ],
)
def test_choices_resolve_to_the_agent_mechanism_and_gate_the_end(
    tmp_path: Path, agent: str, mechanism: str
) -> None:
    (tmp_path / "ww.yaml").write_text(CHOICES, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    service.start("manual", "TASK-3", agent=agent, init_artifact="Test it.")
    md = MarkdownOutputAdapter()

    verify = service.next("TASK-3")
    assert [choice.label for choice in verify.choices] == [
        "pass",
        "fail",
        "fail and give comment",
    ]
    rendered = md.render_instruction(verify)
    assert "#### Choices" in rendered
    assert "3. `fail and give comment` — The test case failed" in rendered
    assert mechanism in rendered
    assert (
        "./ww interact TASK-3 --role worker --transcript - "
        "--choice=\"<label or number>\" --end <<'EOF'" in rendered
    )
    assert "Nothing chosen yet." in rendered
    if agent == "codex":
        assert "question-tool schema" in rendered
        assert "structured options when offered" in rendered
        assert "text-only question only when required" in rendered
        assert "timeout, dismissal, or preselected value is not an answer" in rendered

    with pytest.raises(StateError, match="is not one of the choices"):
        service.interact("TASK-3", choice="maybe")
    with pytest.raises(StateError, match="record the operator's pick with --choice"):
        service.interact("TASK-3", operator="I ran it.", end=True)

    picked = service.interact(
        "TASK-3", choice="3", operator="The button stays disabled after upload."
    )
    assert picked.chosen == "fail and give comment"
    assert "Chosen so far: `fail and give comment`." in md.render_instruction(picked)
    service.interact("TASK-3", choice="Pass")  # a later pick replaces the earlier one
    ended = service.interact("TASK-3", end=True)
    assert ended.chosen == "pass"

    text = service.interactions_text("TASK-3")
    assert "· verify · operator\n\nChoice: fail and give comment" in text
    assert "The button stays disabled after upload." in text
    assert "· verify · operator\n\nChoice: pass" in text
    state, _ = service.load("TASK-3")
    assert state.item_executions[state.cursor].chosen == "pass"


def test_a_transcript_a_choice_and_the_end_go_in_one_call(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(CHOICES, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    service.start("manual", "TASK-6", agent="codex", init_artifact="Test it.")
    service.next("TASK-6")

    ended = service.interact(
        "TASK-6",
        transcript="Agent: Did it pass?\nOperator: It failed: the button stays off.",
        choice="3",
        end=True,
    )

    assert (ended.chosen, ended.interaction_ended) == ("fail and give comment", True)
    speakers = [entry.speaker for entry in service.interactions.entries("TASK-6")]
    assert speakers == ["operator", "agent", "operator", "end"]
    assert service.interactions.entries("TASK-6")[0].text == (
        "Choice: fail and give comment"
    )


def test_choices_are_a_json_core_value_in_instructions_and_input_descriptions(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: manual
    steps:
      - name: verify
        description: Set the result using {{ww.choices}}.
        interactive: true
        choices:
          - 'A, "quoted" 🧪': The label keeps its punctuation.
        variables:
          - result: Set result to one of {{ww.choices}}.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("manual", "TASK-CHOICES", agent="codex", init_artifact="Test.")

    page = service.next("TASK-CHOICES")
    rendered = MarkdownOutputAdapter().render_instruction(page)

    assert page.action_text is not None
    assert '["A, \\"quoted\\" 🧪"]' in page.action_text
    assert page.required_values[0].description == (
        'Set result to one of ["A, \\"quoted\\" 🧪"].'
    )
    assert "Choices" in rendered
    assert page.choices[0].label == 'A, "quoted" 🧪'


MANUAL_TESTS = """workflows:
  - name: manual
    steps:
      - name: collect
        description: Collect the test cases.
        items:
          analyze: Show the test case to the operator.
          interactive: page
          choices:
            - pass: The test case passed.
            - fail: The test case failed; the operator explains why.
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _get_state(port: int, *, attempts: int = 100) -> dict[str, Any]:
    """Poll the page until a waiter serves it, as the browser tab does."""
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/state") as reply:
                return cast(dict[str, Any], json.load(reply))
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.05)
    raise AssertionError("the operator page did not come up")


def _post(port: int, path: str, **payload: object) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as reply:
            return reply.status, cast(dict[str, Any], json.load(reply))
    except urllib.error.HTTPError as error:
        return error.code, cast(dict[str, Any], json.load(error))


def _act(port: int, **payload: object) -> tuple[int, dict[str, Any]]:
    return _post(port, "/act", **payload)


class _Waiter:
    """Runs one ``interact --await`` in the background, as the agent's shell does."""

    def __init__(self, service: WorkflowService, task_id: str, timeout: float = 5):
        self.opened: list[str] = []
        self.result: OperatorPageResult | None = None
        self.error: BaseException | None = None

        def run() -> None:
            try:
                self.result = run_operator_page(
                    service, task_id, timeout=timeout, open_browser=self.opened.append
                )
            except BaseException as error:  # noqa: BLE001 - surfaced by join
                self.error = error

        self.thread = threading.Thread(target=run)
        self.thread.start()

    def join(self) -> OperatorPageResult:
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "the waiter did not return"
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _sheet_file(tmp_path: Path, task_id: str) -> dict[str, Any]:
    path = tmp_path / ".ww/operator-ui" / f"{task_id}.json"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _collect(
    service: WorkflowService, task_id: str, count: int, agent: str = "claudecode"
) -> None:
    start_after_init(service, "manual", task_id, agent=agent)
    service.next(task_id)
    for number in range(1, count + 1):
        service.add_item(task_id, WorkItem(f"case-{number}", f"Test case {number}."))
    service.complete(task_id, artifact="collected", summary_for_next="Cases.")


def test_the_operator_page_is_an_answer_sheet_that_ww_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    monkeypatch.setenv("WW_OPERATOR_PORT", str(port))
    monkeypatch.setattr(server, "_OPEN_BROWSER_AFTER", 0.3)
    (tmp_path / "ww.yaml").write_text(MANUAL_TESTS, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    md = MarkdownOutputAdapter()
    _collect(service, "TASK-4", 3)

    first = service.next("TASK-4")
    assert (first.ui, first.interactive, first.agent) == (True, True, "claudecode")
    rendered = md.render_instruction(first)
    assert "### Operator page" in rendered
    assert "> **Operator page.** This stage is answered by the operator" in rendered
    assert rendered.index("**Operator page.**") < rendered.index("### Operator page")
    assert "WW_OPERATOR_WAIT=1800 ./ww interact TASK-4 --role worker --await" in (
        rendered
    )
    assert "`run_in_background` option and go on with the conversation" in rendered
    assert "### Interaction with the operator" not in rendered
    assert "answer" not in first.to_dict()  # the core model knows only the flag

    # The sheet lists every item; answers land in the sheet file at once, in
    # any order, and can be revised until applied.  The item is untouched.
    waiter = _Waiter(service, "TASK-4")
    state = _get_state(port)
    assert state["current_item_id"] == "case-1"
    assert [(item["id"], item["status"]) for item in state["items"]] == [
        ("case-1", "open"),
        ("case-2", "open"),
        ("case-3", "open"),
    ]
    assert [choice["label"] for choice in state["choices"]] == ["pass", "fail"]
    assert waiter.opened == []  # a polling tab was seen, so none was opened
    assert _act(port, action="answer", item_id="case-2") == (
        400,
        {"error": "pick one of the choices"},
    )
    assert (
        _act(
            port,
            action="answer",
            item_id="case-2",
            choice="fail",
            comment="The error is not shown.",
        )[0]
        == 200
    )
    sheet = _sheet_file(tmp_path, "TASK-4")
    assert sheet["runs"]["01-manual"]["case-2"]["comment"] == "The error is not shown."
    task_files = "".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / ".ww/tasks/TASK-4").rglob("*")
        if path.is_file()
    )
    assert "The error is not shown." not in task_files  # nothing in the task yet
    assert _act(port, action="answer", item_id="case-2", choice="2")[0] == 200
    sheet = _sheet_file(tmp_path, "TASK-4")
    assert sheet["runs"]["01-manual"]["case-2"]["choice"] == "fail"
    assert sheet["runs"]["01-manual"]["case-2"]["comment"] == ""
    assert "answer" not in service.item("TASK-4", "case-2").to_dict()
    state = _get_state(port)
    shown = next(item for item in state["items"] if item["id"] == "case-2")
    assert (state["answered"], shown["status"], shown["answer"]) == (
        1,
        "answered",
        "fail",
    )
    assert _act(port, action="answer", item_id="case-1", choice="pass")[0] == 200
    assert _act(port, action="pause")[0] == 200

    # A pause ends the wait; the answers given so far are applied in plan
    # order through the public calls, and the walk stops at case-3.
    result = waiter.join()
    assert (result.outcome, result.applied, result.paused) == (
        "paused",
        ("case-1", "case-2"),
        True,
    )
    assert (result.answered, result.total) == (2, 3)
    text = result.render()
    assert "The operator said they are done for now." in text
    assert "Applied the answers of case-1, case-2: each stage is completed" in text
    assert "Stop here; do not wait again" in text
    page = service.instruction("TASK-4")
    assert (page.item_name, page.item_status, page.ui) == (
        "handle-item",
        "in_progress",
        True,
    )
    assert "The operator is done for now. Stop here" in md.render_instruction(page)
    execution, _snapshot = service.load("TASK-4")
    done = [r for r in execution.item_executions if r.status == "completed"][-2:]
    assert [r.chosen for r in done] == ["pass", "fail"]
    assert all(r.interaction_ended for r in done)
    case_1 = service.item("TASK-4", "case-1")
    assert (case_1.resolved, case_1.reported) == (True, True)
    assert done[1].artifact is not None
    artifact = (tmp_path / done[1].artifact).read_text(encoding="utf-8")
    assert "## Result\n\n# handle-item: case-2\n\nTest case 2.\n\n" in artifact
    assert "Operator's answer: `fail`" in artifact
    assert _sheet_file(tmp_path, "TASK-4")["runs"]["01-manual"] == {}

    # An applied answer is shown from the record and cannot change; the last
    # answer lifts the pause and ends the wait by itself.
    waiter = _Waiter(service, "TASK-4")
    state = _get_state(port)
    shown = next(item for item in state["items"] if item["id"] == "case-1")
    assert (shown["status"], shown["answer"], state["paused"]) == (
        "processed",
        "pass",
        True,
    )
    assert _act(port, action="answer", item_id="case-1", choice="fail") == (
        400,
        {"error": "the answer of 'case-1' was already applied; it cannot change"},
    )
    assert (
        _act(port, action="answer", item_id="case-3", choice="pass", comment="Fine.")[0]
        == 200
    )
    result = waiter.join()
    assert (result.outcome, result.applied, result.paused) == (
        "answered",
        ("case-3",),
        False,
    )
    assert "Every item is answered; go on with the page above." in result.render()
    finished = service.instruction("TASK-4")
    assert (finished.ui, finished.item_name) == (False, "update-workflow-summary")

    record = service.interactions_text("TASK-4")
    assert "· handle-item · case-2 · operator\n\nChoice: fail" in record
    assert "· handle-item · case-3 · pause\n\nThe operator is" in record
    assert "· handle-item · case-3 · operator\n\nFine." in record
    assert "· handle-item · case-3 · end\n\nThe operator ended" in record
    assert "Answered case-3 on the operator page." in record


def test_a_cut_wait_loses_nothing_and_a_closed_tab_ends_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    monkeypatch.setenv("WW_OPERATOR_PORT", str(port))
    monkeypatch.setattr(server, "_OPEN_BROWSER_AFTER", 0.1)
    monkeypatch.setattr(server, "_CLOSING_GRACE", 0.2)
    (tmp_path / "ww.yaml").write_text(MANUAL_TESTS, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    _collect(service, "TASK-8", 3)
    stage = service.next("TASK-8")
    execution, _snapshot = service.load("TASK-8")
    assert stage.agent == "claudecode"

    # A wait nobody polls opens the browser and ends by itself.
    opened: list[str] = []
    result = run_operator_page(
        service, "TASK-8", timeout=0.4, open_browser=opened.append
    )
    assert opened == [f"http://127.0.0.1:{port}/"]
    assert (result.outcome, result.applied) == ("timed_out", ())
    assert "The wait passed with nothing new. Nothing was applied." in result.render()

    # Answers left on the sheet by a wait that was killed, including a stale
    # one whose stage was already completed, are applied first on the next
    # wait, and the stale one is dropped.
    sheet = AnswerSheet(service.storage, "TASK-8", execution.created_at)
    sheet.record("01-manual", "case-1", Answer("pass", "", "t1"))
    sheet.record("01-manual", "case-2", Answer("fail", "Hmm.", "t2"))
    service.interact("TASK-8", choice="pass", end=True)
    service.complete("TASK-8", artifact="done by hand", summary_for_next="x")
    result = run_operator_page(service, "TASK-8", timeout=0.3, open_browser=None)
    assert (result.outcome, result.applied) == ("timed_out", ("case-2",))
    assert sheet.read("01-manual") == {}
    execution, _snapshot = service.load("TASK-8")
    assert [r.chosen for r in execution.item_executions if r.chosen] == ["pass", "fail"]

    # A tab that says it is closing ends the wait unless a poll follows.
    waiter = _Waiter(service, "TASK-8")
    _get_state(port)
    assert _post(port, "/closing")[0] == 200
    _get_state(port)  # a reload polls again within the grace, so no end
    time.sleep(0.3)
    assert waiter.thread.is_alive()
    assert _post(port, "/closing")[0] == 200
    result = waiter.join()
    assert (result.outcome, result.applied, result.answered) == ("closed", (), 2)
    assert "The operator closed the page. Nothing was applied." in result.render()
    assert "Do not open the page again on your own" in result.render()


def test_the_operator_page_serves_stages_declared_with_ui_only(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.start("task", "TASK-5", agent="codex", init_artifact="Do it.")
    discuss = service.next("TASK-5")
    assert discuss.ui is False
    assert "#### Operator page" not in (
        MarkdownOutputAdapter().render_instruction(discuss)
    )
    with pytest.raises(StateError, match="serves per-item stages declared with"):
        run_operator_page(service, "TASK-5", timeout=1, open_browser=None)

    # An interactive item stage without ui is a conversation in the session.
    (tmp_path / "ww.yaml").write_text(
        MANUAL_TESTS.replace("interactive: page", "interactive: true"), encoding="utf-8"
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "manual", "TASK-7", agent="codex")
    service.next("TASK-7")
    service.add_item("TASK-7", WorkItem("case-1", "Upload."))
    service.complete("TASK-7", artifact="c", summary_for_next="s")
    stage = service.next("TASK-7")
    assert (stage.item_name, stage.interactive, stage.ui) == (
        "handle-item",
        True,
        False,
    )
    with pytest.raises(StateError, match="serves per-item stages declared with"):
        run_operator_page(service, "TASK-7", timeout=1, open_browser=None)


def test_the_cli_pauses_and_refuses_a_mixed_await(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    service.start("task", "TASK-6", agent="codex", init_artifact="Do it.")
    service.next("TASK-6")
    root = ["--root", str(tmp_path)]
    later = ["interact", "TASK-6", "--operator-said", "Later.", "--pause"]
    assert main([*root, *later]) == 0
    assert "The operator is done for now. Stop here" in capsys.readouterr().out
    assert main([*root, "interact", "TASK-6", "--agent-said", "Noted."]) == 0
    assert "The operator is done for now. Stop here" in capsys.readouterr().out
    assert main([*root, "interact", "TASK-6", "--operator-said", "Back."]) == 0
    assert "Stop here" not in capsys.readouterr().out
    assert main([*root, "interact", "TASK-6", "--await", "--operator-said", "x"]) == 1
    assert "record entries with a separate interact call" in capsys.readouterr().err
    assert main([*root, "interact", "TASK-6", "--pause", "--end"]) == 1
    assert "use one of them" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("agent", "prefix", "wording"),
    [
        ("claudecode", "WW_OPERATOR_WAIT=1800 ", "`run_in_background` option"),
        ("codex", "", "The command blocks until it returns"),
        ("cursor", "", "The command blocks until it returns"),
    ],
)
def test_the_wait_is_run_the_way_the_agent_can(
    tmp_path: Path, agent: str, prefix: str, wording: str
) -> None:
    (tmp_path / "ww.yaml").write_text(MANUAL_TESTS, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    _collect(service, "TASK-9", 1, agent=agent)
    rendered = MarkdownOutputAdapter().render_instruction(service.next("TASK-9"))
    assert f"{prefix}./ww interact TASK-9 --role worker --await" in rendered
    assert wording in rendered


MANUAL_TESTS_WITH_DOCUMENT = """documents:
  - test_cases: The test cases with their results.
workflows:
  - name: manual
    steps:
      - name: collect
        description: Collect the test cases.
        items:
          analyze: Show the test case to the operator.
          interactive: page
          saves:
            - documents.test_cases: Record the result under the case.
          choices:
            - pass: The test case passed.
            - fail: The test case failed; the operator explains why.
"""


def test_a_worker_role_wait_applies_every_answer_and_names_the_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single runtime prints worker-role commands; ``next`` is still the
    manager's, so the session must not pass the worker role to it."""
    port = _free_port()
    monkeypatch.setenv("WW_OPERATOR_PORT", str(port))
    monkeypatch.setattr(server, "_OPEN_BROWSER_AFTER", 0.1)
    (tmp_path / "ww.yaml").write_text(MANUAL_TESTS_WITH_DOCUMENT, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    _collect(service, "TASK-10", 3, agent="codex")
    stage = service.next("TASK-10")
    (document,) = stage.documents
    Path(document.path).parent.mkdir(parents=True, exist_ok=True)
    Path(document.path).write_text("# Cases\n", encoding="utf-8")
    execution, _snapshot = service.load("TASK-10")
    sheet = AnswerSheet(service.storage, "TASK-10", execution.created_at)
    sheet.record("01-manual", "case-1", Answer("pass", "", "t1"))
    sheet.record("01-manual", "case-2", Answer("fail", "No button.", "t2"))
    sheet.record("01-manual", "case-3", Answer("pass", "", "t3"))

    result = run_operator_page(
        service, "TASK-10", timeout=0.2, open_browser=None, caller_role="worker"
    )
    assert result.applied == ("case-1", "case-2", "case-3")
    assert result.documents == (document.path,)
    items = {item.id: item for item in service.items("TASK-10")}
    assert (items["case-1"].actual_solution, items["case-1"].resolved) == (
        "pass",
        True,
    )
    assert items["case-2"].actual_solution == "fail: No button."
    assert items["case-2"].reported is True
    text = result.render()
    assert "its item resolved with the answer as the actual solution" in text
    assert "ww cannot write them: record the operator's answers for case-1, " in text
    assert f"- `{document.path}`" in text
    assert service.instruction("TASK-10").item_name == "update-workflow-summary"
