# SPDX-License-Identifier: GPL-3.0-or-later
"""Extensions end to end: compiled into a plan, executed, and inspected."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.actions import Extension, ExtensionAction, actions
from ww.cli import main
from ww.config import load_configuration
from ww.errors import ConfigurationError, StateError
from ww.extensions import ExtensionRegistry
from ww.plan import compile_workflow_plan
from ww.service import WorkflowService
from ww.storage import Storage

RECORDING_EXTENSION = """
import json

from ww.extensions.api import (
    Extension,
    ExtensionCommand,
    ExtensionHandler,
    ExtensionResult,
    ModeDefinition,
    ProvidedVariable,
)


def _record(context):
    note = context.values.get("note", "")
    context.store.append_line(
        "log.jsonl",
        json.dumps({
            "task": context.task_id,
            "run": context.run_id,
            "item": context.item_id,
            "work_item": context.work_item_id,
            "attempt": context.attempt,
            "operation": context.operation_id,
            "note": note,
            "marker": context.config.get("marker"),
        }),
    )
    return ExtensionResult(True, output="recorded " + note)


def _fail(context):
    return ExtensionResult(False, error="deliberate failure")


def _explode(context):
    raise RuntimeError("unhandled")


def _produce(context):
    return ExtensionResult(
        True,
        output="created ticket",
        values={"ticket_url": "https://example.test/TASK-1"},
    )


def _undeclared(context):
    return ExtensionResult(True, values={"surprise": "value"})


def _wrong_result(context):
    return "not an ExtensionResult"


def _log(context):
    lines = context.store.read_lines("log.jsonl")
    return "\\n".join(lines) if lines else "empty"


EXTENSION = Extension(
    vendor="acme",
    name="notes",
    description="Record notes.",
    handlers=(
        ExtensionHandler(
            "record",
            _record,
            "Record a note.",
            provide=(ProvidedVariable("note", "Anything worth writing down."),),
        ),
        ExtensionHandler("fail", _fail, "Always fails."),
        ExtensionHandler("explode", _explode, "Always raises."),
        ExtensionHandler("produce", _produce, outputs=("ticket_url",)),
        ExtensionHandler("undeclared", _undeclared),
        ExtensionHandler("wrong-result", _wrong_result),
    ),
    modes=(ModeDefinition("terse", ("Be brief.",)),),
    commands=(ExtensionCommand("log", _log, "Print the recorded notes."),),
)
"""

CHECKING_EXTENSION = """
from ww.extensions.api import (
    Extension, ExtensionCheckResult, ExtensionHandler, ExtensionResult
)

def _run(context):
    if context.store.read_text("run") == "interrupt":
        raise KeyboardInterrupt
    context.store.write_text("ran", context.operation_id or "missing")
    return ExtensionResult(True, output="ran")

def _check(context):
    mode = context.store.read_text("check")
    if mode == "succeeded":
        return ExtensionCheckResult.succeeded(ExtensionResult(True, output="attested"))
    if mode == "not_succeeded":
        return ExtensionCheckResult.not_succeeded()
    if mode == "exception":
        raise RuntimeError("checker exploded")
    if mode == "invalid":
        return None
    return ExtensionCheckResult.unknown("outcome is unknown")

EXTENSION = Extension(
    vendor="acme", name="checking",
    handlers=(ExtensionHandler("publish", _run, check=_check),),
)
"""

VALIDATING_EXTENSION = """
from ww.extensions.api import (
    Extension, ExtensionHandler, ExtensionResult, ProvidedVariable
)

def _publish(context):
    context.store.write_text("published", context.values["message"])
    return ExtensionResult(True, output="published")

def _check_message(values):
    if values.get("message") == "bad":
        return "message must not be bad"
    return None

