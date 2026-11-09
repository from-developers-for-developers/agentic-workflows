# SPDX-License-Identifier: GPL-3.0-or-later
"""The process that watches one headless agent: ``python -m ww.headless.supervisor``.

ww starts it detached, so the ww command that launched the agent can return
at once.  It starts the agent's program in a session of its own, copies its
output to the log, reads each JSON line for the session ID and usage, stops
the program when it runs past its timeout or prints nothing for longer than
its idle timeout, and records how it ended.  The launch record it is given
holds everything it needs; the environment is the one it was started with.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from ww.agents import AgentRegistry, HeadlessLauncher, StreamFacts

from .records import LaunchStatus, read_record, write_record

# How long a stopped program has to end before it is killed.
GRACE_SECONDS = 10.0
# The supervisor's arguments: the record, the output log, the error log.
ARGUMENTS = 3
# How often the record is rewritten while output arrives.
WRITE_EVERY_SECONDS = 2.0


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stop_group(pid: int | None) -> None:
    """Stop the process group ``pid`` leads: politely, then for good."""
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _facts_reader(agent: str) -> Callable[[dict[str, Any]], StreamFacts]:
    launcher = AgentRegistry().resolve(agent).launcher("headless")
    if isinstance(launcher, HeadlessLauncher):
        return launcher.stream_facts
    return lambda line: StreamFacts()


class Supervisor:
    """Runs one launch's program to its end and keeps its record."""

    def __init__(self, path: Path, output: Path, errors: Path) -> None:
        self.path = path
        self.output = output
        self.errors = errors
        record = read_record(path)
        if record is None:
            raise SystemExit(f"no launch record at {path}")
        self.record = record
        self.facts = _facts_reader(record.agent)
        self.last_write = 0.0

    def save(self, **changes: Any) -> None:
        self.record = replace(self.record, **changes)
        write_record(self.path, self.record)
        self.last_write = time.monotonic()

    def run(self) -> int:
        try:
            with self.errors.open("ab") as errors:
                process = subprocess.Popen(
                    list(self.record.argv),
                    cwd=self.record.workspace,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    start_new_session=True,
                )
        except OSError as error:
            self.save(status="could_not_start", error=str(error), ended_at=now())
            return 1
        self.save(status="running", supervisor_pid=os.getpid(), agent_pid=process.pid)
        assert process.stdout is not None
        ended = self._watch(process, process.stdout)
        code = process.wait()
        if ended is not None:
            self.save(status=ended, exit_code=code, ended_at=now())
        else:
            self.save(status="exited", exit_code=code, ended_at=now())
        return 0

    def _watch(
        self, process: subprocess.Popen[bytes], stream: IO[bytes]
    ) -> LaunchStatus | None:
        """Copy the output until it ends; the status when the program was stopped."""
        started = time.monotonic()
        last_output = started
        timeout = _seconds(self.record.timeout_minutes)
        idle = _seconds(self.record.idle_timeout_minutes)
        selector = selectors.DefaultSelector()
        selector.register(stream, selectors.EVENT_READ)
        buffer = b""
        with self.output.open("ab") as log:
            while True:
                if selector.select(timeout=1.0):
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        break
                    log.write(chunk)
                    log.flush()
                    last_output = time.monotonic()
                    buffer += chunk
                    *lines, buffer = buffer.split(b"\n")
                    for line in lines:
                        self._read(line)
                    if time.monotonic() - self.last_write > WRITE_EVERY_SECONDS:
                        write_record(self.path, self.record)
                        self.last_write = time.monotonic()
                elapsed = time.monotonic()
                if timeout is not None and elapsed - started > timeout:
                    stop_group(process.pid)
                    return "timed_out"
                if idle is not None and elapsed - last_output > idle:
                    stop_group(process.pid)
                    return "idle"
            if buffer:
                self._read(buffer)
        return None

    def _read(self, line: bytes) -> None:
        try:
            value = json.loads(line)
        except ValueError:
            return
        if not isinstance(value, dict):
            return
        facts = self.facts(value)
        changes: dict[str, Any] = {}
        if facts.session_id and self.record.session_id is None:
            changes["session_id"] = facts.session_id
        if facts.usage is not None:
            changes["usage"] = self.record.usage + facts.usage
        if changes:
            self.record = replace(self.record, **changes)


def _seconds(minutes: float | None) -> float | None:
    return minutes * 60 if minutes is not None else None


def main(argv: list[str]) -> int:
    if len(argv) != ARGUMENTS:
        print(
            "usage: python -m ww.headless.supervisor RECORD OUTPUT ERRORS",
            file=sys.stderr,
        )
        return 2
    path, output, errors = (Path(each) for each in argv)
    return Supervisor(path, output, errors).run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
