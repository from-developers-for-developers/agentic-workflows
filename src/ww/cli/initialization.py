# SPDX-License-Identifier: GPL-3.0-or-later
"""Interactive and flag-driven project initialization."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import yaml

from ww.defaults import SKILLS, WW_SKILL_NAME, skill_location
from ww.discovery import AGENT_DIRECTORIES
from ww.errors import StateError
from ww.output_adapters.terminal import initialization_progress
from ww.results import InitializationResult
from ww.storage import Storage

from .prompts import (
    _ask_checklist,
    _ask_choice,
    _ask_names,
    _ask_yes_no,
    _interactive_terminal,
)


def _init_choices(storage: Storage) -> dict[str, object]:
    path = storage.runtime_path / "init-choices.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read saved init choices: {error}") from error
    if not isinstance(value, dict):
        raise StateError("Saved init choices must be a JSON object")
    return value


def _save_init_choice(storage: Storage, key: str, value: object) -> None:
    choices = _init_choices(storage)
    choices[key] = value
    storage.locks.atomic_write(
        storage.runtime_path / "init-choices.json",
        json.dumps(choices, indent=2) + "\n",
    )


def _instructions_linked(storage: Storage) -> bool:
    names = {"AGENTS.md"}
    for directory, name in ((".claude", "CLAUDE.md"), (".gemini", "GEMINI.md")):
        if (storage.root / directory).is_dir() or (storage.root / name).exists():
            names.add(name)
    return all(
        (storage.root / name).is_file()
        and "@WW_AGENT_INSTRUCTIONS.md"
        in (storage.root / name).read_text(encoding="utf-8")
        for name in names
    )


def _init_prompt(percent: int, question: str) -> str:
    """Keep progress attached to the question currently being answered."""
    return f"{initialization_progress(percent)} {question}"


def _link_agent_instructions(
    storage: Storage, result: InitializationResult
) -> InitializationResult:
    """Append the approved reference while preserving existing instructions."""
    names = {"AGENTS.md"}
    for directory, filename in ((".claude", "CLAUDE.md"), (".gemini", "GEMINI.md")):
        if (storage.root / directory).is_dir():
            names.add(filename)
    names.update(
        name for name in ("CLAUDE.md", "GEMINI.md") if (storage.root / name).is_file()
    )
    created = list(result.created)
    for name in sorted(names):
        path = storage.root / name
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        if "@WW_AGENT_INSTRUCTIONS.md" in content:
            continue
        separator = (
            "\n\n"
            if content and not content.endswith("\n")
            else "\n"
            if content
            else ""
        )
        storage.locks.atomic_write(
            path, content + separator + "@WW_AGENT_INSTRUCTIONS.md\n"
        )
        created.append(name + (" (added instruction reference)" if content else ""))
    actions = tuple(
        action for action in result.actions if "@WW_AGENT_INSTRUCTIONS.md" not in action
    )
    return replace(result, created=tuple(created), actions=actions)


def _initialization_options(
    storage: Storage, args: argparse.Namespace
) -> tuple[str, str, bool, tuple[str, ...]]:
    interactive = not args.no_input and not args.json_output and sys.stdin.isatty()
    task_kind = args.task_id_format
    if task_kind is None and interactive and not _configured_task_format(storage):
        print(
            "Choose the default task ID format:\n"
            "  uuid      TASK-<unique UUID> — unique across projects (default)\n"
            "  digit     TASK-1, TASK-2, ... — sequential project numbers\n"
            "  timestamp TASK-<timestamp> — based on the creation time\n"
        )
        task_kind = _ask_choice(
            _init_prompt(5, "Task ID format [uuid/digit/timestamp] (uuid): "),
            ("uuid", "digit", "timestamp"),
            "uuid",
        )
    task_kind = task_kind or "uuid"
    workflows = (
        f"task_format: TASK-{{{task_kind}}}\n\n"
        "modes: []\nhandlers: []\nhooks: {}\nworkflows: []\n"
    )

    project: dict[str, object] = {"enabled": True, "extensions": {}}
    has_git = (storage.root / ".git").exists()
    if has_git:
        existing_git = _existing_git_settings(storage)
        existing_worktrees = existing_git.get("worktrees")
        worktrees = (
            existing_worktrees
            if isinstance(existing_worktrees, bool)
            else args.worktrees
        )
        if worktrees is None and interactive:
            worktrees = _ask_yes_no(
                _init_prompt(15, "Use Git worktrees? [y/N]: "), False
            )
        worktrees = bool(worktrees)
        existing_directory = existing_git.get("worktree_dir")
        directory = (
            existing_directory
            if isinstance(existing_directory, str) and existing_directory
            else args.worktree_dir
        )
        if worktrees and directory is None and interactive:
            directory = _ask_directory(
                _init_prompt(25, "Worktree directory (./git-worktrees): ")
            )
        directory = directory or "./git-worktrees"
        formats = {"default": "feature/{{task_id}}"}
        existing_formats = existing_git.get("branch_name_formats")
        if isinstance(existing_formats, dict):
            formats.update(existing_formats)
        formats.update(_branch_format_arguments(args.branch_format))
        if interactive:
            for workflow in _workflow_names(storage):
                if workflow in formats:
                    continue
                value = input(
                    _init_prompt(
                        35, f"Branch format for workflow {workflow!r} (blank to skip): "
                    )
                ).strip()
                if value:
                    formats[workflow] = value
        git: dict[str, object] = {
            "commit_message": "{{task_id}}: {{commit_message}}",
            "base_branches": {"default": _git_base_branch(storage.root)},
            "use_separate_branch": True,
            "branch_name_formats": formats,
            "worktrees": worktrees,
        }
        if worktrees:
            git.update(
                {
                    "worktree_dir": directory,
                    "worktree_name_format": "{{task_id}}",
                }
            )
        project = {"enabled": True, "extensions": {"ww/git": git}}

    ignore_runtime = args.update_gitignore
    choices = _init_choices(storage)
    ignore_path = storage.root / ".gitignore"
    ignored = ignore_path.is_file() and bool(
        {".ww", ".ww/"}.intersection(
            line.strip()
            for line in ignore_path.read_text(encoding="utf-8").splitlines()
        )
    )
    if ignore_runtime is None:
        ignore_runtime = True if ignored else choices.get("update_gitignore")
    if has_git and ignore_runtime is None and interactive:
        ignore_runtime = _ask_yes_no(
            _init_prompt(45, "Create or update .gitignore to exclude .ww/? [Y/n]: "),
            True,
        )
    if ignore_runtime is None:
        ignore_runtime = True
    if isinstance(ignore_runtime, bool):
        _save_init_choice(storage, "update_gitignore", ignore_runtime)
    if args.link_instructions is None:
        linked = choices.get("link_instructions")
        if _instructions_linked(storage):
            args.link_instructions = True
        elif isinstance(linked, bool):
            args.link_instructions = linked
    if args.link_instructions is None and interactive:
        print(
            "\nWW_AGENT_INSTRUCTIONS.md tells your agents how to work through ww:\n"
            "to start from `./ww discover`, follow each response, and keep going\n"
            "until the workflow is done. Referencing it from the instruction files\n"
            "your agents already read (AGENTS.md, and CLAUDE.md or GEMINI.md where\n"
            "those agents are set up) means they pick it up on their own, so you\n"
            "can just ask for the work instead of explaining ww every session.\n"
            "\n"
            "Answer no to keep those files untouched; you can add the one-line\n"
            "reference yourself later, or name ww in the request when you want it.\n"
        )
        args.link_instructions = _ask_yes_no(
            _init_prompt(
                50, "Add @WW_AGENT_INSTRUCTIONS.md to agent instruction files? [Y/n]: "
            ),
            True,
        )
    if isinstance(args.link_instructions, bool):
        _save_init_choice(storage, "link_instructions", args.link_instructions)
    return (
        workflows,
        json.dumps(project, indent=2) + "\n",
        bool(ignore_runtime),
        _skill_directories(storage, args.skills, interactive, progress=True),
    )


def _skill_location(directory: str) -> str:
    """The ``ww`` skill, whose presence marks a directory as already set up."""
    return skill_location(directory, WW_SKILL_NAME)


_SKILL_NAMES = " and ".join(SKILLS)


def _agent_directories(storage: Storage) -> tuple[str, ...]:
    """Agent directories from discovery that exist in this project."""
    return tuple(
        directory
        for directory in _known_agent_directories()
        if (storage.root / directory).is_dir()
    )


def _known_agent_directories() -> tuple[str, ...]:
    """Shared agent skills plus each integration-specific settings directory."""
    return (".agents", *dict.fromkeys(AGENT_DIRECTORIES.values()))


def _skill_directories(
    storage: Storage,
    requested: bool | None,
    interactive: bool,
    *,
    progress: bool = False,
) -> tuple[str, ...]:
    """Choose the agent directories that receive the bundled skills.

    Directories already holding the ``ww`` skill are included so
    initialization reports their files as preserved and adds any skill that
    is missing; storage never overwrites them.
    """
    saved = _init_choices(storage).get("agents", {})
    choices = dict(saved) if isinstance(saved, dict) else {}
    chosen_directories: list[str] = []
    directories = _known_agent_directories()
    undecided: list[tuple[str, bool]] = []
    for directory in directories:
        location = _skill_location(directory)
        exists = (storage.root / directory).is_dir()
        installed = (storage.root / location).exists()
        selected = choices.get(directory)
        if requested is False:
            selected = False
        elif installed or (requested and exists):
            selected = True
        elif not isinstance(selected, bool) and interactive:
            # Every open question is settled in one place below, so the
            # operator answers a single screen instead of one prompt per agent.
            undecided.append((directory, exists))
            selected = None
        if isinstance(selected, bool):
            choices[directory] = selected
            _save_init_choice(storage, "agents", choices)
        if selected is True:
            chosen_directories.append(directory)
    if undecided:
        chosen = _choose_agent_directories(undecided, progress)
        for directory, _ in undecided:
            choices[directory] = directory in chosen
            if directory in chosen:
                chosen_directories.append(directory)
        _save_init_choice(storage, "agents", choices)
    return tuple(chosen_directories)


def _choose_agent_directories(
    undecided: list[tuple[str, bool]], progress: bool
) -> set[str]:
    """Settle every open agent-directory question, in one screen where we can.

    A terminal we can redraw gets a checklist. A pipe, a dumb terminal, or a
    captured stdin gets plain questions instead: one for each directory that
    already exists, and one shared question for the agents without one.
    """
    if _interactive_terminal():
        return set(
            _ask_checklist(
                f"\nInstall the {_SKILL_NAMES} skills into which agent directories?",
                tuple(
                    (directory, "already present" if exists else "", exists)
                    for directory, exists in undecided
                ),
            )
        )
    chosen = {
        directory
        for directory, exists in undecided
        if exists
        and _ask_yes_no(
            _progress(
                progress,
                55,
                f"Install the {_SKILL_NAMES} skills into {directory}/skills? [Y/n]: ",
            ),
            True,
        )
    }
    absent = [directory for directory, exists in undecided if not exists]
    if absent:
        print("\nNo directory exists yet for these agents. ww can create one and")
        print("install its skill, so you can ask the agent to work through ww:")
        for row in _columns(absent, shutil.get_terminal_size((80, 24))[0] - 2):
            print("  " + row)
        print()
        chosen.update(
            _ask_names(
                _progress(progress, 90, "Create which? (comma-separated, or none): "),
                tuple(absent),
            )
        )
    return chosen


def _ask_directory(prompt: str) -> str | None:
    """Read a directory path, refusing an answer to the previous question.

    This prompt follows a ``[y/N]`` one, and a stray ``y`` used to be accepted
    as the directory's name: ww then created a directory called ``y`` and
    recorded it in the project configuration without complaint.
    """
    while True:
        value = input(prompt).strip()
        if not value:
            return None
        if value.lower() in {"y", "yes", "n", "no"}:
            print("That looks like an answer to the previous question.")
            print("Enter a directory path, or press Enter for ./git-worktrees.")
            continue
        return value


def _columns(names: list[str], width: int) -> list[str]:
    """Lay the names out in rows that fit, so a narrow terminal stays readable."""
    rows: list[str] = []
    row = ""
    for name in names:
        candidate = f"{row}  {name}" if row else name
        if row and len(candidate) > width:
            rows.append(row)
            row = name
        else:
            row = candidate
    if row:
        rows.append(row)
    return rows


def _progress(progress: bool, percent: int, question: str) -> str:
    return _init_prompt(percent, question) if progress else question


def _configured_task_format(storage: Storage) -> bool:
    if not storage.config_path.is_file():
        return False
    try:
        raw = yaml.safe_load(storage.config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(raw, dict) and bool(raw.get("task_format"))


def _existing_git_settings(storage: Storage) -> dict[str, object]:
    if not storage.project_config_path.is_file():
        return {}
    try:
        raw = json.loads(storage.project_config_path.read_text(encoding="utf-8"))
        extensions = raw.get("extensions", {})
        settings = extensions.get("ww/git", {})
    except (AttributeError, OSError, json.JSONDecodeError):
        return {}
    return settings if isinstance(settings, dict) else {}


def _workflow_names(storage: Storage) -> tuple[str, ...]:
    if not storage.config_path.is_file():
        return ()
    try:
        raw = yaml.safe_load(storage.config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ()
    if not isinstance(raw, dict) or not isinstance(raw.get("workflows"), list):
        return ()
    names: list[str] = []
    for item in raw["workflows"]:
        if isinstance(item, dict):
            name = item.get("name")
            if not isinstance(name, str) and len(item) == 1:
                name = next(iter(item))
            if isinstance(name, str):
                names.append(name)
    return tuple(names)


def _branch_format_arguments(values: list[str]) -> dict[str, str]:
    formats: dict[str, str] = {}
    for value in values:
        workflow, separator, branch_format = value.partition("=")
        if not separator or not workflow.strip() or not branch_format.strip():
            raise StateError("--branch-format must use WORKFLOW=FORMAT")
        formats[workflow.strip()] = branch_format.strip()
    return formats


def _git_base_branch(root: Path) -> str:
    for branch in ("master", "main"):
        reference_result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=root,
            check=False,
        )
        if reference_result.returncode == 0:
            return branch
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    current = result.stdout.strip()
    return current if current in {"master", "main"} else "main"


def _finish_initialization(
    storage: Storage, result: InitializationResult
) -> InitializationResult:
    created = list(result.created)
    actions = list(result.actions)
    try:
        raw = json.loads(storage.project_config_path.read_text(encoding="utf-8"))
        git = raw.get("extensions", {}).get("ww/git", {})
    except (AttributeError, OSError, json.JSONDecodeError):
        git = {}
    if isinstance(git, dict) and git.get("worktrees") is True:
        configured = git.get("worktree_dir")
        if isinstance(configured, str) and configured:
            directory = Path(configured)
            if not directory.is_absolute():
                directory = storage.root / directory
            if not directory.exists():
                directory.mkdir(parents=True)
                try:
                    label = str(directory.relative_to(storage.root))
                except ValueError:
                    label = str(directory)
                created.append(label)
    ignored = False
    path = storage.root / ".gitignore"
    if path.is_file():
        entries = {
            line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        }
        ignored = ".ww/" in entries or ".ww" in entries
    if not ignored and (storage.root / ".git").exists():
        actions.append("Optionally add exactly .ww/ to .gitignore.")
    missing = [
        directory
        for directory in _agent_directories(storage)
        if not (storage.root / _skill_location(directory)).exists()
    ]
    if missing:
        actions.append(
            f"Optionally install the {_SKILL_NAMES} skills with `init --skills` "
            "for: " + ", ".join(missing) + "."
        )
    return replace(result, created=tuple(created), actions=tuple(actions))
