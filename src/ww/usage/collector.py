# SPDX-License-Identifier: GPL-3.0-or-later
"""Collecting token usage from the agents' session logs.

Every ww command records the agent session that ran it (see
:mod:`ww.cli.audit`).  The collector reads those sessions' logs from where it
stopped last time and places each model response's tokens:

- A session's tokens belong to the task its latest ww command put it on, on
  the step under way at the time, as ``worker`` when that command was a
  worker's and as ``manager`` or ``single`` otherwise.
- A ww command counts only once the execution log shows it ran here, so a
  command an agent merely wrote into a file or ran for another project
  moves nothing.
- Tokens before a session's next ww command wait: ``start``, and a worker's
  first ``instruction``, claim them for their task; any other command, or
  six hours without a line, leaves them outside every task.  A task whose
  run has finished stops taking tokens; one this ww cannot read in full
  takes them without a step.
- A subagent's tokens follow its own ww commands; until it runs one, they go
  to whatever its spawner was doing when it started it, once the subagent
  has fallen quiet.

Nothing here may fail a ww command: callers run it with every error
suppressed, and a session whose logs are not on this machine is only noted,
with the host it ran on, until a collection where they are reads it.
"""

from __future__ import annotations

import json
import os
from bisect import bisect_left
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ww.agents import (
    Agent,
    AgentRegistry,
    Call,
    LogFile,
    Spawn,
    TokenCounts,
    TurnUsage,
    Usage,
    UsageReader,
    builtin_agents,
)
from ww.errors import WwError
from ww.extensions import ExtensionRegistry
from ww.project_config import UsageSettings
from ww.storage import Storage
from ww.task_codenames import codenames
from ww.task_references import resolve_task_reference

from .store import (
    HOOK,
    TRANSCRIPT,
    FileState,
    SessionState,
    Stretch,
    UsageState,
    UsageStore,
)
from .timeline import TaskTimes, instant

# A subagent that wrote nothing for this long has finished, so its tokens
# with no ww command of its own can go to its spawner.
QUIET = timedelta(minutes=2)
# A session that wrote nothing for this long will run no ``start`` for the
# tokens still waiting: they belong to no task.
SESSION_QUIET = timedelta(hours=6)
# Automatic collections read only sessions seen this recently.
RECENT = timedelta(days=7)
# How far a hook looks back for the ww command that names its task.
HOOK_WINDOW = timedelta(hours=12)
# How long a ``start`` that named no task may take to record the task.
START_WINDOW = timedelta(minutes=15)
# A command an agent ran appears in the execution log this soon: ww records
# it as ``started`` before it does anything, in whole seconds.
RAN_BEFORE = timedelta(seconds=5)
RAN_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class Invocation:
    """One ww command from the execution log, with the session that ran it."""

    at: str
    command: str
    task: str | None
    agent: str | None
    session: str | None
    host: str | None


@dataclass(frozen=True)
class Collection:
    """What one collection read and recorded."""

    files: int = 0
    stretches: int = 0
    unreadable: tuple[str, ...] = ()


@dataclass
class _Totals:
    counts: TokenCounts = field(default_factory=TokenCounts)
    first: str = ""
    last: str = ""

    def add(self, counts: TokenCounts, first: str, last: str) -> None:
        self.counts = self.counts + counts
        self.first = min(self.first, first) if self.first else first
        self.last = max(self.last, last)


# A place for tokens: task, run, step, role; ``None`` task is outside.
_Place = tuple[str | None, str | None, str | None, str | None]
_OUTSIDE: _Place = (None, None, None, None)


