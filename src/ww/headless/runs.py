# SPDX-License-Identifier: GPL-3.0-or-later
"""Starting, watching and stopping the headless runs of a task's steps.

When the manager's ``next`` reaches a step whose agent runs headless, ww
starts that agent itself instead of handing the manager a worker bootstrap:
it checks the agent's program, writes a launch record, and starts the
supervisor (:mod:`ww.headless.supervisor`) detached.  The manager's page
then says the run is going and tells it to ``wait``; once the run ends
without completing the step, the page shows why and offers resuming,
starting again, or taking the step back into a session.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ww.agents import (
    AgentRegistry,
    CatalogCache,
    Finding,
    HeadlessLauncher,
    LaunchRequest,
)
from ww.errors import StateError
from ww.instructions.commands import (
    headless_launch_command,
    headless_wait_command,
    instruction_command,
)
from ww.instructions.models import HeadlessState, HeadlessView, Instruction
from ww.project_config import ProjectConfig

from .records import LaunchRecord, LaunchStore, read_record, write_record
from .supervisor import stop_group

# Set in a headless agent's environment: the launch it belongs to.  ww
# refuses manager commands from such a process, since its agent performs one
# step and is not the task's manager.
LAUNCH_VARIABLE = "WW_HEADLESS_LAUNCH"
# How many lines of each stream a failure page shows.
TAIL_LINES = 20


class LaunchRefusedError(StateError):
    """The agent's program failed the checks that must hold before a launch."""

    def __init__(self, agent: str, findings: tuple[Finding, ...]) -> None:
        self.agent = agent
        self.findings = findings
        errors = "; ".join(f.message for f in findings if f.level == "error")
        super().__init__(f"ww did not start {agent} headless: {errors}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def worker_prompt(instruction: Instruction, *, resuming: bool = False) -> str:
    """What a headless agent is told: run its page, finish, and stop."""
    bootstrap = instruction_command(
        instruction.task_id,
        instruction.run_id,
        role="worker",
        assignment=instruction.assignment_token,
    )
    first = (
        "Continue your ww assignment. If it is not completed yet, run "
        f"`{bootstrap}` to read its page again"
        if resuming
        else f"You are a ww worker. Run `{bootstrap}`"
    )
    return (
        f"{first} and follow the page it prints: it is your whole assignment. "
        "Finish with the completion command the page shows. You perform this "
        "one step only: once ww has recorded your result, stop, and never run "
        "a command with `--role manager`."
    )


def is_headless_dispatch(instruction: Instruction) -> bool:
    """Whether ``instruction`` hands the manager a step ww runs headless."""
    return (
        instruction.requested_run == "headless"
        and instruction.item_id is not None
        and instruction.next_role == "worker"
        and instruction.role == "worker"
        and instruction.status in {"pending", "in_progress"}
        and instruction.parallel_branches == ()
    )


def _clean_environment(
    environ: Mapping[str, str], session_variables: Iterable[str]
) -> dict[str, str]:
    """``environ`` without any agent's session variable, so the launched
    agent's ww commands are not counted as its host's."""
    dropped = set(session_variables)
    return {key: value for key, value in environ.items() if key not in dropped}


def _git_directory(workspace: Path) -> Path | None:
    """The repository's shared Git directory, which commits write to."""
    try:
        found = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = found.stdout.strip()
    return Path(path) if found.returncode == 0 and path else None


def _tail(path: Path, lines: int = TAIL_LINES) -> tuple[str, ...]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(text.splitlines()[-lines:])


class HeadlessRuns:
    """The headless runs of one project's tasks."""

    def __init__(
        self,
        root: Path,
        config: ProjectConfig,
        registry: AgentRegistry,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.root = root
        self.config = config
        self.registry = registry
        self.store = LaunchStore(root)
        self.cache = CatalogCache(root / ".ww" / "agents" / "catalogs")
        self.environ = os.environ if environ is None else environ

    # -- the manager's page ------------------------------------------------

    def dispatch(self, instruction: Instruction, *, launch: bool) -> Instruction:
        """``instruction`` with its headless run: started now when ``launch``
        and none is going, else as the run stands."""
        if not is_headless_dispatch(instruction):
            return instruction
        assert instruction.item_id is not None and instruction.run_id is not None
        latest = self.store.latest(
            instruction.task_id, instruction.run_id, instruction.item_id
        )
        if latest is not None and latest.assignment != instruction.assignment_token:
            latest = None
        if latest is not None and latest.released:
            return instruction
        if latest is None:
            if not launch:
                return replace(instruction, headless=self._pending_view(instruction))
            try:
                latest = self.launch(instruction)
            except LaunchRefusedError as refused:
                return replace(
                    instruction, headless=self._refused_view(instruction, refused)
                )
        return replace(instruction, headless=self.view(instruction, latest))

    def launch(
        self, instruction: Instruction, *, resume: LaunchRecord | None = None
    ) -> LaunchRecord:
        """Check the agent's program and start it on ``instruction``'s step."""
        agent_name = instruction.requested_agent or instruction.agent
        agent = self.registry.resolve(agent_name)
        launcher = agent.launcher("headless")
        if not isinstance(launcher, HeadlessLauncher):
            raise StateError(f"ww cannot run agent {agent_name!r} headless")
        settings = self.config.headless_settings(agent.name)
        workspace = Path(instruction.working_directory or self.root)
        writable = [self.root / ".ww"]
        git = _git_directory(workspace)
        if git is not None and not git.is_relative_to(workspace):
            writable.append(git)
        request = LaunchRequest(
            task_id=instruction.task_id,
            prompt=worker_prompt(instruction, resuming=resume is not None),
            working_directory=workspace,
            model=instruction.requested_model,
            reasoning=instruction.requested_reasoning,
            settings=settings,
            writable=tuple(writable),
        )
        readiness, findings = launcher.check(request, cache=self.cache)
        if readiness.binary is None or any(f.level == "error" for f in findings):
            raise LaunchRefusedError(agent.name, findings)
        if resume is not None and resume.session_id is None:
            raise StateError(
                f"launch {resume.id} named no session, so it cannot be resumed; "
                "start it again instead"
            )
        argv = (
            launcher.resume_command(readiness.binary, request, resume.session_id)
            if resume is not None and resume.session_id is not None
            else launcher.launch_command(readiness.binary, request)
        )
        assert instruction.item_id is not None and instruction.run_id is not None
        record = LaunchRecord(
            id=secrets.token_hex(4),
            task_id=instruction.task_id,
            run_id=instruction.run_id,
            item_id=instruction.item_id,
            step=instruction.step or instruction.item_name or instruction.item_id,
            agent=agent.name,
            argv=tuple(argv),
            workspace=str(workspace),
            started_at=_now(),
            assignment=instruction.assignment_token,
            resumes=resume.id if resume is not None else None,
            timeout_minutes=settings.timeout_minutes,
            idle_timeout_minutes=settings.idle_timeout_minutes,
        )
        self._start(record, settings.env)
        return record

    def _start(self, record: LaunchRecord, env: Mapping[str, str]) -> None:
        self.store.write(record)
        session_variables = [
            agent.session_variable
            for agent in self.registry.known()
            if agent.session_variable
        ]
        environment = {
            **_clean_environment(self.environ, session_variables),
            **env,
            LAUNCH_VARIABLE: record.id,
        }
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "ww.headless.supervisor",
                    str(self.store.path(record)),
                    str(self.store.output_path(record)),
                    str(self.store.error_path(record)),
                ],
                cwd=record.workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            self.store.write(
                replace(
                    record, status="could_not_start", error=str(error), ended_at=_now()
                )
            )

    # -- the manager's decisions after a failed run -------------------------

    def relaunch(self, instruction: Instruction, *, resume: bool) -> LaunchRecord:
        """Start the step's agent again, continuing its last session when
        ``resume``.  Refused while a run is still going."""
        latest = self._current(instruction)
        if latest is not None and not latest.finished:
            raise StateError(
                f"the headless run {latest.id} of this step is still going; wait for it"
            )
        if resume and latest is None:
            raise StateError("this step has no headless run to resume")
        return self.launch(instruction, resume=latest if resume else None)

    def release(self, instruction: Instruction) -> None:
        """Give the step back to a session: ww stops waiting on its runs."""
        latest = self._current(instruction)
        if latest is None:
            raise StateError("this step has no headless run to give back")
        if latest.released:
            raise StateError("this step was already given back to a session")
        if not latest.finished:
            raise StateError(
                f"the headless run {latest.id} of this step is still going; wait for it"
            )
        self.store.release(latest)

    def _current(self, instruction: Instruction) -> LaunchRecord | None:
        if not is_headless_dispatch(instruction):
            raise StateError("the current step does not run headless")
        assert instruction.item_id is not None and instruction.run_id is not None
        latest = self.store.latest(
            instruction.task_id, instruction.run_id, instruction.item_id
        )
        if latest is None or latest.assignment != instruction.assignment_token:
            return None
        return latest

    def stop_all(self, task_id: str) -> tuple[LaunchRecord, ...]:
        """Stop every run of ``task_id`` still going, such as on ``reset``."""
        stopped = []
        for path in sorted(
            (self.store.tasks / task_id / "runs").glob("*/headless/*.json")
        ):
            record = read_record(path)
            if record is None or record.finished:
                continue
            stop_group(record.supervisor_pid)
            stop_group(record.agent_pid)
            record = replace(
                record, status="stopped", error="the task was reset", ended_at=_now()
            )
            write_record(path, record)
            stopped.append(record)
        return tuple(stopped)

    # -- views ---------------------------------------------------------------

    def view(self, instruction: Instruction, record: LaunchRecord) -> HeadlessView:
        task = instruction.task_id
        state: HeadlessState = "failed" if record.finished else "running"
        return HeadlessView(
            state=state,
            agent=record.agent,
            launch_id=record.id,
            status=record.status,
            reason=failure_reason(record) if state == "failed" else None,
            exit_code=record.exit_code,
            session_id=record.session_id,
            started_at=record.started_at,
            ended_at=record.ended_at,
            output_log=str(self.store.output_path(record)),
            error_log=str(self.store.error_path(record)),
            output_tail=_tail(self.store.output_path(record))
            if state == "failed"
            else (),
            error_tail=_tail(self.store.error_path(record))
            if state == "failed"
            else (),
            usage=record.usage,
            wait_command=headless_wait_command(task),
            resume_command=(
                headless_launch_command(task, "resume")
                if record.session_id is not None
                else None
            ),
            relaunch_command=headless_launch_command(task),
            session_command=headless_launch_command(task, "session"),
        )

    def _pending_view(self, instruction: Instruction) -> HeadlessView:
        return HeadlessView(
            state="not_started",
            agent=instruction.requested_agent or instruction.agent,
            wait_command=headless_wait_command(instruction.task_id),
            relaunch_command=headless_launch_command(instruction.task_id),
            session_command=headless_launch_command(instruction.task_id, "session"),
        )

    def _refused_view(
        self, instruction: Instruction, refused: LaunchRefusedError
    ) -> HeadlessView:
        return replace(
            self._pending_view(instruction),
            state="refused",
            agent=refused.agent,
            findings=tuple(
                f"{finding.level.capitalize()}: {finding.message}"
                for finding in refused.findings
            ),
        )


def failure_reason(record: LaunchRecord) -> str:
    """Why a finished run did not complete its step, in a sentence."""
    if record.status == "could_not_start":
        return f"its program could not be started: {record.error or 'unknown error'}"
    if record.status == "timed_out":
        return (
            f"it ran longer than its timeout of {record.timeout_minutes:g} minutes "
            "and was stopped"
        )
    if record.status == "idle":
        return (
            f"it printed nothing for {record.idle_timeout_minutes:g} minutes and "
            "was stopped"
        )
    if record.status == "stopped":
        return f"it was stopped: {record.error or 'by ww'}"
    if record.exit_code:
        return f"it exited with code {record.exit_code} without completing the step"
    return "it ended without completing the step"
