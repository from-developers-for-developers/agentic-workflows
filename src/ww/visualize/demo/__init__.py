# SPDX-License-Identifier: GPL-3.0-or-later
"""The demo the page shows on every project: a small project and its run.

``ww.yaml``, ``ww.json`` and ``rules/`` here are a whole ww project, shipped
with the package and never part of anyone's configuration. ``record`` copies
it to a throwaway directory without git or extensions and plays an agent
through the ``demo`` workflow with ww's own CLI, keeping each command it
typed and the page ww printed in reply. The page replays that recording, so
every page in it is ww's real output.
"""

from __future__ import annotations

import contextlib
import io
import shlex
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from ww.errors import StateError
from ww.storage import Storage

DEMO_WORKFLOW = "demo"
TASK = "TASK-1"
# How the recording names the throwaway directory, wherever it really was.
SHOWN_ROOT = "~/projects/greeter"
_PROJECT_FILES = ("ww.yaml", "ww.json", "rules")


@dataclass(frozen=True)
class Act:
    """One thing the agent does: say why, write files, then run ww."""

    say: str
    argv: tuple[str, ...]
    files: tuple[tuple[str, str], ...] = ()
    role: str = "agent"
    # ww refuses this one on purpose: a check fails and the step stays open.
    refused: bool = False


def _rule_result(rule: str, checked: str) -> str:
    return (
        f'{{"id": "{rule}", "status": "judged", "verdict": "pass", '
        f'"checked": "{checked}"}}'
    )


NOTES = ".ww/tasks/TASK-1/documents/notes.md"
SCRIPT: tuple[Act, ...] = (
    Act(
        "The operator asks for a change. The agent starts a task with the demo "
        "workflow; ww records the requirements and names the next command.",
        (
            "start",
            TASK,
            "--workflow",
            DEMO_WORKFLOW,
            "--agent",
            "claudecode",
            "--requirements",
            "Add a --quiet flag to the greet command.",
            "--role",
            "manager",
        ),
        role="manager",
    ),
    Act(
        "The agent asks ww for its next assignment: the plan, and the document "
        "it must keep.",
        ("next", TASK, "--role", "manager"),
        role="manager",
    ),
    Act(
        "It writes the plan into the task's notes and completes the step.",
        (
            "complete",
            TASK,
            "--role",
            "worker",
            "--artifact=Planned the flag: greet() gains quiet=False.",
            "--summary=The plan is in the notes.",
        ),
        files=((NOTES, "# Plan\n\nAdd `--quiet` to `greet`; verify by hand.\n"),),
    ),
    Act(
        "It implements the flag, but forgets the changelog. On completion ww "
        "runs the rule's check, refuses the completion and sends it back.",
        (
            "complete",
            TASK,
            "--role",
            "worker",
            "--artifact=Added the --quiet flag.",
            "--summary=greet accepts --quiet.",
        ),
        files=(
            (
                "src/greet.py",
                "def greet(name, quiet=False):\n    if not quiet:\n"
                '        print(f"Hello, {name}!")\n',
            ),
        ),
        refused=True,
    ),
    Act(
        "It adds the changelog line and completes again. The check passes; the "
        "two judged rules now go to a verifier.",
        (
            "complete",
            TASK,
            "--role",
            "worker",
            "--artifact=Added the --quiet flag and its changelog line.",
            "--summary=greet accepts --quiet.",
        ),
        files=(("CHANGES.md", "- greet: add --quiet.\n"),),
    ),
    Act(
        "As the verifier it judges each rule against the change. ww records "
        "the step, runs run-checks itself, and hands over the review.",
        (
            "complete",
            TASK,
            "--role",
            "worker",
            "--artifact=Both rules hold for the change.",
            "--rule-result="
            + _rule_result("conventions/small-steps", "One function changed."),
            "--rule-result="
            + _rule_result("implement/1", "greet keeps its signature."),
        ),
    ),
    Act(
        "The review finds an issue, so the agent's assessment is positive.",
        (
            "complete",
            TASK,
            "--role",
            "worker",
            "--artifact=--quiet still prints the banner: there is more to do.",
            "--summary=The banner still prints with --quiet.",
        ),
    ),
    Act(
        "The manager records the outcome. Positive is a goto: ww sends the flow "
        "back to implement, with the review's reason.",
        ("next", TASK, "--role", "manager", "--outcome", "positive"),
        role="manager",
    ),
    Act(
        "Round two: the agent fixes what the review found.",
        (
            "complete",
            TASK,
            "--role",
            "worker",
            "--artifact=--quiet now hides the banner too.",
            "--summary=--quiet hides all output.",
        ),
        files=(
            (
                "src/greet.py",
                "def greet(name, quiet=False):\n    if quiet:\n        return\n"
                '    print(f"Hello, {name}!")\n',
            ),
        ),
    ),
    Act(
        "The rules are judged again for the new change.",
        (
            "complete",
            TASK,
            "--role",
            "worker",
            "--artifact=Both rules hold.",
            "--rule-result="
            + _rule_result("conventions/small-steps", "A two-line change."),
            "--rule-result="
            + _rule_result("implement/1", "The signature is unchanged."),
        ),
    ),
    Act(
        "The second review is clean: negative.",
        (
            "complete",
            TASK,
            "--role",
            "worker",
            "--artifact=No issues left.",
            "--summary=The change is clean.",
        ),
    ),
    Act(
        "Negative runs nothing extra, so the flow continues to the release notes.",
        ("next", TASK, "--role", "manager", "--outcome", "negative"),
        role="manager",
    ),
    Act(
        "The release notes are saved as task metadata.",
        (
            "complete",
            TASK,
            "--role",
            "worker",
            "--metadata",
            "release_notes=greet gains --quiet.",
            "--artifact=Wrote the release notes.",
            "--summary=Release notes saved.",
        ),
    ),
    Act(
        "Last, ww asks for a summary of the run for the task's history.",
        (
            "complete",
            TASK,
            "--role",
            "worker",
            "--variable",
            "summary=greet gains --quiet, after one review round.",
            "--artifact=Summarised the run.",
        ),
    ),
)


