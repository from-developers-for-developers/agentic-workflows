# SPDX-License-Identifier: GPL-3.0-or-later
"""Run reports: ww's self-assessment and workflow feedback, kept locally.

Two optional modes of ``ww.json`` make the built-in workflow summary ask the
agent for more than the summary: ``debug.collect`` for how ww itself behaved
during the run, ``feedback.collect`` for how well the workflow that ran was
composed. ww writes each answer as one record per run under ``.ww/debug/`` or
``.ww/feedback/``. Nothing leaves the machine by itself: ``ww debug report``
publishes a debug record to ww's GitHub issues only after showing it in full
and asking, and feedback records are read by the operator and the
``ww-workflow-feedback`` skill alone.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import yaml

from ww import __version__, builtin_workflows
from ww.config.composition import compose_configuration
from ww.errors import StateError
from ww.project_config import ProjectConfig
from ww.workflow_config import ProvidedVariable

if TYPE_CHECKING:
    from ww.storage import Storage

RECORD_SCHEMA = 1
GITHUB_REPOSITORY = "from-developers-for-developers/agentic-workflows"
NEW_ISSUE_URL = f"https://github.com/{GITHUB_REPOSITORY}/issues/new"
# GitHub refuses a longer request line; the prefilled body shrinks to fit.
MAX_ISSUE_URL_LENGTH = 8000
GH_TIMEOUT_SECONDS = 60
# What one entry of a report array may carry.
ENTRY_KEYS = frozenset({"summary", "detail", "step"})


@dataclass(frozen=True)
class ReportArray:
    """One of a report's two arrays: the value the summary step provides."""

    variable: str
    field: str
    heading: str
    description: str


@dataclass(frozen=True)
class ReportKind:
    """A collected record kind: where it lives and what it asks for."""

    name: str
    directory: str
    label: str
    title: str
    prompt: str
    arrays: tuple[ReportArray, ReportArray]

    @property
    def variables(self) -> tuple[ProvidedVariable, ...]:
        return tuple(
            ProvidedVariable(array.variable, array.description) for array in self.arrays
        )

    @property
    def path(self) -> str:
        return f".ww/{self.directory}/"


DEBUG = ReportKind(
    "debug",
    "debug",
    "debug info about ww itself",
    "Debug record",
    (
        "Also assess, from this run alone, how ww itself behaved: the tool, "
        "its pages, commands and handlers, not the project's code. Report "
        "errors first; an empty array is a fine answer."
    ),
    (
        ReportArray(
            "debug_errors",
            "errors",
            "Errors, bugs and blockers",
            (
                "A JSON array of the errors, bugs and blockers ww caused during "
                "this run: a command that failed or was refused although it "
                "should have passed, a wrong or misleading page, a crash, a "
                "handler ww mishandled, a stop that needed the operator. Each "
                "entry is an object with `summary` (one sentence), `detail` "
                "(the command, the exact error and what you expected) and "
                "`step` (the step it happened in, or null). `[]` when none."
            ),
        ),
        ReportArray(
            "debug_inconveniences",
            "inconveniences",
            "Inconveniences",
            (
                "A JSON array assessing ww's usability in this run: unclear or "
                "repetitive pages, missing or awkward commands, detours ww "
                "forced. Same entry shape as `debug_errors`. `[]` when none."
            ),
        ),
    ),
)

FEEDBACK = ReportKind(
    "feedback",
    "feedback",
    "feedback about the workflow",
    "Workflow feedback record",
    (
        "Also assess, from this run alone, how well the workflow itself was "
        "composed and how effective it was; ww's own defects do not belong "
        "here."
    ),
    (
        ReportArray(
            "feedback_problems",
            "problems",
            "Application and code problems met during the run",
            (
                "A JSON array of the application or code bugs that surfaced "
                "while the workflow ran (failing builds, flaky tests, broken "
                "tooling), as objects with `summary`, `detail` and `step` (or "
                "null). `[]` when none."
            ),
        ),
        ReportArray(
            "feedback_improvements",
            "improvements",
            "Workflow improvements",
            (
                "A JSON array of how the workflow could be more effective: steps "
                "to add, drop, merge or reorder, prompts to clarify, checks or "
                "mechanical work to automate or script. Same entry shape. `[]` "
                "when none."
            ),
        ),
    ),
)