EXTENSION = Extension(
    vendor="acme", name="checked",
    handlers=(
        ExtensionHandler(
            "publish", _publish,
            provide=(ProvidedVariable("message", "What to publish."),),
            validate=_check_message,
        ),
    ),
)
"""

ALTERNATE_BINDING_EXTENSION = """
from ww.extensions.api import (
    Extension, ExtensionHandler, ExtensionResult, ExtensionVariable,
)

def _workspace(context):
    return str(context.root / "alternate-workspace")

def _record(context):
    context.store.write_text("workspace", context.values["__task_workspace_dir"])
    return ExtensionResult(True)

EXTENSION = Extension(
    vendor="acme",
    name="binding",
    handlers=(ExtensionHandler("record", _record),),
    variables=(ExtensionVariable("__task_workspace_dir", _workspace),),
)
"""


class AlternateExtensionAction(ExtensionAction):
    identifier = "test_extension_binding"

    def parse(
        self, source: dict[str, object], name: str, description: str, path: str
    ) -> Extension:
        del name, description, path
        reference = source.get("reference")
        if set(source) != {"reference"} or not isinstance(reference, str):
            raise ConfigurationError("alternate extension action requires a reference")
        return Extension(reference)


def _extension(root: Path) -> None:
    directory = root / "ext" / "acme" / "notes"
    directory.mkdir(parents=True)
    (directory / "extension.py").write_text(RECORDING_EXTENSION, encoding="utf-8")


def _project(root: Path, handler: str = "record") -> None:
    _extension(root)
    (root / "ww-agentic-workflows.yaml").write_text(
        f"""workflows:
  - name: task
    modes:
      - ext/acme/notes/modes:terse
    steps:
      - name: work
        prompt: true
        hooks:
          after_complete:
            - name: ext/acme/notes/handlers:{handler}
""",
        encoding="utf-8",
    )


def _service(root: Path) -> WorkflowService:
    return WorkflowService(Storage(root), extensions=ExtensionRegistry.discover(root))


def _checking_service(root: Path) -> WorkflowService:
    directory = root / "ext" / "acme" / "checking"
    directory.mkdir(parents=True)
    (directory / "extension.py").write_text(CHECKING_EXTENSION, encoding="utf-8")
    (root / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        prompt: true
        hooks:
          after_complete:
            - name: ext/acme/checking/handlers:publish
""",
        encoding="utf-8",
    )
    return WorkflowService(Storage(root), extensions=ExtensionRegistry.discover(root))


