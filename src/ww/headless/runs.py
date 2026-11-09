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
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
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
from ww.instructions.models import (
    BranchView,
    HeadlessState,
    HeadlessView,
    Instruction,
)
from ww.project_config import ProjectConfig

from .records import LaunchRecord, LaunchStore, read_record, write_record
from .supervisor import stop_group

# Set in a headless agent's environment: the launch it belongs to.  ww
# refuses manager commands from such a process, since its agent performs one
# step and is not the task's manager.
LAUNCH_VARIABLE = "WW_HEADLESS_LAUNCH"
# How many lines of each stream a failure page shows.
TAIL_LINES = 20
# How long stopping waits for a just-launched supervisor to record its pid.
STARTUP_SECONDS = 10.0


class LaunchRefusedError(StateError):
    """The agent's program failed the checks that must hold before a launch."""

    def __init__(self, agent: str, findings: tuple[Finding, ...]) -> None:
        self.agent = agent
        self.findings = findings
        errors = "; ".join(f.message for f in findings if f.level == "error")
        super().__init__(f"ww did not start {agent} headless: {errors}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Target:
    """What one headless run works on: a step, or one branch of a parallel
    step, with the agent, settings and page command it is started with."""

    agent: str
    model: str | None
    reasoning: str | None
    bootstrap: str
    branch: str | None = None


def worker_prompt(bootstrap: str, *, resuming: bool = False) -> str:
    """What a headless agent is told: run its page, finish, and stop."""
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


def is_headless_group(instruction: Instruction) -> bool:
    """Whether ``instruction`` is a parallel step in progress with a branch
    ww runs headless."""
    return (
        instruction.item_id is not None
        and instruction.item_status == "in_progress"
        and any(b.requested_run == "headless" for b in instruction.parallel_branches)
    )


def _step_target(instruction: Instruction) -> Target:
    return Target(
        agent=instruction.requested_agent or instruction.agent,
        model=instruction.requested_model,
        reasoning=instruction.requested_reasoning,
        bootstrap=instruction_command(
            instruction.task_id,
            instruction.run_id,
            role="worker",
            assignment=instruction.assignment_token,
        ),
    )


def _branch_target(instruction: Instruction, branch: BranchView) -> Target:
    return Target(
        agent=branch.requested_agent or instruction.agent,
        model=branch.requested_model,
        reasoning=branch.requested_reasoning,
        bootstrap=branch.instruction_command,
        branch=branch.name,
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
        """``instruction`` with its headless runs: those not started yet are
        started when ``launch`` (as far as ``max_parallel`` allows), the others
        shown as they stand."""
        if is_headless_group(instruction):
            branches = tuple(
                self._branch(instruction, branch, launch=launch)
                for branch in instruction.parallel_branches
            )
            return replace(instruction, parallel_branches=branches)
        if not is_headless_dispatch(instruction):
            return instruction
        view = self._run(instruction, _step_target(instruction), launch=launch)
        return instruction if view is None else replace(instruction, headless=view)

    def _branch(
        self, instruction: Instruction, branch: BranchView, *, launch: bool
    ) -> BranchView:
        if branch.requested_run != "headless" or branch.artifact is not None:
            return branch
        view = self._run(
            instruction, _branch_target(instruction, branch), launch=launch
        )
        return branch if view is None else replace(branch, headless=view)

    def _run(
        self, instruction: Instruction, target: Target, *, launch: bool
    ) -> HeadlessView | None:
        """The view of ``target``'s run, started first when it has none; ``None``
        once it was given back to a session."""
        latest = self._latest(instruction, target)
        if latest is not None and latest.released:
            return None
        if latest is not None:
            return self.view(instruction, latest)
        if not launch:
            return self._pending_view(instruction, target, "not_started")
        if not self._has_room(target.agent):
            return self._pending_view(instruction, target, "queued")
        try:
            return self.view(instruction, self.launch(instruction, target))
        except LaunchRefusedError as refused:
            return replace(
                self._pending_view(instruction, target, "refused"),
                findings=tuple(
                    f"{finding.level.capitalize()}: {finding.message}"
                    for finding in refused.findings
                ),
            )

    def _latest(self, instruction: Instruction, target: Target) -> LaunchRecord | None:
        assert instruction.item_id is not None and instruction.run_id is not None
        latest = self.store.latest(
            instruction.task_id, instruction.run_id, instruction.item_id, target.branch
        )
        if latest is not None and latest.assignment != instruction.assignment_token:
            return None
        return latest

    def _has_room(self, agent_name: str) -> bool:
        """Whether another run of the agent fits its ``max_parallel``."""
        agent = self.registry.resolve(agent_name)
        limit = self.config.headless_settings(agent.name).max_parallel
        return limit is None or self.store.running(agent.name) < limit

    def launch(
        self,
        instruction: Instruction,
        target: Target,
        *,
        resume: LaunchRecord | None = None,
    ) -> LaunchRecord:
        """Check the agent's program and start it on ``target``."""
        agent = self.registry.resolve(target.agent)
        launcher = agent.launcher("headless")
        if not isinstance(launcher, HeadlessLauncher):
            raise StateError(f"ww cannot run agent {target.agent!r} headless")
        settings = self.config.headless_settings(agent.name)
        workspace = Path(instruction.working_directory or self.root)
        writable = [self.root / ".ww"]
        git = _git_directory(workspace)
        if git is not None and not git.is_relative_to(workspace):
            writable.append(git)
        request = LaunchRequest(
            task_id=instruction.task_id,
            prompt=worker_prompt(target.bootstrap, resuming=resume is not None),
            working_directory=workspace,
            model=target.model,
            reasoning=target.reasoning,
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
        step = instruction.step or instruction.item_name or instruction.item_id
        record = LaunchRecord(
            id=secrets.token_hex(4),
            task_id=instruction.task_id,
            run_id=instruction.run_id,
            item_id=instruction.item_id,
            step=f"{step}/{target.branch}" if target.branch else step,
            agent=agent.name,
            argv=tuple(argv),
            workspace=str(workspace),
            started_at=_now(),
            assignment=instruction.assignment_token,
            branch=target.branch,
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

    def relaunch(
        self, instruction: Instruction, *, resume: bool, branch: str | None = None
    ) -> LaunchRecord:
        """Start the step's (or ``branch``'s) agent again, continuing its last
        session when ``resume``.  Refused while a run is still going."""
        target = self._target(instruction, branch)
        latest = self._latest(instruction, target)
        if latest is not None and not latest.finished:
            raise StateError(
                f"the headless run {latest.id} of {_what(target)} is still going; "
                "wait for it"
            )
        if resume and latest is None:
            raise StateError(f"{_what(target)} has no headless run to resume")
        if not self._has_room(target.agent):
            raise StateError(
                f"{target.agent} already runs as many headless runs as its "
                "max_parallel allows; wait for one to end first"
            )
        return self.launch(instruction, target, resume=latest if resume else None)

    def release(self, instruction: Instruction, *, branch: str | None = None) -> None:
        """Give the step (or ``branch``) back to a session: ww stops waiting
        on its runs."""
        target = self._target(instruction, branch)
        latest = self._latest(instruction, target)
        if latest is None:
            raise StateError(f"{_what(target)} has no headless run to give back")
        if latest.released:
            raise StateError(f"{_what(target)} was already given back to a session")
        if not latest.finished:
            raise StateError(
                f"the headless run {latest.id} of {_what(target)} is still going; "
                "wait for it"
            )
        self.store.release(latest)

    def _target(self, instruction: Instruction, branch: str | None) -> Target:
        if branch is None:
            if not is_headless_dispatch(instruction):
                raise StateError("the current step does not run headless")
            return _step_target(instruction)
        found = next(
            (each for each in instruction.parallel_branches if each.name == branch),
            None,
        )
        if found is None or not is_headless_group(instruction):
            raise StateError(f"the current step has no branch {branch!r} in progress")
        if found.requested_run != "headless":
            raise StateError(f"branch {branch!r} does not run headless")
        return _branch_target(instruction, found)

    def stop_all(self, task_id: str) -> tuple[LaunchRecord, ...]:
        """Stop every run of ``task_id`` still going, such as on ``reset``."""
        stopped = []
        for path in sorted(
            (self.store.tasks / task_id / "runs").glob("*/headless/*.json")
        ):
            record = _started(path)
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
        task, branch = instruction.task_id, record.branch
        state: HeadlessState = "failed" if record.finished else "running"
        failed = state == "failed"
        return HeadlessView(
            state=state,
            agent=record.agent,
            launch_id=record.id,
            status=record.status,
            reason=failure_reason(record) if failed else None,
            exit_code=record.exit_code,
            session_id=record.session_id,
            started_at=record.started_at,
            ended_at=record.ended_at,
            output_log=str(self.store.output_path(record)),
            error_log=str(self.store.error_path(record)),
            output_tail=_tail(self.store.output_path(record)) if failed else (),
            error_tail=_tail(self.store.error_path(record)) if failed else (),
            usage=record.usage,
            wait_command=headless_wait_command(task),
            resume_command=(
                headless_launch_command(task, "resume", branch=branch)
                if record.session_id is not None
                else None
            ),
            relaunch_command=headless_launch_command(task, branch=branch),
            session_command=headless_launch_command(task, "session", branch=branch),
        )

    def _pending_view(
        self, instruction: Instruction, target: Target, state: HeadlessState
    ) -> HeadlessView:
        task, branch = instruction.task_id, target.branch
        return HeadlessView(
            state=state,
            agent=target.agent,
            wait_command=headless_wait_command(task),
            relaunch_command=headless_launch_command(task, branch=branch),
            session_command=headless_launch_command(task, "session", branch=branch),
        )


def _what(target: Target) -> str:
    return f"branch {target.branch!r}" if target.branch else "this step"


def _started(path: Path, seconds: float = STARTUP_SECONDS) -> LaunchRecord | None:
    """The record at ``path`` once its supervisor has recorded its pid.

    A run just launched is ``starting`` until its supervisor's first write;
    stopping it before then would leave the supervisor to start the agent
    anyway.  After ``seconds`` the record is taken as it is.
    """
    deadline = time.monotonic() + seconds
    record = read_record(path)
    while (
        record is not None
        and not record.finished
        and record.supervisor_pid is None
        and time.monotonic() < deadline
    ):
        time.sleep(0.1)
        record = read_record(path)
    return record


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
    work = "recording the branch's artifact" if record.branch else "completing the step"
    if record.exit_code:
        return f"it exited with code {record.exit_code} without {work}"
    return f"it ended without {work}"