KINDS: dict[str, ReportKind] = {DEBUG.name: DEBUG, FEEDBACK.name: FEEDBACK}
_VARIABLE_KINDS: dict[str, tuple[ReportKind, ReportArray]] = {
    array.variable: (kind, array) for kind in KINDS.values() for array in kind.arrays
}


def collected_kinds(config: ProjectConfig) -> tuple[ReportKind, ...]:
    """The kinds the project collects, in the order the summary asks for them."""
    kinds: list[ReportKind] = []
    if config.debug.collect:
        kinds.append(DEBUG)
    if config.feedback.collect:
        kinds.append(FEEDBACK)
    return tuple(kinds)


def summary_variables(config: ProjectConfig) -> tuple[ProvidedVariable, ...]:
    """The values the workflow summary provides besides ``summary``."""
    return tuple(
        variable for kind in collected_kinds(config) for variable in kind.variables
    )


def summary_prompt(base: str, config: ProjectConfig) -> str:
    """The summary prompt, extended with what the collected kinds ask for."""
    return " ".join([base, *(kind.prompt for kind in collected_kinds(config))])


def start_notice(config: ProjectConfig) -> str | None:
    """What the first page of a run tells the operator about collection.

    The agent says it once, at the start; the collection itself is silent,
    and the end-of-run summary step carries its questions.
    """
    kinds = collected_kinds(config)
    if not kinds:
        return None
    what = " and ".join(f"{kind.label} (kept under `{kind.path}`)" for kind in kinds)
    return (
        f"Tell the operator once, now, that ww collects {what} at the end of "
        "this run. It stays on this machine only; ww never sends it unless the "
        "operator explicitly reports it. Do not mention the collection again "
        "during the run; the final summary step asks for it."
    )


def validate_report_values(supplied: Mapping[str, str]) -> dict[str, list[dict]]:
    """Parse the report arrays among ``supplied``; refuse a malformed one.

    Returns the parsed arrays by variable name. A value is a JSON array of
    objects with ``summary`` and the optional ``detail`` and ``step``; a bare
    string entry is taken as its summary.
    """
    parsed: dict[str, list[dict]] = {}
    for name, value in supplied.items():
        if name not in _VARIABLE_KINDS:
            continue
        try:
            entries = json.loads(value)
        except json.JSONDecodeError as error:
            raise StateError(f"{name} must be a JSON array: {error}") from error
        if not isinstance(entries, list):
            raise StateError(f"{name} must be a JSON array of objects")
        parsed[name] = [
            _entry(name, index, entry) for index, entry in enumerate(entries)
        ]
    return parsed


def _entry(name: str, index: int, entry: object) -> dict[str, object]:
    if isinstance(entry, str):
        entry = {"summary": entry}
    if not isinstance(entry, dict):
        raise StateError(f"{name}[{index}] must be an object with a summary")
    unknown = set(entry) - ENTRY_KEYS
    if unknown:
        raise StateError(
            f"{name}[{index}] has unknown key(s): " + ", ".join(sorted(unknown))
        )
    summary = entry.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise StateError(f"{name}[{index}].summary must be a non-empty string")
    normalized: dict[str, object] = {"summary": summary.strip()}
    for key in ("detail", "step"):
        value = entry.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise StateError(f"{name}[{index}].{key} must be a string or null")
        if value.strip():
            normalized[key] = value.strip()
    return normalized