def _validating_service(root: Path) -> WorkflowService:
    directory = root / "ext" / "acme" / "checked"
    directory.mkdir(parents=True)
    (directory / "extension.py").write_text(VALIDATING_EXTENSION, encoding="utf-8")
    (root / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        prompt: true
        hooks:
          after_complete:
            - name: ext/acme/checked/handlers:publish
""",
        encoding="utf-8",
    )
    return WorkflowService(Storage(root), extensions=ExtensionRegistry.discover(root))


def test_a_refused_provided_value_fails_the_completion_and_saves_nothing(
    tmp_path: Path,
) -> None:
    service = _validating_service(tmp_path)
    start_after_init(service, "task", "TASK-CHECKED", agent="codex")
    service.next("TASK-CHECKED")

    with pytest.raises(StateError, match="publish rejected the supplied value"):
        service.complete(
            "TASK-CHECKED",
            (("message", "bad"),),
            "# work\n",
            summary_for_next="Done.",
        )

    state = service.tasks.read_execution_state("TASK-CHECKED", "01-task")
    assert state is not None
    # The agent step is still open, nothing of the refused completion landed.
    assert state.status == "in_progress"
    assert state.item_executions[1].status == "in_progress"
    assert state.item_executions[1].artifact is None
    assert "message" not in dict(state.workflow_values)
    assert not (tmp_path / ".ww/ext/acme/checked/published").exists()

    accepted = service.complete(
        "TASK-CHECKED",
        (("message", "good"),),
        "# work\n",
        summary_for_next="Done.",
    )

    assert accepted.status != "failed"
    assert (tmp_path / ".ww/ext/acme/checked/published").read_text() == "good"


def test_an_extension_handler_compiles_as_automatic_ww_work(tmp_path: Path) -> None:
    _project(tmp_path)
    service = _service(tmp_path)

    start_after_init(service, "task", "TASK-1", agent="codex")
    snapshot = service.tasks.read_plan_snapshot("TASK-1", "01-task")

    assert snapshot is not None
    item = snapshot.plan.items[2]
    assert (item.name, item.kind, item.owner) == ("record", "extension", "ww")
    assert item.execution == "automatic"
    extension = item.payload_as(Extension)
    assert extension.reference == "ext/acme/notes/handlers:record"
    assert extension.version == "0"
    assert extension.api_version == 1
    assert extension.source == "project:ext/acme/notes/extension.py"
    assert extension.fingerprint is not None
    assert extension.fingerprint.startswith("sha256:")
    assert extension.settings == {}
    # Declared inputs travel the same path a CLI handler's do.
    assert item.requires_agent_input
    assert [value.name for value in item.provide] == ["note"]


def test_an_extension_handler_runs_and_keeps_its_own_state(tmp_path: Path) -> None:
    _project(tmp_path)
    service = _service(tmp_path)

    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    ready = service.complete(
        "TASK-1",
        (("note", "hello"),),
        "# work\n",
        summary_for_next="Done.",
    )

    assert ready.status != "failed"
    state = service.tasks.read_execution_state("TASK-1", "01-task")
    assert state is not None
    assert state.item_executions[2].status == "completed"
    assert state.item_executions[2].result == "recorded hello"

    store = tmp_path / ".ww/ext/acme/notes/log.jsonl"
    assert json.loads(store.read_text(encoding="utf-8")) == {
        "task": "TASK-1",
        "run": "01-task",
        "item": "task:work:after_complete:step:1",
        "work_item": None,
        "attempt": 1,
        "operation": state.item_executions[2].operation_id,
        "note": "hello",
        "marker": None,
    }


def test_a_run_uses_the_extension_settings_frozen_in_its_plan(tmp_path: Path) -> None:
    _project(tmp_path)
    config = tmp_path / "ww-agentic-workflows.json"
    config.write_text(
        json.dumps({"extensions": {"acme/notes": {"marker": "original"}}}),
        encoding="utf-8",
    )
    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-SETTINGS", agent="codex")

    config.write_text(
        json.dumps({"extensions": {"acme/notes": {"marker": "changed"}}}),
        encoding="utf-8",
    )
    resumed = _service(tmp_path)
    resumed.next("TASK-SETTINGS")
    resumed.complete(
        "TASK-SETTINGS",
        (("note", "hello"),),
        "# work\n",
        summary_for_next="Done.",
    )

    recorded = json.loads(
        (tmp_path / ".ww/ext/acme/notes/log.jsonl").read_text(encoding="utf-8")
    )
    assert recorded["marker"] == "original"


def test_dispatch_accepts_an_in_place_fix_of_the_same_extension_version(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-FIX", agent="codex")
    extension_path = tmp_path / "ext/acme/notes/extension.py"
    extension_path.write_text(
        extension_path.read_text(encoding="utf-8") + "\n# fixed after compile\n",
        encoding="utf-8",
    )

    resumed = _service(tmp_path)
    resumed.next("TASK-FIX")
    finished = resumed.complete(
        "TASK-FIX",
        (("note", "hello"),),
        "# work\n",
        summary_for_next="Done.",
    )

    assert finished.error is None
    assert (tmp_path / ".ww/ext/acme/notes/log.jsonl").is_file()


def test_dispatch_rejects_extension_version_drift_from_the_saved_plan(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-DRIFT", agent="codex")
    extension_path = tmp_path / "ext/acme/notes/extension.py"
    extension_path.write_text(
        extension_path.read_text(encoding="utf-8").replace(
            'name="notes",', 'name="notes",\n    version="2",'
        ),
        encoding="utf-8",
    )

    resumed = _service(tmp_path)
    resumed.next("TASK-DRIFT")
    with pytest.raises(
        ConfigurationError, match="expected version '0' .* found version '2'"
    ):
        resumed.complete(
            "TASK-DRIFT",
            (("note", "hello"),),
            "# work\n",
            summary_for_next="Done.",
        )

    state = resumed.tasks.read_execution_state("TASK-DRIFT", "01-task")
    assert state is not None
    extension_index = next(
        index
        for index, item in enumerate(
            resumed.tasks.read_task_record("TASK-DRIFT")[0][0].snapshot.plan.items
        )
        if item.kind == "extension"
    )
    assert state.item_executions[extension_index].status == "pending"

    extension_path.write_text(RECORDING_EXTENSION, encoding="utf-8")
    recovered = _service(tmp_path).next("TASK-DRIFT")
    assert recovered.item_name == "update-workflow-summary"


def test_alternate_extension_action_applies_frozen_variable_bindings(
    tmp_path: Path,
) -> None:
    actions.register(AlternateExtensionAction())
    try:
        directory = tmp_path / "ext/acme/binding"
        directory.mkdir(parents=True)
        (directory / "extension.py").write_text(
            ALTERNATE_BINDING_EXTENSION, encoding="utf-8"
        )
        (tmp_path / "ww-agentic-workflows.yaml").write_text(
            """handlers:
  - name: record-binding
    action:
      type: test_extension_binding
      reference: ext/acme/binding/handlers:record
workflows:
  - name: task
    steps:
      - name: work
        prompt: true
        hooks:
          after_complete:
            - name: record-binding
""",
            encoding="utf-8",
        )
        service = WorkflowService(
            Storage(tmp_path), extensions=ExtensionRegistry.discover(tmp_path)
        )
        start_after_init(service, "task", "TASK-BINDING", agent="codex")
        service.next("TASK-BINDING")
        service.complete("TASK-BINDING", artifact="work", summary_for_next="Done.")

        assert (tmp_path / ".ww/ext/acme/binding/workspace").read_text() == str(
            tmp_path / "alternate-workspace"
        )
    finally:
        actions.unregister("test_extension_binding")


def test_cli_state_commands_ignore_an_unused_broken_extension(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        prompt: true
""",
        encoding="utf-8",
    )
    broken = tmp_path / "ext/acme/broken/extension.py"
    broken.parent.mkdir(parents=True)
    broken.write_text("raise RuntimeError('unused import')\n", encoding="utf-8")

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "start",
                "TASK-LAZY",
                "--workflow",
                "task",
                "--agent",
                "codex",
                "--init-artifact",
                "requirements",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "status", "TASK-LAZY"]) == 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "reset", "TASK-LAZY", "--yes"]) == 0


