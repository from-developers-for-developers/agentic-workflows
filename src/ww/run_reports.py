# SPDX-License-Identifier: GPL-3.0-or-later
"""Run reports: ww's self-assessment and workflow feedback, kept locally.

Two optional modes of ``ww.json`` ask the agent for an assessment at the end
of every run: ``debug.collect`` for how ww itself behaved, asked by a
ww-generated item the session that drove the run performs, and
``feedback.collect`` for how well the workflow that ran was composed, asked
by the built-in workflow summary. ww writes each answer as one record per run
under ``.ww/debug/`` or ``.ww/feedback/``. A debug record holds ww-related
data only: every string that reaches it is redacted, the task is named by a
digest, and the workflow appears as its structural shape. Nothing leaves the
machine by itself: ``ww debug report`` publishes a debug record to ww's
GitHub issues only after showing it in full and asking, and feedback records
are read by the operator and the ``ww-workflow-feedback`` skill alone.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from ww import __version__
from ww.actions import DefinedAction, Prompt
from ww.errors import StateError
from ww.project_config import ProjectConfig, load_project_config
from ww.variables import task_slug
from ww.workflow_config import HandlerDefinition, ProvidedVariable

if TYPE_CHECKING:
    from ww.plan.models import PlanItem, WorkflowPlan
    from ww.storage import Storage

RECORD_SCHEMA = 1
GITHUB_REPOSITORY = "from-developers-for-developers/agentic-workflows"
NEW_ISSUE_URL = f"https://github.com/{GITHUB_REPOSITORY}/issues/new"
# GitHub refuses a longer request line; the prefilled body shrinks to fit.
MAX_ISSUE_URL_LENGTH = 8000
GH_TIMEOUT_SECONDS = 60
# What one entry of a report array may carry.
ENTRY_KEYS = frozenset({"summary", "detail", "step"})
# Who recorded an event: ww observing the run, or the operator noting it.
EVENT_SOURCES = ("ww", "operator")
# Events recorded before a run's record exists wait here, one file per task
# or request, until the run completes or the operator notes them standalone.
PENDING_DIRECTORY = "pending"
# The run ID of a standalone record holding a task's or request's events.
NOTES_RUN_ID = "notes"
# How many hex digits of the task ID's SHA-256 name a record.
TASK_DIGEST_LENGTH = 12
# What ww-related data a record may say instead of the original words.
NO_PROJECT_DATA_RULE = (
    "Describe ww's behaviour only: name no project, path, ticket key, "
    "commit message, code or person; ww redacts what slips through."
)


@dataclass(frozen=True)
class ReportArray:
    """One of a report's two arrays: the value the agent provides."""

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
    # Whether records of this kind carry the events ww observed and the
    # operator noted, apart from the agent-written arrays.
    events: bool = False
    # The ww-generated plan item that asks for the arrays, performed by the
    # session that drove the run; ``None`` leaves them to the summary step.
    item: str | None = None

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
        "Assess, from this run alone, how ww itself behaved: the tool, its "
        "pages, commands and handlers, not the project's code. Answer each "
        "question below for the whole run; an empty array is a fine answer.\n"
        "- Which ww commands errored or were refused although they should "
        "have passed?\n"
        "- Which pages misled you or lacked a command you needed?\n"
        "- Which steps did you retry, and which did the operator force past?\n"
        "- Did you edit ww's state by hand, or work around ww in another way?\n"
        "- Which rounds of the run were wasted on ww rather than the work?\n"
        + NO_PROJECT_DATA_RULE
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
                "(the ww command, the exact ww error and what you expected) and "
                "`step` (the step it happened in, or null). Describe ww's "
                "behaviour only: no project name, path, ticket key, code, commit "
                "message or user name. `[]` when none."
            ),
        ),
        ReportArray(
            "debug_inconveniences",
            "inconveniences",
            "Inconveniences",
            (
                "A JSON array assessing ww's usability in this run: unclear or "
                "repetitive pages, missing or awkward commands, detours ww "
                "forced. Same entry shape and the same rule as `debug_errors`: "
                "ww's behaviour only, no project data. `[]` when none."
            ),
        ),
    ),
    events=True,
    item="assess-ww",
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
    """The kinds the project collects, in the order the run asks for them."""
    kinds: list[ReportKind] = []
    if config.debug.collect:
        kinds.append(DEBUG)
    if config.feedback.collect:
        kinds.append(FEEDBACK)
    return tuple(kinds)