class Collector:
    """One collection over a checkout's known sessions."""

    def __init__(
        self,
        storage: Storage,
        settings: UsageSettings,
        task_format: str | None = None,
        *,
        agents: Iterable[Agent] | None = None,
        environ: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> None:
        """``agents`` are the project's (``AgentRegistry.known()``), so a
        declared or extension agent with a usage reader is read like a
        built-in one; without them, the built-ins."""
        self.storage = storage
        self.settings = settings
        self.task_format = task_format
        self.environ = os.environ if environ is None else environ
        self.now = now or datetime.now(timezone.utc)
        self.store = UsageStore(storage)
        self.times = TaskTimes(storage.task_persistence, storage.runtime_path / "tasks")
        self.agents = {
            agent.name: agent
            for agent in (builtin_agents() if agents is None else agents)
        }
        self._invocations: list[Invocation] | None = None
        self._invocation_times: list[str] | None = None
        self._known: tuple[tuple[str, ...], dict[str, str]] | None = None
        self._out: dict[tuple[Any, ...], _Totals] = {}

    # Entry points ---------------------------------------------------------

    def collect(
        self,
        *,
        everything: bool = False,
        scan: bool = False,
        rescan: Iterable[str] = (),
        rescan_all: bool = False,
    ) -> Collection:
        """Read every known session's new log lines and record their tokens.

        ``everything`` reads sessions of any age, not only recent ones;
        ``scan`` also looks in the agents' directories for sessions that ran
        in this checkout before ww recorded sessions; ``rescan`` names
        sessions (``<agent>:<session>``) to read again from the start, and
        ``rescan_all`` means every one: their earlier records are dropped,
        but only for sessions whose logs are here to read again.
        """
        if not self.settings.enabled:
            return Collection()
        with self.store.lock():
            state = self.store.load()
            sessions = self._sessions(state, everything=everything, scan=scan)
            again = set(sessions) if rescan_all else set(rescan)
            if again:
                readable = {
                    key
                    for key in again
                    if key in sessions and self._files(key, sessions[key])
                }
                self.store.forget_sessions(readable)
                state.files = {
                    key: file
                    for key, file in state.files.items()
                    if f"{file.agent}:{file.session}" not in readable
                }
            read = 0
            unreadable = []
            for key, session in sessions.items():
                agent = self.agents.get(session.agent)
                files = self._files(key, session)
                session.readable = bool(files)
                state.sessions[key] = session
                if not files or agent is None or agent.usage is None:
                    unreadable.append(key)
                    continue
                for file in files:
                    read += self._read_file(agent, agent.usage, key, file, state)
            stretches = self._stretches()
            self.store.append(stretches)
            self.store.save(state)
        return Collection(read, len(stretches), tuple(unreadable))

    def record_turn(self, agent: Agent, turn: TurnUsage) -> None:
        """Record one turn's tokens that an agent's hook reported.

        Such an agent tells ww commands no session, so the turn goes to the
        task of the latest ww command run without one, if that task is
        still under way; it is reported as the session's own, never a
        subagent's.
        """
        if not self.settings.enabled:
            return
        at = instant(self.now.isoformat())
        with self.store.lock():
            state = self.store.load()
            key = f"{agent.name}:{turn.session}"
            file = state.files.get(key) or FileState(agent.name, turn.session)
            seen = list(file.carry.get("seen", []))
            if turn.key in seen:
                return
            file.carry["seen"] = [*seen, turn.key][-64:]
            since = instant((self.now - HOOK_WINDOW).isoformat())
            latest = next(
                (
                    invocation
                    for invocation in reversed(self.invocations())
                    if invocation.task
                    and invocation.session is None
                    and invocation.command != "hook"
                    and since <= invocation.at <= at
                ),
                None,
            )
            place = _OUTSIDE
            if latest and latest.task and self.times.active(latest.task, at):
                runtime = self.times.runtime(latest.task, at)
                place = (
                    latest.task,
                    *self.times.place(latest.task, at),
                    "single" if runtime == "single" else "manager",
                )
            self._add(
                place,
                agent.name,
                turn.session,
                key,
                turn.model,
                HOOK,
                at,
                at,
                turn.counts,
            )
            state.files[key] = file
            state.sessions[key] = SessionState(agent.name, None, True)
            self.store.append(self._stretches())
            self.store.save(state)

    # Sessions and files ---------------------------------------------------

    def invocations(self) -> list[Invocation]:
        """Every ww command in the execution log and its rotated files, oldest first."""
        if self._invocations is None:
            path = self.storage.runtime_path / "executions.jsonl"
            found = []
            for log in sorted(path.parent.glob(path.name + ".*"), reverse=True) + [
                path
            ]:
                try:
                    lines = log.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for line in lines:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and isinstance(
                        record.get("timestamp"), str
                    ):
                        found.append(
                            Invocation(
                                instant(record["timestamp"]),
                                str(record.get("command")),
                                _text(record.get("task_id")),
                                _text(record.get("agent")),
                                _text(record.get("agent_session")),
                                _text(record.get("host")),
                            )
                        )
            self._invocations = sorted(found, key=lambda invocation: invocation.at)
        return self._invocations

    def task_sessions(self, task_id: str) -> dict[str, str | None]:
        """The sessions that ran a ww command for ``task_id``, with their host."""
        return {
            f"{invocation.agent}:{invocation.session}": invocation.host
            for invocation in self.invocations()
            if invocation.task == task_id and invocation.agent and invocation.session
        }

    def _sessions(
        self, state: UsageState, *, everything: bool, scan: bool
    ) -> dict[str, SessionState]:
        since = instant((self.now - RECENT).isoformat())
        found: dict[str, SessionState] = {}
        if everything:
            found.update(state.sessions)
        for invocation in self.invocations():
            if not (invocation.agent and invocation.session):
                continue
            if everything or invocation.at >= since:
                key = f"{invocation.agent}:{invocation.session}"
                known = found.get(key) or state.sessions.get(key)
                found[key] = SessionState(
                    invocation.agent,
                    invocation.host or (known.host if known else None),
                    known.readable if known else None,
                )
        if scan:
            for agent in self.agents.values():
                if agent.usage is None:
                    continue
                for session in agent.usage.sessions_in(
                    self.roots(agent), self.storage.root
                ):
                    key = f"{agent.name}:{session}"
                    found.setdefault(
                        key, state.sessions.get(key) or SessionState(agent.name)
                    )
        # A session hooks report has no logs to read.
        return {
            key: session
            for key, session in found.items()
            if (owner := self.agents.get(session.agent)) is not None
            and owner.usage is not None
        }

    def roots(self, agent: Agent) -> tuple[Path, ...]:
        """Where to look for ``agent``'s logs: its own home and the extra roots."""
        assert agent.usage is not None
        extra = tuple(
            path if path.is_absolute() else self.storage.root / path
            for path in (Path(root).expanduser() for root in self.settings.log_roots)
        )
        return (*agent.usage.homes(self.environ), *extra)

    def _files(self, key: str, session: SessionState) -> tuple[LogFile, ...]:
        agent = self.agents.get(session.agent)
        if agent is None or agent.usage is None:
            return ()
        try:
            found = agent.usage.files(
                key.removeprefix(f"{agent.name}:"), self.roots(agent)
            )
        except OSError:
            return ()
        # A reader's keys are bare IDs; ww's carry the agent's name.
        return tuple(
            replace(
                file,
                key=f"{agent.name}:{file.key}",
                parent=f"{agent.name}:{file.parent}" if file.parent else None,
            )
            for file in found
        )

    # Reading --------------------------------------------------------------

    def _read_file(
        self,
        agent: Agent,
        reader: UsageReader,
        session_key: str,
        file: LogFile,
        state: UsageState,
    ) -> int:
        # An agent's name may hold a colon (``ext/.../agents:<key>``).
        session = session_key.removeprefix(f"{agent.name}:")
        current = state.files.get(file.key) or FileState(agent.name, session)
        current.path = str(file.path)
        state.files[file.key] = current
        try:
            stat = file.path.stat()
        except OSError:
            return 0
        read = 0
        if stat.st_size > current.offset:
            try:
                reading = reader.read(file, current.offset, current.carry)
            except OSError:
                return 0
            current.offset = reading.offset
            current.carry = reading.carry
            current.directory = current.directory or reading.directory
            for event in reading.events:
                if isinstance(event, Spawn):
                    current.spawns[event.tool] = instant(event.at)
                elif isinstance(event, Call):
                    self._call(event, current, file, state)
                elif isinstance(event, Usage):
                    self._usage(event, current, file)
            read = 1
        silent = self.now - datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        if current.pending and _spawned(file):
            if not current.calls and silent > QUIET:
                self._settle_with_spawner(current, file, state)
        elif current.pending and silent > SESSION_QUIET:
            self._settle(current, file, _OUTSIDE)
        return read

    def _call(
        self, call: Call, current: FileState, file: LogFile, state: UsageState
    ) -> None:
        at = instant(call.at)
        task = self._task(call, current.session, at)
        if task is None or not self._ran(call.verb, task, at, current, file):
            return
        role = (
            "worker"
            if call.role == "worker"
            else "single"
            if self.times.runtime(task, at) == "single"
            else "manager"
        )
        if current.pending:
            if _claims(call.verb, call.role):
                self._settle(current, file, (task, *self.times.place(task, at), role))
            elif _spawned(file) and not current.calls:
                self._settle_with_spawner(current, file, state)
            else:
                self._settle(current, file, _OUTSIDE)
        current.calls.append([at, task, role, call.verb])

    def _usage(self, usage: Usage, current: FileState, file: LogFile) -> None:
        at = instant(usage.at)
        if current.calls:
            _, task, role, _ = current.calls[-1]
            if self.times.active(task, at):
                self._add(
                    (task, *self.times.place(task, at), role),
                    current.agent,
                    current.session,
                    file.key,
                    usage.model,
                    TRANSCRIPT,
                    at,
                    at,
                    usage.counts,
                )
                return
        waiting = current.pending.setdefault(
            usage.model or "", {"first": at, "last": at}
        )
        totals = TokenCounts.from_dict(waiting) + usage.counts
        waiting.update(totals.to_dict())
        waiting["last"] = at

    # Attribution ----------------------------------------------------------

    def _task(self, call: Call, session: str, at: str) -> str | None:
        """The existing task a call names; a ``start`` without one, from the log."""
        if call.task:
            known, names = self._tasks()
            try:
                resolved = resolve_task_reference(
                    call.task, self.task_format, known, names
                ).resolved
            except WwError:
                return None
            return resolved if resolved and self.times.exists(resolved) else None
        if call.verb != "start":
            return None
        # ww logs ``start`` once it has finished, in whole seconds.
        since = instant((_moment(at) - timedelta(seconds=5)).isoformat())
        until = instant((_moment(at) + START_WINDOW).isoformat())
        starts = [
            invocation
            for invocation in self.invocations()
            if invocation.command == "start"
            and invocation.task
            and since <= invocation.at <= until
        ]
        own = [invocation for invocation in starts if invocation.session == session]
        chosen = (own or starts)[:1]
        return chosen[0].task if chosen else None

    def _ran(
        self, verb: str, task: str, at: str, current: FileState, file: LogFile
    ) -> bool:
        """Whether this project's execution log saw the command run.

        A log quotes ww commands it never ran here: in a file it writes, a
        test, a page it checks, or against another project's ``--root``.  A
        command older than the execution log cannot be checked and counts.
        """
        invocations = self.invocations()
        if not invocations or at < invocations[0].at:
            return True
        since = instant((_moment(at) - RAN_BEFORE).isoformat())
        until = instant((_moment(at) + RAN_AFTER).isoformat())
        # A Codex subagent runs with its own thread ID, a Claude Code one with
        # its parent's session ID.
        sessions = {None, current.session, file.key.rsplit(":", 1)[-1]}
        start = bisect_left(self._times(), since)
        for invocation in invocations[start:]:
            if invocation.at > until:
                return False
            if (
                invocation.command == verb
                and invocation.task == task
                and invocation.session in sessions
            ):
                return True
        return False

    def _times(self) -> list[str]:
        if self._invocation_times is None:
            self._invocation_times = [
                invocation.at for invocation in self.invocations()
            ]
        return self._invocation_times

    def _tasks(self) -> tuple[tuple[str, ...], dict[str, str]]:
        if self._known is None:
            tasks = self.storage.task_persistence
            ids = [
                found
                for task_id in tasks.task_ids()
                for found in (task_id, *tasks.child_task_ids(task_id))
            ]
            self._known = (
                tuple(ids),
                {name: task_id for task_id, name in codenames(tasks).items()},
            )
        return self._known

    def _settle(self, current: FileState, file: LogFile, place: _Place) -> None:
        for model, waiting in current.pending.items():
            self._add(
                place,
                current.agent,
                current.session,
                file.key,
                model or None,
                TRANSCRIPT,
                str(waiting.get("first", "")),
                str(waiting.get("last", "")),
                TokenCounts.from_dict(waiting),
            )
        current.pending = {}

    def _settle_with_spawner(
        self, current: FileState, file: LogFile, state: UsageState
    ) -> None:
        """Give a subagent's waiting tokens to what its spawner was doing."""
        spawner, at = _spawner(file, current, state)
        if spawner is None or at is None:
            self._settle(current, file, _OUTSIDE)
            return
        place = self._place_at(spawner, at)
        if place is None:
            # The spawner's own tokens of that time still wait too.
            for model, waiting in current.pending.items():
                mine = spawner.pending.setdefault(model, {"first": waiting["first"]})
                totals = TokenCounts.from_dict(mine) + TokenCounts.from_dict(waiting)
                mine.update(totals.to_dict())
                mine["first"] = min(str(mine["first"]), str(waiting["first"]))
                mine["last"] = max(str(mine.get("last", "")), str(waiting["last"]))
            current.pending = {}
            return
        self._settle(current, file, place)

    def _place_at(self, file: FileState, at: str) -> _Place | None:
        """Where ``file``'s tokens at ``at`` belong; ``None`` while undecided."""
        before = [call for call in file.calls if call[0] <= at]
        if before:
            _, task, role, _ = before[-1]
            if self.times.active(task, at):
                return (task, *self.times.place(task, at), role)
        after = [call for call in file.calls if call[0] > at]
        if not after:
            return None
        call_at, task, role, verb = after[0]
        if _claims(verb, role):
            return (task, *self.times.place(task, call_at), role)
        return _OUTSIDE

    # Output ---------------------------------------------------------------

    def _add(
        self,
        place: _Place,
        agent: str,
        session: str,
        file: str,
        model: str | None,
        source: str,
        first: str,
        last: str,
        counts: TokenCounts,
    ) -> None:
        key = (*place, agent, session, file, model, source)
        self._out.setdefault(key, _Totals()).add(counts, first, last)

    def _stretches(self) -> list[Stretch]:
        stretches = [
            Stretch(
                task=task,
                run=run,
                step=step,
                role=role,
                agent=agent,
                session=session,
                file=file,
                model=model,
                source=source,
                first=totals.first,
                last=totals.last,
                counts=totals.counts,
            )
            for (
                task,
                run,
                step,
                role,
                agent,
                session,
                file,
                model,
                source,
            ), totals in (self._out.items())
            if totals.counts.total
        ]
        self._out = {}
        return stretches


def project_collector(storage: Storage, extensions: ExtensionRegistry) -> Collector:
    """A collector with the project's usage settings and every agent it names."""
    config = extensions.config
    return Collector(
        storage,
        config.usage,
        config.task_format,
        agents=AgentRegistry.of(extensions).known(),
    )


def _claims(verb: str, role: str | None) -> bool:
    """Whether a command claims the tokens waiting before it for its task.

    ``start`` claims the conversation that led to it, and a worker's
    ``instruction`` the subagent's reading of its prompt.
    """
    return verb == "start" or (verb == "instruction" and role == "worker")


def _spawned(file: LogFile) -> bool:
    return file.parent is not None or file.spawn_tool is not None


def _spawner(
    file: LogFile, current: FileState, state: UsageState
) -> tuple[FileState | None, str | None]:
    """The file that started ``file`` and when, by parent key or tool call."""
    if file.parent is not None:
        return state.files.get(file.parent), (
            instant(file.spawned_at) if file.spawned_at else None
        )
    for other in state.files.values():
        if (
            other is not current
            and other.session == current.session
            and file.spawn_tool in other.spawns
        ):
            return other, other.spawns[file.spawn_tool]
    return None, None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _moment(at: str) -> datetime:
    return datetime.fromisoformat(at.replace("Z", "+00:00"))