def test_a_failing_extension_handler_fails_the_item(tmp_path: Path) -> None:
    _project(tmp_path, handler="fail")
    service = _service(tmp_path)

    start_after_init(service, "task", "TASK-2", agent="codex")
    service.next("TASK-2")
    result = service.complete("TASK-2", artifact="# work\n", summary_for_next="Done.")

    assert result.status == "failed"
    assert "deliberate failure" in (result.error or "")


def test_an_extension_that_raises_is_contained(tmp_path: Path) -> None:
    _project(tmp_path, handler="explode")
    service = _service(tmp_path)

    start_after_init(service, "task", "TASK-3", agent="codex")
    service.next("TASK-3")
    result = service.complete("TASK-3", artifact="# work\n", summary_for_next="Done.")

    # A third party's exception is a failed step, not a crashed ww.
    assert result.status == "failed"
    assert "RuntimeError: unhandled" in (result.error or "")


def test_declared_extension_outputs_become_durable_workflow_values(
    tmp_path: Path,
) -> None:
    _project(tmp_path, handler="produce")
    service = _service(tmp_path)

    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    service.complete("TASK-1", artifact="# work\n", summary_for_next="Done.")

    state = service.tasks.read_execution_state("TASK-1", "01-task")
    assert state is not None
    assert dict(state.workflow_values)["ticket_url"] == ("https://example.test/TASK-1")
    assert dict(state.item_executions[2].output_values) == {
        "ticket_url": "https://example.test/TASK-1"
    }