def _summary_kinds(config: ProjectConfig) -> tuple[ReportKind, ...]:
    return tuple(kind for kind in collected_kinds(config) if kind.item is None)


def summary_variables(config: ProjectConfig) -> tuple[ProvidedVariable, ...]:
    """The values the workflow summary provides besides ``summary``."""
    return tuple(
        variable for kind in _summary_kinds(config) for variable in kind.variables
    )


def summary_prompt(base: str, config: ProjectConfig) -> str:
    """The summary prompt, extended with what the summary kinds ask for."""
    return " ".join([base, *(kind.prompt for kind in _summary_kinds(config))])


def debug_item(config: ProjectConfig) -> HandlerDefinition | None:
    """The item that asks for the debug assessment, when it is collected.

    It is compiled right before the workflow summary and performed by the
    session that drove the run, the manager under ``auto``, so the answers
    come from whoever met ww's pages and commands. Its page lists the
    events ww observed itself (``events_notice``) so the agent adds to them.
    """
    if DEBUG not in collected_kinds(config):
        return None
    assert DEBUG.item is not None
    return HandlerDefinition(
        DEBUG.item,
        description="Assess how ww itself behaved during this run.",
        action=DefinedAction("prompt", Prompt(DEBUG.prompt)),
        provide=DEBUG.variables,
    )


def asks_for_reports(item: PlanItem) -> bool:
    """Whether completing ``item`` supplies report arrays to validate."""
    return item.summary or item.name == DEBUG.item