def workflow_source(config_path: Path, name: str) -> object | None:
    """The workflow ``name`` as written in the composed ``ww.yaml``.

    This is the definition itself, not the compiled plan: small enough for a
    report. A built-in workflow is read from ww's own files. ``None`` when it
    cannot be found or read; a report is still worth keeping without it.
    """
    try:
        found = _find_workflow(compose_configuration(config_path).raw, name)
    except Exception:  # noqa: BLE001 - any configuration problem leaves it out
        found = None
    if found is not None:
        return found
    try:
        for entry in builtin_workflows.BUILTIN_DIRECTORY.iterdir():
            if not entry.name.endswith(".yaml"):
                continue
            found = _find_workflow(
                yaml.safe_load(entry.read_text(encoding="utf-8")), name
            )
            if found is not None:
                return found
    except Exception:  # noqa: BLE001
        return None
    return None


def _find_workflow(raw: object, name: str) -> object | None:
    if not isinstance(raw, dict):
        return None
    workflows = raw.get("workflows")
    if isinstance(workflows, dict):
        return workflows.get(name)
    if not isinstance(workflows, list):
        return None
    for entry in workflows:
        if not isinstance(entry, dict):
            continue
        if entry.get("name") == name or ("name" not in entry and name in entry):
            return entry
    return None


def record_id(task_id: str, run_id: str) -> str:
    """The record's ID and file stem: a child task's slash becomes a plus."""
    return f"{task_id.replace('/', '+')}--{run_id}"