def test_a_later_step_can_use_a_declared_extension_output(tmp_path: Path) -> None:
    _extension(tmp_path)
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: create
        prompt: true
        artifact: false
        hooks:
          after_complete:
            - name: ext/acme/notes/handlers:produce
      - name: review
        description: Review {{ticket_url}}.
        artifact: false
""",
        encoding="utf-8",
    )
    service = _service(tmp_path)

    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    service.complete("TASK-1", summary_for_next="Done.")
    ready = service.next("TASK-1")

    assert ready.item_name == "review"
    assert ready.action_text == "Review https://example.test/TASK-1."


@pytest.mark.parametrize("handler", ["undeclared", "wrong-result"])
def test_invalid_extension_results_fail_consistently(
    tmp_path: Path, handler: str
) -> None:
    _project(tmp_path, handler=handler)
    service = _service(tmp_path)

    start_after_init(service, "task", "TASK-BAD", agent="codex")
    service.next("TASK-BAD")
    result = service.complete("TASK-BAD", artifact="# work\n", summary_for_next="Done.")

    assert result.status == "failed"
    assert "invalid result" in (result.error or "")


@pytest.mark.parametrize(
    "mode", ["succeeded", "not_succeeded", "unknown", "exception", "invalid"]
)
def test_default_recovery_uses_extension_checker_tri_state(
    tmp_path: Path, mode: str
) -> None:
    service = _checking_service(tmp_path)
    store = service.extensions.store("acme/checking")
    store.write_text("check", mode)
    store.write_text("run", "interrupt")
    start_after_init(service, "task", "TASK-CHECK", agent="codex")
    service.next("TASK-CHECK")
    with pytest.raises(KeyboardInterrupt):
        service.complete("TASK-CHECK", artifact="# work\n", summary_for_next="Done.")

    store.write_text("run", "done")
    service.next("TASK-CHECK")
    recovered = service.recover("TASK-CHECK")
    if mode == "succeeded":
        assert recovered.item_name == "update-workflow-summary"
        assert store.read_text("ran") is None
    elif mode == "not_succeeded":
        assert recovered.item_name == "update-workflow-summary"
        assert store.read_text("ran") is not None
    else:
        assert recovered.status == "interrupted"
        assert mode in {"unknown", "exception", "invalid"}
        assert "checker" in (recovered.error or "") or "unknown" in (
            recovered.error or ""
        )


def test_an_extension_mode_is_usable_by_a_workflow(tmp_path: Path) -> None:
    _project(tmp_path)

    assert main(["--root", str(tmp_path), "modes"]) == 0


def test_an_extension_mode_can_be_selected_without_loading_other_extensions(
    tmp_path: Path,
) -> None:
    _extension(tmp_path)
    broken = tmp_path / "ext/other/broken/extension.py"
    broken.parent.mkdir(parents=True)
    broken.write_text("raise RuntimeError('unused')\n", encoding="utf-8")
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        prompt: true
""",
        encoding="utf-8",
    )
    service = _service(tmp_path)

    start_after_init(
        service, "task", "TASK-MODE", ("ext/acme/notes/modes:terse",), agent="codex"
    )

    state = service.tasks.read_execution_state("TASK-MODE", "01-task")
    assert state is not None
    assert state.modes == ("ext/acme/notes/modes:terse",)


def test_an_empty_registry_cannot_resolve_a_reference(tmp_path: Path) -> None:
    _project(tmp_path)
    service = WorkflowService(Storage(tmp_path), extensions=ExtensionRegistry(tmp_path))

    with pytest.raises(ConfigurationError) as error:
        start_after_init(service, "task", "TASK-4", agent="codex")

    assert "ext/acme/notes/modes:terse" in str(error.value)