@dataclass
class Frame:
    say: str
    role: str
    command: str
    output: str
    files: list[dict[str, str]] = field(default_factory=list)
    # What ww ran by itself in reply, as its records say.
    automatic: list[dict[str, str]] = field(default_factory=list)
    # The step the run is at once the command returns, by plan path.
    step: str | None = None
    status: str | None = None
    refused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "say": self.say,
            "role": self.role,
            "command": self.command,
            "output": self.output,
            "files": self.files,
            "automatic": self.automatic,
            "step": self.step,
            "status": self.status,
            "refused": self.refused,
        }


@contextlib.contextmanager
def demo_project() -> Iterator[Path]:
    """A fresh copy of the demo project in a directory of its own."""
    with (
        as_file(files("ww.visualize").joinpath("demo")) as source,
        tempfile.TemporaryDirectory(prefix="ww-demo-") as directory,
    ):
        root = Path(directory) / "greeter"
        root.mkdir()
        for name in _PROJECT_FILES:
            origin = Path(source) / name
            if origin.is_dir():
                shutil.copytree(origin, root / name)
            else:
                shutil.copy2(origin, root / name)
        yield root


def record() -> list[dict[str, Any]]:
    """Play the script through ww and keep every command and page."""
    with demo_project() as root:
        return [frame.to_dict() for frame in _play(root)]


def demo() -> dict[str, Any]:
    """The demo workflow as the Workflows tab shows it, and its recording."""
    from ww.visualize.atlas import build_atlas
    from ww.visualize.project import Project

    with demo_project() as root:
        atlas = build_atlas(Project.load(Storage(root)))
    workflow = next(
        entry for entry in atlas["workflows"] if entry["name"] == DEMO_WORKFLOW
    )
    rules = [
        group
        for group in atlas["library"]["rules"]["groups"]
        if group["origin"] == "configuration"
    ]
    return {
        "workflow": workflow,
        "rules": rules,
        "root": SHOWN_ROOT,
        "frames": record(),
    }


def _play(root: Path) -> Iterator[Frame]:
    seen: dict[str, str] = {}
    for act in SCRIPT:
        for relative, content in act.files:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        output, code = _run(root, act.argv)
        if (code != 0) != act.refused:
            raise StateError(
                f"the demo stopped at `ww {shlex.join(act.argv)}`: {output.strip()}"
            )
        step, status, automatic = _progress(root, seen)
        yield Frame(
            say=act.say,
            role=act.role,
            command="ww " + shlex.join(act.argv),
            output=_shown(output, root),
            files=[{"path": path, "content": content} for path, content in act.files],
            automatic=automatic,
            step=step,
            status=status,
            refused=act.refused,
        )


def _run(root: Path, argv: tuple[str, ...]) -> tuple[str, int]:
    # The CLI imports this package to serve the page; import it only to run it.
    from ww.cli.main import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--root", str(root), *argv])
    return out.getvalue() + (err.getvalue() if code else ""), code


def _progress(
    root: Path, seen: dict[str, str]
) -> tuple[str | None, str | None, list[dict[str, str]]]:
    """Where the run is, and which automatic items ww completed since last."""
    tasks = Storage(root).task_persistence
    state = tasks.read_execution_state(TASK)
    snapshot = tasks.read_plan_snapshot(TASK)
    if state is None or snapshot is None:
        return None, None, []
    automatic = []
    for item, record in zip(snapshot.plan.items, state.item_executions, strict=True):
        key = f"{item.id}:{record.goto_round}"
        if (
            item.owner == "ww"
            and record.status == "completed"
            and seen.get(key) != record.status
        ):
            automatic.append(
                {"name": item.name, "step": item.step, "result": record.result or ""}
            )
        seen[key] = record.status
    plan = snapshot.plan
    step = plan.items[state.cursor].step if state.cursor < len(plan.items) else None
    # An assessment waiting for its outcome is where the run is, though the
    # cursor has moved past it.
    for item, record in zip(plan.items, state.item_executions, strict=True):
        if (
            item.assessment_question
            and record.status == "completed"
            and record.assessment_outcome is None
        ):
            step = item.step
    return step, state.status, automatic


def _shown(text: str, root: Path) -> str:
    """The page as the recording shows it, with the directory named neutrally."""
    # The longer spelling first: a resolved temporary directory on macOS is
    # the other one behind /private.
    for spelling in sorted({str(root), str(root.resolve())}, key=len, reverse=True):
        text = text.replace(spelling, SHOWN_ROOT)
    return text