def start_notice(config: ProjectConfig) -> str | None:
    """What the first page of a run tells the operator about collection.

    The agent says it once, at the start; the collection itself is silent
    until the end of the run asks its questions.
    """
    kinds = collected_kinds(config)
    if not kinds:
        return None
    what = " and ".join(f"{kind.label} (kept under `{kind.path}`)" for kind in kinds)
    return (
        f"Tell the operator once, now, that ww collects {what} at the end of "
        "this run. It stays on this machine only; ww never sends it unless the "
        "operator explicitly reports it. Do not mention the collection again "
        "during the run; ww asks for it at the end."
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


def workflow_shape(plan: WorkflowPlan) -> dict[str, Any]:
    """The plan's structure, without any text the project wrote.

    Each item is its name, step, phase, source, kind, owner and role, with
    the handler it registered and its items and children markers; the
    counts come first. Descriptions, prompts, rules, checks and artifacts
    stay out.
    """
    items = [_item_shape(item) for item in plan.items]
    return {
        "workflow": plan.workflow,
        "handoff": plan.handoff,
        "counts": {
            "items": len(items),
            "steps": sum(1 for item in plan.items if item.phase == "step"),
            "hooks": sum(1 for item in plan.items if item.phase != "step"),
        },
        "items": items,
    }


def _item_shape(item: PlanItem) -> dict[str, Any]:
    shape: dict[str, Any] = {
        "name": item.name,
        "step": item.step,
        "phase": item.phase,
        "source": item.source,
        "kind": item.kind,
        "owner": item.owner,
        "role": item.role,
    }
    markers: dict[str, Any] = {
        "handler": item.registered_handler,
        "items": item.item_operation,
        "children": item.child_operation or item.child_stage,
        "interactive": item.interactive or None,
        "rules": len(item.rules) or None,
        "checks": len(item.checks) or None,
    }
    shape.update({key: value for key, value in markers.items() if value})
    return shape


# Redaction: what a record may not keep of the project, the task or a person.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_QUOTED = re.compile(r"`[^`\n]*`|\"[^\"\n]*\"|(?<!\w)'[^'\n]*'(?!\w)")
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
# A path is a token with a slash in it or ending in a known extension; the
# punctuation around it stays, as does ww's own launcher.
_TOKEN = re.compile(r"[^\s'\"`()\[\]{}<>,;]+")
_TRAILING_PUNCTUATION = re.compile(r"[.,;:!?]+$")
_PATH_EXTENSIONS = (
    ".py", ".pyi", ".md", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".txt",
    ".ini", ".cfg", ".lock", ".xml", ".csv", ".sql", ".sh", ".log", ".html",
    ".css", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".java", ".kt", ".rb",
    ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".scala",
)  # fmt: skip
_LAUNCHERS = frozenset({"./ww", "ww"})
_WW_COMMAND = re.compile(r"`\s*(?:\./)?ww(?:\s|`)")


def redact(text: str, *, task_id: str | None = None, run_id: str | None = None) -> str:
    """``text`` with the task, run, paths, quoted values, emails and keys hidden.

    The task and run IDs become ``<task>`` and ``<run>``, email addresses
    ``<email>``, quoted and backticked values ``<value>``, file paths
    ``<path>`` and Jira-like keys ``<ticket>``. A backticked ww command keeps
    its words, since it is ww's own structure, and is redacted inside.
    """
    for literal, placeholder in (
        (task_id, "<task>"),
        (task_slug(task_id) if task_id else None, "<task>"),
        (run_id, "<run>"),
    ):
        if literal:
            text = text.replace(literal, placeholder)
    text = _EMAIL.sub("<email>", text)
    text = _QUOTED.sub(_redact_quoted, text)
    text = _TOKEN.sub(_redact_path, text)
    return _TICKET.sub("<ticket>", text)


def _redact_quoted(match: re.Match[str]) -> str:
    quoted = match.group()
    if _WW_COMMAND.match(quoted):
        inner = _TOKEN.sub(_redact_path, quoted[1:-1])
        return f"`{_TICKET.sub('<ticket>', inner)}`"
    return "<value>"


def _redact_path(match: re.Match[str]) -> str:
    token = match.group()
    trailing = _TRAILING_PUNCTUATION.search(token)
    end = trailing.group() if trailing else ""
    core = token[: len(token) - len(end)]
    if core in _LAUNCHERS:
        return token
    if "/" in core or core.lower().endswith(_PATH_EXTENSIONS):
        return "<path>" + end
    return token


def _redacted(
    entries: Iterable[Mapping[str, Any]], task_id: str | None, run_id: str | None
) -> list[dict[str, Any]]:
    """``entries`` (report entries or events) with their free text redacted."""
    return [
        {
            key: redact(value, task_id=task_id, run_id=run_id)
            if key in {"summary", "detail"} and isinstance(value, str)
            else value
            for key, value in entry.items()
        }
        for entry in entries
    ]


def task_digest(task_id: str) -> str:
    """How a record names its task: a short SHA-256 of the task ID."""
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:TASK_DIGEST_LENGTH]