def test_compiling_with_no_registry_at_all_reports_the_reference(
    tmp_path: Path,
) -> None:
    _extension(tmp_path)
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        prompt: true
        hooks:
          after_complete:
            - name: ext/acme/notes/handlers:record
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as error:
        compile_workflow_plan(
            load_configuration(tmp_path / "ww-agentic-workflows.yaml"),
            tmp_path,
            "task",
            "codex",
        )

    assert "no extensions are loaded" in str(error.value)


def test_referencing_an_absent_handler_fails_before_the_task_exists(
    tmp_path: Path,
) -> None:
    _project(tmp_path, handler="absent")
    service = _service(tmp_path)

    with pytest.raises(ConfigurationError) as error:
        start_after_init(service, "task", "TASK-5", agent="codex")

    assert "has no handler 'absent'" in str(error.value)
    assert not (tmp_path / ".ww/tasks/TASK-5").exists()


def test_the_cli_lists_extensions_and_runs_their_commands(
    tmp_path: Path, capsys
) -> None:
    _project(tmp_path)
    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-6", agent="codex")
    service.next("TASK-6")
    service.complete(
        "TASK-6",
        (("note", "written"),),
        "# work\n",
        summary_for_next="Done.",
    )

    assert main(["--root", str(tmp_path), "extensions"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert catalog["extensions"][0]["id"] == "acme/notes"
    assert catalog["extensions"][0]["handlers"][0]["reference"] == (
        "ext/acme/notes/handlers:record"
    )
    produce = next(
        handler
        for handler in catalog["extensions"][0]["handlers"]
        if handler["reference"].endswith(":produce")
    )
    assert produce["outputs"] == ["ticket_url"]
    assert catalog["extensions"][0]["commands"][0]["usage"] == (
        "./ww extension acme/notes log"
    )

    assert main(["--root", str(tmp_path), "extension", "acme/notes", "log"]) == 0
    assert '"note": "written"' in capsys.readouterr().out


def test_the_cli_contains_an_invalid_extension_command_result(
    tmp_path: Path, capsys
) -> None:
    _extension(tmp_path)
    path = tmp_path / "ext/acme/notes/extension.py"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'return "\\n".join(lines) if lines else "empty"', "return 7"
        ),
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "extension", "acme/notes", "log"]) == 1
    assert "non-string result" in capsys.readouterr().err


def test_the_git_extension_commits_and_records_what_it_committed(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / ".gitignore").write_text(".ww/\ntasks/\n", encoding="utf-8")
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    hooks:
      before_start_workflow:
        - name: ext/ww/git/handlers:is-git-clean
    steps:
      - name: work
        prompt: true
        hooks:
          after_complete:
            - name: ext/ww/git/handlers:git-commit