class RunReportStore:
    """One directory of records of one kind, one JSON file per run."""

    def __init__(self, storage: Storage, kind: ReportKind) -> None:
        self.storage = storage
        self.kind = kind
        self.directory = storage.runtime_path / kind.directory

    def _lock(self) -> AbstractContextManager[None]:
        return self.storage.locks.lock(
            self.directory, purpose=f"{self.kind.name} run reports"
        )

    def record(
        self,
        *,
        task_id: str,
        run_id: str,
        workflow: str,
        agent: str,
        runtime: str | None,
        modes: tuple[str, ...],
        values: Mapping[str, str],
        workflow_definition: object,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        """Write the run's record from the summary's values; none without them."""
        parsed = validate_report_values(
            {
                array.variable: values[array.variable]
                for array in self.kind.arrays
                if array.variable in values
            }
        )
        if len(parsed) != len(self.kind.arrays):
            return None
        # The plan compiler imports this module for the summary step's
        # variables, so the schema versions are read here, not at import.
        from ww.execution_models.runs import PLAN_SCHEMA_VERSION
        from ww.storage_adapters.task_document import TASK_STATE_SCHEMA_VERSION

        record: dict[str, Any] = {
            "schema": RECORD_SCHEMA,
            "id": record_id(task_id, run_id),
            "kind": self.kind.name,
            "workflow": workflow,
            "recorded_at": now or _now(),
            "task_id": task_id,
            "run_id": run_id,
            "agent": agent,
            "runtime": runtime,
            "modes": list(modes),
            "ww_version": __version__,
            "plan_schema_version": PLAN_SCHEMA_VERSION,
            "task_state_schema_version": TASK_STATE_SCHEMA_VERSION,
            "workflow_definition": workflow_definition,
            **{array.field: parsed[array.variable] for array in self.kind.arrays},
            "reported": None,
        }
        with self._lock():
            self._save(record)
        return record

    def records(self) -> list[dict[str, Any]]:
        """Every record, oldest first."""
        if not self.directory.is_dir():
            return []
        found = []
        for path in sorted(self.directory.glob("*.json")):
            found.append(self._read(path))
        found.sort(key=lambda record: str(record.get("recorded_at", "")))
        return found

    def unreported(self) -> list[dict[str, Any]]:
        return [record for record in self.records() if not record.get("reported")]

    def get(self, identifier: str) -> dict[str, Any]:
        path = self.directory / f"{identifier}.json"
        if not path.is_file():
            raise StateError(f"unknown {self.kind.name} record {identifier!r}")
        return self._read(path)

    def mark_reported(
        self, identifier: str, *, method: str, url: str | None, now: str | None = None
    ) -> dict[str, Any]:
        with self._lock():
            record = self.get(identifier)
            record["reported"] = {"at": now or _now(), "method": method, "url": url}
            self._save(record)
        return record

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != RECORD_SCHEMA:
                raise ValueError(f"unsupported {self.kind.name} record schema")
            for array in self.kind.arrays:
                if not isinstance(data.get(array.field), list):
                    raise ValueError(f"{array.field} must be a list")
            data.setdefault("id", path.stem)
            return data
        except (OSError, ValueError) as error:
            raise StateError(
                f"invalid {self.kind.name} record {path}: {error}"
            ) from error

    def _save(self, record: dict[str, Any]) -> None:
        self.storage.locks.atomic_write(
            self.directory / f"{record['id']}.json",
            json.dumps(record, indent=2) + "\n",
        )


def listing(store: RunReportStore) -> dict[str, object]:
    """The store's records with their report state, for ``--json``."""
    records = store.records()
    return {
        "kind": store.kind.name,
        "path": store.kind.path,
        "total": len(records),
        "unreported": sum(1 for record in records if not record.get("reported")),
        "records": records,
    }


def render_listing(store: RunReportStore) -> str:
    records = store.records()
    lines = [f"# {store.kind.title}s", ""]
    if not records:
        lines.append(f"No records under `{store.kind.path}`.")
        return "\n".join(lines) + "\n"
    lines.append(f"Kept under `{store.kind.path}`, oldest first.")
    lines.append("")
    for record in records:
        counts = ", ".join(
            f"{array.field}: {len(record[array.field])}" for array in store.kind.arrays
        )
        reported = record.get("reported")
        state = (
            f"reported {reported.get('at')} via {reported.get('method')}"
            + (f" ({reported['url']})" if reported.get("url") else "")
            if isinstance(reported, dict)
            else "not reported"
        )
        lines.append(
            f"- `{record['id']}` — `{record['workflow']}` at {record['recorded_at']}: "
            f"{counts}; {state}"
        )
    return "\n".join(lines) + "\n"


def render_record(record: Mapping[str, Any], kind: ReportKind) -> str:
    """The record as Markdown: exactly what a report would send."""
    lines = [
        f"# {kind.title}: `{record['workflow']}` at {record['recorded_at']}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Workflow | `{record['workflow']}` |",
        f"| Recorded | {record['recorded_at']} |",
        f"| ww version | {record.get('ww_version')} |",
        f"| Agent | {record.get('agent')} |",
        f"| Runtime | {record.get('runtime') or 'default'} |",
        f"| Modes | {', '.join(record.get('modes') or []) or 'none'} |",
        f"| Plan schema | {record.get('plan_schema_version')} |",
        f"| Task state schema | {record.get('task_state_schema_version')} |",
    ]
    for array in kind.arrays:
        lines.extend(["", f"## {array.heading}", ""])
        entries = record.get(array.field) or []
        if not entries:
            lines.append("None.")
        for entry in entries:
            step = f" (step `{entry['step']}`)" if entry.get("step") else ""
            detail = f": {entry['detail']}" if entry.get("detail") else ""
            lines.append(f"- **{entry['summary']}**{step}{detail}")
    definition = record.get("workflow_definition")
    if definition is not None:
        lines.extend(
            [
                "",
                "<details><summary>Workflow definition</summary>",
                "",
                "```yaml",
                yaml.safe_dump(
                    definition, sort_keys=False, allow_unicode=True
                ).rstrip(),
                "```",
                "",
                "</details>",
            ]
        )
    return "\n".join(lines) + "\n"


def issue_title(record: Mapping[str, Any]) -> str:
    errors = len(record.get(DEBUG.arrays[0].field) or [])
    noun = "error" if errors == 1 else "errors"
    return (
        f"Debug report: `{record['workflow']}` run, ww {record.get('ww_version')}, "
        f"{errors} {noun}"
    )


def issue_body(record: Mapping[str, Any]) -> str:
    return (
        render_record(record, DEBUG)
        + "\n_Collected by ww's `debug.collect` mode and reported by the operator "
        "with `ww debug report`._\n"
    )


def new_issue_url(record: Mapping[str, Any]) -> str:
    """The prefilled new-issue page, shortened until GitHub accepts the URL."""
    title = issue_title(record)
    body = issue_body(record)
    url = _issue_url(title, body)
    if len(url) <= MAX_ISSUE_URL_LENGTH:
        return url
    slim = {**record, "workflow_definition": None}
    body = (
        issue_body(slim)
        + "\n_The workflow definition was left out: too long for a prefilled issue._\n"
    )
    url = _issue_url(title, body)
    if len(url) <= MAX_ISSUE_URL_LENGTH:
        return url
    cut = len(body) - (len(url) - MAX_ISSUE_URL_LENGTH) * 2 - 80
    body = body[: max(cut, 0)] + "\n\n_[truncated: too long for a prefilled issue]_\n"
    return _issue_url(title, body)


def _issue_url(title: str, body: str) -> str:
    return NEW_ISSUE_URL + "?" + urlencode({"title": title, "body": body})


def gh_is_authenticated() -> bool:
    """Whether the ``gh`` CLI is installed and logged in."""
    if shutil.which("gh") is None:
        return False
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def gh_create_issue(title: str, body: str) -> str:
    """Create the issue with ``gh``; returns its URL or raises ``StateError``."""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                GITHUB_REPOSITORY,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            input=body,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise StateError(f"gh issue create failed: {error}") from error
    if result.returncode != 0:
        raise StateError(
            "gh issue create failed: " + (result.stderr.strip() or "no error output")
        )
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


@dataclass(frozen=True)
class ReportOutcome:
    """What happened to one record of a ``debug report`` run."""

    id: str
    status: str  # created | opened | skipped | cancelled
    url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "status": self.status, "url": self.url}