def record_id(task_id: str, run_id: str) -> str:
    """The record's ID and file stem: the task's digest and the run."""
    return f"{task_digest(task_id)}--{run_id}"


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
        workflow_shape: Mapping[str, Any] | None,
        request_id: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        """Write the run's record from the supplied values; none without them.

        The record names the task by its digest only, and every string in it
        passes ``redact``. A kind with events takes over the events pending
        for the task and for ``request_id``, the bootstrap request the run
        was bound from, and drops their standalone notes records, now that
        the run's own record holds them. A run that already has its record
        keeps it: the existing record is returned unchanged.
        """
        parsed = validate_report_values(
            {
                array.variable: values[array.variable]
                for array in self.kind.arrays
                if array.variable in values
            }
        )
        if len(parsed) != len(self.kind.arrays):
            return None
        record = self._new_record(
            task_id,
            run_id,
            workflow=workflow,
            agent=agent,
            runtime=runtime,
            modes=modes,
            now=now,
        )
        record["workflow_shape"] = dict(workflow_shape) if workflow_shape else None
        record.update(
            {
                array.field: _redacted(parsed[array.variable], task_id, run_id)
                for array in self.kind.arrays
            }
        )
        with self._lock():
            # A completed run is committed more than once under ``auto``;
            # the first write took the pending events, so it stands.
            existing = self.directory / f"{record['id']}.json"
            if existing.is_file():
                return self._read(existing)
            if self.kind.events:
                owners = [task_id] + ([request_id] if request_id else [])
                record["events"] = _redacted(
                    self._take_pending(owners), task_id, run_id
                )
            self._save(record)
        return record

    def _new_record(
        self,
        task_id: str,
        run_id: str,
        *,
        workflow: str | None,
        agent: str | None,
        runtime: str | None,
        modes: tuple[str, ...],
        now: str | None,
    ) -> dict[str, Any]:
        """A record's fixed fields, in their order; the arrays follow."""
        # The plan compiler imports this module for the summary step's
        # variables, so the schema versions are read here, not at import.
        from ww.execution_models.runs import PLAN_SCHEMA_VERSION
        from ww.storage_adapters.task_document import TASK_STATE_SCHEMA_VERSION

        return {
            "schema": RECORD_SCHEMA,
            "id": record_id(task_id, run_id),
            "kind": self.kind.name,
            "workflow": workflow or "unknown",
            "recorded_at": now or _now(),
            "run_id": run_id,
            "agent": agent,
            "runtime": runtime,
            "modes": list(modes),
            "ww_version": __version__,
            "plan_schema_version": PLAN_SCHEMA_VERSION,
            "task_state_schema_version": TASK_STATE_SCHEMA_VERSION,
            "workflow_shape": None,
            **{array.field: [] for array in self.kind.arrays},
            "reported": None,
        }

    def collecting(self) -> bool:
        """Whether the project collects this kind now."""
        config = load_project_config(self.storage.project_config_path)
        return self.kind.name in {kind.name for kind in collected_kinds(config)}

    def note(
        self,
        owner: str,
        *,
        source: str,
        summary: str,
        detail: str | None = None,
        once: bool = False,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        """Keep one event for ``owner``, a task or request, until its run's record.

        ``ww`` events are kept only while the kind is collected; the operator's
        notes always are. ``once`` drops an event ``ww`` already recorded
        with the same words, so one stop is noted once, not on every
        transition that leaves the run stopped. The event, or ``None`` when
        nothing was kept.
        """
        if not self.kind.events:
            raise StateError(f"{self.kind.name} records carry no events")
        if source not in EVENT_SOURCES:
            raise StateError(f"unknown event source {source!r}")
        if not summary.strip():
            raise StateError("an event needs a non-empty summary")
        if source == "ww" and not self.collecting():
            return None
        event = _event(source, summary, detail, now)
        with self._lock():
            pending = self.pending(owner)
            if once and any(
                (kept["source"], kept["summary"], kept.get("detail"))
                == (event["source"], event["summary"], event.get("detail"))
                for kept in pending
            ):
                return None
            path = self._pending_path(owner)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
        return event

    def add_event(
        self,
        identifier: str,
        *,
        source: str,
        summary: str,
        detail: str | None = None,
        task_id: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Append one event, redacted, to the record ``identifier``.

        ``task_id`` is the task the record belongs to, when the caller knows
        it, so the event hides it too.
        """
        if source not in EVENT_SOURCES:
            raise StateError(f"unknown event source {source!r}")
        if not summary.strip():
            raise StateError("an event needs a non-empty summary")
        with self._lock():
            record = self.get(identifier)
            (event,) = _redacted(
                [_event(source, summary, detail, now)], task_id, record.get("run_id")
            )
            record.setdefault("events", []).append(event)
            self._save(record)
        return record

    def latest_for(self, task_id: str) -> dict[str, Any] | None:
        """The task's latest run record, if a run of it completed."""
        prefix = f"{task_digest(task_id)}--"
        runs = [
            record
            for record in self.records()
            if str(record["id"]).startswith(prefix)
            and record.get("run_id") != NOTES_RUN_ID
        ]
        return runs[-1] if runs else None

    def notes_record(
        self,
        owner: str,
        *,
        workflow: str | None,
        agent: str | None,
        runtime: str | None,
        modes: tuple[str, ...],
        now: str | None = None,
    ) -> dict[str, Any]:
        """Write ``owner``'s pending events as a standalone record.

        Written when the operator notes a task or request without a completed
        run, so the note is kept even if no run ever completes; the run's own
        record replaces it when one does.
        """
        record = self._new_record(
            owner,
            NOTES_RUN_ID,
            workflow=workflow,
            agent=agent,
            runtime=runtime,
            modes=modes,
            now=now,
        )
        with self._lock():
            record["events"] = _redacted(self.pending(owner), owner, None)
            self._save(record)
        return record

    def events_notice(self, owner: str) -> str:
        """What the page asking for the assessment says about ``owner``'s events.

        The events ww observed itself are listed as they wait for the run's
        record, so the agent adds what they miss instead of repeating them.
        """
        pending = self.pending(owner)
        if not pending:
            return (
                "ww recorded no events of its own for this run: whatever went "
                "wrong, only your answers will say so."
            )
        listed = "; ".join(
            f"{event.get('at')} ({event.get('source')}) {event['summary']}"
            + (f": {event['detail']}" if event.get("detail") else "")
            for event in pending
        )
        return (
            "ww already recorded these events for this run; add what they miss "
            f"rather than repeating them: {listed}"
        )

    def _pending_path(self, owner: str) -> Path:
        return self.directory / PENDING_DIRECTORY / f"{task_slug(owner)}.jsonl"

    def pending(self, owner: str) -> list[dict[str, Any]]:
        """The events kept for ``owner``; a line that cannot be read is skipped."""
        path = self._pending_path(owner)
        if not path.is_file():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and isinstance(event.get("summary"), str):
                events.append(event)
        return events

    def _take_pending(self, owners: list[str]) -> list[dict[str, Any]]:
        """Collect and remove the owners' pending events and notes records."""
        events = [event for owner in owners for event in self.pending(owner)]
        events.sort(key=lambda event: str(event.get("at", "")))
        for owner in owners:
            self._pending_path(owner).unlink(missing_ok=True)
            notes = self.directory / f"{record_id(owner, NOTES_RUN_ID)}.json"
            if notes.is_file():
                try:
                    reported = self._read(notes).get("reported")
                except StateError:
                    reported = None
                if not reported:
                    notes.unlink(missing_ok=True)
        return events

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
            if self.kind.events and not isinstance(data.setdefault("events", []), list):
                raise ValueError("events must be a list")
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
        if store.kind.events:
            counts += f", events: {len(record.get('events') or [])}"
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
    """The record as Markdown: exactly what a report would send.

    A record's strings were redacted when it was written, so the page and
    the issue body carry nothing the record does not.
    """
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
    if kind.events:
        lines.extend(["", "## Events ww observed and the operator noted", ""])
        events = record.get("events") or []
        if not events:
            lines.append("None.")
        for event in events:
            detail = f": {event['detail']}" if event.get("detail") else ""
            lines.append(
                f"- {event.get('at')} ({event.get('source')}) "
                f"**{event['summary']}**{detail}"
            )
    shape = record.get("workflow_shape")
    if shape is not None:
        lines.extend(
            [
                "",
                "<details><summary>Workflow shape</summary>",
                "",
                "```json",
                json.dumps(shape, indent=2),
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
    slim = {**record, "workflow_shape": None}
    body = (
        issue_body(slim)
        + "\n_The workflow shape was left out: too long for a prefilled issue._\n"
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


def _event(
    source: str, summary: str, detail: str | None, now: str | None
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "at": now or _now(),
        "source": source,
        "summary": summary.strip(),
    }
    if detail is not None and detail.strip():
        event["detail"] = detail.strip()
    return event


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