""",
        encoding="utf-8",
    )
    for command in (
        ("git", "init", "-q", "."),
        ("git", "config", "user.email", "t@e.st"),
        ("git", "config", "user.name", "Test"),
        ("git", "add", "-A"),
        ("git", "commit", "-qm", "seed"),
    ):
        subprocess.run(command, cwd=tmp_path, check=True, capture_output=True)

    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-7", agent="codex")
    (tmp_path / "new.txt").write_text("content\n", encoding="utf-8")
    service.next("TASK-7")
    service.complete(
        "TASK-7",
        (("commit_message", "feat(demo): add a file"),),
        "# work\n",
        summary_for_next="Done.",
    )

    log = subprocess.run(
        ("git", "log", "--format=%s"),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    # The default commit_format prefixes the task, so git log alone still
    # attributes a commit even though the store is gitignored.
    assert log.stdout.splitlines()[0] == "TASK-7: feat(demo): add a file"

    recorded = json.loads(
        (tmp_path / ".ww/ext/ww/git/commits.jsonl").read_text(encoding="utf-8")
    )
    assert recorded["task_id"] == "TASK-7"
    assert recorded["run_id"] == "01-task"
    assert recorded["message"] == "TASK-7: feat(demo): add a file"
    assert len(recorded["sha"]) == 40

    assert main(["--root", str(tmp_path), "extension", "ww/git", "commits"]) == 0
    assert "feat(demo): add a file" in capsys.readouterr().out
    assert (
        main(["--root", str(tmp_path), "extension", "ww/git", "commits", "TASK-9"]) == 0
    )
    assert "No commits recorded for TASK-9." in capsys.readouterr().out


def test_git_start_branch_hook_keeps_first_declared_step_filter(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(".ww/\ntasks/\n", encoding="utf-8")
    (tmp_path / "ww-agentic-workflows.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "ww/git": {
                        "base_branches": {"default": "main"},
                        "use_separate_branch": True,
                        "branch_name_formats": {"default": "feature/{{task_id}}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """hooks:
  before_start_workflow:
    - ext/ww/git/handlers:start-task-branch: ~
workflows:
  - name: task
    steps:
      - name: develop
        prompt: true
""",
        encoding="utf-8",
    )
    for command in (
        ("git", "init", "-q", "-b", "main", "."),
        ("git", "config", "user.email", "t@e.st"),
        ("git", "config", "user.name", "Test"),
        ("git", "add", "-A"),
        ("git", "commit", "-qm", "seed"),
    ):
        subprocess.run(command, cwd=tmp_path, check=True, capture_output=True)

    result = _service(tmp_path).start("task", "TASK-LEGACY", agent="codex")

    assert result.status == "pending"
    snapshot = _service(tmp_path).tasks.read_plan_snapshot("TASK-LEGACY", "01-task")
    assert snapshot is not None
    hook, init = snapshot.plan.items[:2]
    assert (
        hook.payload_as(Extension).reference == "ext/ww/git/handlers:start-task-branch"
    )
    assert hook.payload_as(Extension).settings == {
        "base_branches": {"default": "main"},
        "use_separate_branch": True,
        "branch_name_formats": {"default": "feature/{{task_id}}"},
    }
    assert (hook.phase, hook.step) == ("before_start_workflow", "init")
    assert init.name == "init"
    branch = subprocess.run(
        ("git", "branch", "--show-current"),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch.stdout.strip() == "feature/task-legacy"


def test_start_branch_strategy_is_persisted_for_extension_hooks(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(".ww/\ntasks/\n", encoding="utf-8")
    (tmp_path / "ww-agentic-workflows.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "ww/git": {
                        "base_branches": {"default": "main"},
                        "use_separate_branch": True,
                        "branch_name_formats": {
                            "default": "feature/{{task_id}}",
                            "experiment": "experiment/{{task_id}}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """hooks:
  before_start_workflow:
    - ext/ww/git/handlers:start-task-branch: ~
workflows:
  - name: task
    steps:
      - name: develop
        prompt: true
""",
        encoding="utf-8",
    )
    for command in (
        ("git", "init", "-q", "-b", "main", "."),
        ("git", "config", "user.email", "t@e.st"),
        ("git", "config", "user.name", "Test"),
        ("git", "add", "-A"),
        ("git", "commit", "-qm", "seed"),
    ):
        subprocess.run(command, cwd=tmp_path, check=True, capture_output=True)

    service = _service(tmp_path)
    service.start(
        "task", "TASK-STRATEGY", agent="codex", branch_naming_strategy="experiment"
    )

    state = service.tasks.read_execution_state("TASK-STRATEGY", "01-task")
    assert state is not None
    assert dict(state.workflow_values)["__branch_naming_strategy"] == "experiment"
    branch = subprocess.run(
        ("git", "branch", "--show-current"),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch.stdout.strip() == "experiment/task-strategy"


def test_develop_preparation_hooks_select_worktree_after_earlier_steps(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitignore").write_text(".ww/\ntasks/\ntrees/\n", encoding="utf-8")
    (tmp_path / "ww-agentic-workflows.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "ww/git": {
                        "base_branches": {"default": "main"},
                        "worktrees": True,
                        "worktree_dir": str(tmp_path / "trees"),
                        "worktree_name_format": "{{task_id}}",
                        "branch_name_formats": {"default": "feature/{{task_id}}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """hooks:
  before_in_progress:
    - steps: [develop]
      handlers:
        - ext/ww/git/handlers:start-task-branch: ~
        - ext/ww/git/handlers:create-worktree: ~
  before_complete:
    - steps: [code-review]
      ext/ww/git/handlers:git-commit: ~