def report_records(
    store: RunReportStore,
    records: list[dict[str, Any]],
    *,
    confirm: Callable[[dict[str, Any], str], bool],
    confirm_submitted: Callable[[str], bool],
    show: Callable[[str], None],
    gh_available: Callable[[], bool] = gh_is_authenticated,
    create_issue: Callable[[str, str], str] = gh_create_issue,
    open_browser: Callable[[str], bool],
) -> list[ReportOutcome]:
    """Publish records one at a time, each shown in full and confirmed.

    With an authenticated ``gh``, the issue is created directly and its URL
    kept on the record. Otherwise the browser opens the new-issue page with
    the title and body prefilled, the operator submits it logged in, and the
    record is marked reported once they confirm they did. A declined
    confirmation leaves the record for a later run.
    """
    outcomes: list[ReportOutcome] = []
    use_gh = gh_available()
    for record in records:
        body = issue_body(record)
        show(
            f"## Report `{record['id']}`\n\nThis will be sent to "
            f"https://github.com/{GITHUB_REPOSITORY}/issues as a new issue titled "
            f"`{issue_title(record)}`, with this body:\n\n{body}"
        )
        if not confirm(record, "gh" if use_gh else "browser"):
            outcomes.append(ReportOutcome(record["id"], "skipped"))
            continue
        if use_gh:
            url = create_issue(issue_title(record), body)
            store.mark_reported(record["id"], method="gh", url=url or None)
            outcomes.append(ReportOutcome(record["id"], "created", url or None))
            continue
        url = new_issue_url(record)
        opened = open_browser(url)
        show(
            ("Opened" if opened else "Could not open a browser; open")
            + " the prefilled new-issue page, review it there and submit it:\n"
            + url
        )
        if confirm_submitted(record["id"]):
            store.mark_reported(record["id"], method="browser", url=None)
            outcomes.append(ReportOutcome(record["id"], "opened"))
        else:
            outcomes.append(ReportOutcome(record["id"], "cancelled"))
    return outcomes


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