workflows:
  - name: task
    steps:
      - name: fetch
        prompt: true
      - name: develop
        prompt: true
      - name: code-review
        prompt: true
""",
        encoding="utf-8",
    )
    for command in (
        ("git", "init", "-q", "-b", "main", "."),
        ("git", "config", "user.email", "t@e.st"),
        ("git", "config", "user.name", "Test"),
        ("git", "add", "-A"),
        ("git", "commit", "-qm", "seed"),
    ):
        subprocess.run(command, cwd=tmp_path, check=True, capture_output=True)

    service = _service(tmp_path)
    ready = start_after_init(service, "task", "TASK-WORKTREE", agent="codex")
    worktree = tmp_path / "trees/TASK-WORKTREE"

    assert ready.item_name == "fetch"
    assert ready.working_directory is None
    service.next("TASK-WORKTREE")
    ready = service.complete(
        "TASK-WORKTREE",
        artifact="# fetched\n",
        summary_for_next="Done.",
    )
    # In the single runtime the completion opens the next step itself, so
    # its preparation hooks have already selected the worktree.
    assert ready.item_name == "develop"
    assert ready.working_directory == str(worktree)
    active = service.next("TASK-WORKTREE")
    assert active.working_directory == str(worktree)
    state = service.tasks.read_execution_state("TASK-WORKTREE", "01-task")
    assert state is not None
    assert state.working_directory == "trees/TASK-WORKTREE"
    # The same state opened from a checkout mounted elsewhere, for example
    # inside a container, prints the worktree for that filesystem.
    moved = tmp_path.parent / f"{tmp_path.name}-moved"
    shutil.copytree(tmp_path, moved, symlinks=True)
    elsewhere = WorkflowService(Storage(moved)).status("TASK-WORKTREE")
    assert elsewhere.working_directory == str(moved / "trees/TASK-WORKTREE")
    ready = service.complete(
        "TASK-WORKTREE",
        artifact="# developed\n",
        summary_for_next="Done.",
    )
    assert ready.item_name == "code-review"
    (worktree / "work.txt").write_text("work\n", encoding="utf-8")
    service.next("TASK-WORKTREE")
    ready = service.complete(
        "TASK-WORKTREE",
        (("commit_message", "commit from the selected worktree"),),
        "# reviewed\n",
        summary_for_next="Done.",
    )

    assert ready.item_name == "update-workflow-summary"
    subject = subprocess.run(
        ("git", "log", "-1", "--format=%s"),
        cwd=worktree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject == "TASK-WORKTREE: commit from the selected worktree"
    assert not (tmp_path / "work.txt").exists()
    assert (
        subprocess.run(
            ("git", "status", "--short"),
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


def test_the_git_extension_refuses_a_dirty_tree(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    hooks:
      before_start_workflow:
        - name: ext/ww/git/handlers:is-git-clean
    steps:
      - name: work
        prompt: true
""",
        encoding="utf-8",
    )
    subprocess.run(("git", "init", "-q", "."), cwd=tmp_path, check=True)

    result = _service(tmp_path).start("task", "TASK-8", agent="codex")

    assert result.status == "failed"
    assert "uncommitted change" in (result.error or "")
